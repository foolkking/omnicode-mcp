#!/usr/bin/env python
"""Hybrid durability soak for edit/sync/freshness/pending queue behavior.

This runner is intentionally smaller than the large-repo benchmark. It creates
a throwaway local workspace, starts a temporary cloud-index backend, and loops
through safe local edits plus cloud sync/search checks. It also exercises the
cloud-down pending queue path by stopping the backend, attempting a sync, then
restarting the backend and draining pending work.

Quick smoke:

    python scripts/soak_hybrid_durability.py \
        --duration-s 30 --max-iterations 6 --sleep-s 0 --json

Duration-bound release smoke:

    python scripts/soak_hybrid_durability.py --duration-s 60 --json

Longer production soak:

    python scripts/soak_hybrid_durability.py \
        --duration-s 1800 --max-iterations 0 --sleep-s 1 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / ".tmp_soak" / "hybrid-durability"


@dataclass
class Step:
    name: str
    ok: bool
    elapsed_ms: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SoakResult:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, ok: bool, started: float, **details: Any) -> None:
        self.steps.append(
            Step(
                name=name,
                ok=ok,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                details=details,
            )
        )

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [
                {
                    "name": step.name,
                    "ok": step.ok,
                    "elapsed_ms": step.elapsed_ms,
                    "details": step.details,
                }
                for step in self.steps
            ],
        }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _safe_rmtree(path: Path) -> None:
    target = path.resolve()
    if not target.exists():
        return
    allowed = (ROOT / ".tmp_soak").resolve()
    target_text = str(target).replace("\\", "/").lower()
    allowed_text = str(allowed).replace("\\", "/").lower()
    if target_text != allowed_text and not target_text.startswith(allowed_text + "/"):
        raise RuntimeError(f"Refusing to remove non-soak path: {target}")

    def _remove_readonly(func: Any, failed_path: str, _exc_info: Any) -> None:
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(target, onerror=_remove_readonly)


def _json_request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    timeout: float = 30.0,
    **kwargs: Any,
) -> dict[str, Any]:
    response = client.request(method, path, timeout=timeout, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _start_backend(
    *,
    port: int,
    state_dir: Path,
    cloud_workspace: Path,
    log_dir: Path,
    log_prefix: str,
) -> tuple[subprocess.Popen[str], Path, Path]:
    cloud_workspace.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{log_prefix}.stdout.log"
    stderr_path = log_dir / f"{log_prefix}.stderr.log"
    env = os.environ.copy()
    pythonpath = str(ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env.update(
        {
            "PYTHONPATH": pythonpath,
            "OMNICODE_STATE_DIR": str(state_dir),
            "OMNICODE_WORKSPACE_REGISTRY": str(state_dir / "workspaces.json"),
            "OMNICODE_SYNC_SEMANTIC_INITIAL_MODE": "exact_only",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "omnicode_adapters.cli.main",
        "serve",
        "--headless",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--mode",
        "cloud-index",
        "--state-dir",
        str(state_dir),
        "--materialize-mirror",
        "true",
        "--mirror-readonly",
        "true",
    ]
    stdout = stdout_path.open("w", encoding="utf-8", newline="\n")
    stderr = stderr_path.open("w", encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        command,
        cwd=str(cloud_workspace),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    stdout.close()
    stderr.close()
    return process, stdout_path, stderr_path


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    last_error = ""
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"backend exited early with code {process.returncode}")
            try:
                payload = _json_request(client, "GET", "/health", timeout=5.0)
                if payload.get("status") == "healthy":
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
    raise TimeoutError(f"backend did not become healthy: {last_error}")


def _stop_backend(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _agent_client(
    *,
    base_url: str,
    workspace: Path,
    workspace_id: str,
    state_dir: Path,
):
    from omnicode_adapters.agent.client import AgentClient

    return AgentClient(
        remote=base_url,
        workspace=workspace,
        workspace_id=workspace_id,
        timeout=20.0,
        max_retries=1,
        batch_max_files=10,
        batch_max_bytes=200_000,
        manifest_path=state_dir / "agent-manifest.json",
        record_manifest=True,
    )


def _write_baseline(workspace: Path) -> None:
    target = workspace / "src" / "service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        'VALUE = "baseline"\n\n'
        "def value():\n"
        "    return VALUE\n",
        encoding="utf-8",
        newline="\n",
    )


def _content_for(marker: str) -> str:
    return (
        f'VALUE = "{marker}"\n\n'
        "def value():\n"
        "    return VALUE\n"
    )


def _pending_count(manifest_path: Path) -> int:
    if not manifest_path.exists():
        return 0
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    pending = data.get("pending") or []
    return len(pending) if isinstance(pending, list) else -1


def _sync_status(client: httpx.Client, workspace_id: str) -> dict[str, Any]:
    return _json_request(
        client,
        "GET",
        "/sync/status",
        params={"workspace_id": workspace_id},
        timeout=10.0,
    )


def _wait_exact_fresh(
    client: httpx.Client,
    *,
    workspace_id: str,
    min_revision: int,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _sync_status(client, workspace_id)
        exact_revision = int(last.get("exact_indexed_revision") or 0)
        if exact_revision >= min_revision:
            return last
        time.sleep(0.25)
    return last


def _search_text(
    client: httpx.Client,
    *,
    workspace_id: str,
    query: str,
) -> dict[str, Any]:
    return _json_request(
        client,
        "POST",
        "/search/text",
        headers={"X-Omnicode-Workspace": workspace_id},
        params={
            "query": query,
            "file_pattern": "*.py",
            "case_sensitive": "true",
            "max_results": 5,
            "context_lines": 0,
        },
        timeout=10.0,
    )


def _apply_validate_sync_search(
    *,
    base_url: str,
    workspace: Path,
    workspace_id: str,
    state_dir: Path,
    marker: str,
) -> dict[str, Any]:
    from omnicode_core.edit.patch import PatchManager

    manager = PatchManager(str(workspace))
    rel = "src/service.py"
    content = _content_for(marker)
    preview = manager.preview_patch(rel, content)
    validation = asyncio.run(manager.validate_patch(rel, content))
    applied = manager.apply_patch(rel, content, source="soak")

    sync_result: dict[str, Any]
    agent = _agent_client(
        base_url=base_url,
        workspace=workspace,
        workspace_id=workspace_id,
        state_dir=state_dir,
    )
    try:
        sync_result = agent.push_file(rel).to_dict()
    finally:
        agent.close()

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        accepted = int(sync_result.get("accepted_revision") or 0)
        status = _wait_exact_fresh(
            client,
            workspace_id=workspace_id,
            min_revision=accepted,
        )
        search = _search_text(client, workspace_id=workspace_id, query=marker)

    rows = search.get("results") or []
    found = any(row.get("file_path") == rel for row in rows)
    return {
        "ok": (
            preview.success
            and validation.success
            and applied.success
            and not sync_result.get("errors")
            and found
        ),
        "preview": preview.success,
        "validation": validation.success,
        "session_id": applied.session_id,
        "rollback_available": applied.rollback_available,
        "sync": sync_result,
        "status": {
            "accepted_revision": status.get("accepted_revision"),
            "exact_indexed_revision": status.get("exact_indexed_revision"),
            "exact_index_ready": status.get("exact_index_ready"),
        },
        "search_found": found,
    }


def _rollback_sync_assert_absent(
    *,
    base_url: str,
    workspace: Path,
    workspace_id: str,
    state_dir: Path,
    session_id: str,
    marker: str,
) -> dict[str, Any]:
    from omnicode_core.edit.patch import PatchManager

    manager = PatchManager(str(workspace))
    rolled = manager.rollback_patch(session_id)
    agent = _agent_client(
        base_url=base_url,
        workspace=workspace,
        workspace_id=workspace_id,
        state_dir=state_dir,
    )
    try:
        sync_result = agent.push_file("src/service.py").to_dict()
    finally:
        agent.close()

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        accepted = int(sync_result.get("accepted_revision") or 0)
        status = _wait_exact_fresh(
            client,
            workspace_id=workspace_id,
            min_revision=accepted,
        )
        search = _search_text(client, workspace_id=workspace_id, query=marker)

    rows = search.get("results") or []
    still_found = any(row.get("file_path") == "src/service.py" for row in rows)
    return {
        "ok": rolled.success and not sync_result.get("errors") and not still_found,
        "rollback": rolled.success,
        "sync": sync_result,
        "status": {
            "accepted_revision": status.get("accepted_revision"),
            "exact_indexed_revision": status.get("exact_indexed_revision"),
            "exact_index_ready": status.get("exact_index_ready"),
        },
        "marker_absent_after_rollback": not still_found,
    }


def _cloud_down_pending_cycle(
    *,
    base_url: str,
    workspace: Path,
    workspace_id: str,
    state_dir: Path,
    process: subprocess.Popen[str],
    port: int,
    cloud_workspace: Path,
    log_dir: Path,
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    marker = "soak-cloud-down"
    rel = "src/service.py"
    manifest_path = state_dir / "agent-manifest.json"
    _stop_backend(process)

    from omnicode_core.edit.patch import PatchManager

    manager = PatchManager(str(workspace))
    content = _content_for(marker)
    preview = manager.preview_patch(rel, content)
    applied = manager.apply_patch(rel, content, source="soak-cloud-down")
    agent = _agent_client(
        base_url=base_url,
        workspace=workspace,
        workspace_id=workspace_id,
        state_dir=state_dir,
    )
    try:
        failed_sync = agent.push_file(rel).to_dict()
    finally:
        agent.close()
    pending_after_failure = _pending_count(manifest_path)

    restarted, _stdout, _stderr = _start_backend(
        port=port,
        state_dir=state_dir,
        cloud_workspace=cloud_workspace,
        log_dir=log_dir,
        log_prefix="cloud-restarted",
    )
    _wait_for_health(base_url, restarted)

    agent = _agent_client(
        base_url=base_url,
        workspace=workspace,
        workspace_id=workspace_id,
        state_dir=state_dir,
    )
    try:
        flush = agent.flush_pending(max_batches=5).to_dict()
    finally:
        agent.close()
    pending_after_flush = _pending_count(manifest_path)

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        accepted = int(flush.get("accepted_revision") or 0)
        status = _wait_exact_fresh(
            client,
            workspace_id=workspace_id,
            min_revision=accepted,
        )
        search = _search_text(client, workspace_id=workspace_id, query=marker)

    found = any(
        row.get("file_path") == rel for row in (search.get("results") or [])
    )
    return restarted, {
        "ok": (
            preview.success
            and applied.success
            and bool(failed_sync.get("errors"))
            and pending_after_failure > 0
            and not flush.get("errors")
            and pending_after_flush == 0
            and found
        ),
        "preview": preview.success,
        "session_id": applied.session_id,
        "failed_sync_errors": failed_sync.get("errors"),
        "pending_after_failure": pending_after_failure,
        "flush": flush,
        "pending_after_flush": pending_after_flush,
        "search_found_after_flush": found,
        "status": {
            "accepted_revision": status.get("accepted_revision"),
            "exact_indexed_revision": status.get("exact_indexed_revision"),
            "exact_index_ready": status.get("exact_index_ready"),
        },
    }


def run_soak(args: argparse.Namespace) -> SoakResult:
    run_started = time.monotonic()
    root = Path(args.root).expanduser().resolve()
    local_workspace = root / "local-workspace"
    state_dir = root / "state"
    cloud_workspace = root / "cloud-workspace"
    log_dir = root / "logs"
    workspace_id = args.workspace_id
    port = int(args.port or _free_port())
    base_url = f"http://127.0.0.1:{port}"
    result = SoakResult()

    if args.reset_state:
        _safe_rmtree(root)
    local_workspace.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_baseline(local_workspace)

    process, stdout_path, stderr_path = _start_backend(
        port=port,
        state_dir=state_dir,
        cloud_workspace=cloud_workspace,
        log_dir=log_dir,
        log_prefix="cloud",
    )
    try:
        started = time.perf_counter()
        _wait_for_health(base_url, process)
        result.add("backend_health", True, started, backend_url=base_url)

        started = time.perf_counter()
        agent = _agent_client(
            base_url=base_url,
            workspace=local_workspace,
            workspace_id=workspace_id,
            state_dir=state_dir,
        )
        try:
            initial = agent.push_file("src/service.py").to_dict()
        finally:
            agent.close()
        result.add(
            "initial_sync",
            not initial.get("errors"),
            started,
            **initial,
        )

        duration_s = max(1.0, float(args.duration_s))
        max_iterations = int(args.max_iterations or 0)
        sleep_s = max(0.0, float(args.sleep_s or 0.0))
        deadline = time.monotonic() + duration_s
        iteration = 0
        ended_by = "duration"
        applied_sessions: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            if max_iterations > 0 and iteration >= max_iterations:
                ended_by = "max_iterations"
                break
            iteration += 1
            marker = f"soak-v{iteration:04d}"
            started = time.perf_counter()
            edit = _apply_validate_sync_search(
                base_url=base_url,
                workspace=local_workspace,
                workspace_id=workspace_id,
                state_dir=state_dir,
                marker=marker,
            )
            result.add(
                f"edit_sync_search_{iteration}",
                bool(edit["ok"]),
                started,
                **{k: v for k, v in edit.items() if k != "ok"},
            )
            if edit.get("session_id"):
                applied_sessions.append({
                    "session_id": str(edit["session_id"]),
                    "marker": marker,
                })

            if (
                args.rollback_every
                and iteration % int(args.rollback_every) == 0
                and applied_sessions
            ):
                target = applied_sessions.pop()
                started = time.perf_counter()
                rollback = _rollback_sync_assert_absent(
                    base_url=base_url,
                    workspace=local_workspace,
                    workspace_id=workspace_id,
                    state_dir=state_dir,
                    session_id=target["session_id"],
                    marker=target["marker"],
                )
                result.add(
                    f"rollback_sync_{iteration}",
                    bool(rollback["ok"]),
                    started,
                    **{k: v for k, v in rollback.items() if k != "ok"},
                )

            if int(args.cloud_down_at) > 0 and iteration == int(args.cloud_down_at):
                started = time.perf_counter()
                process, cloud_down = _cloud_down_pending_cycle(
                    base_url=base_url,
                    workspace=local_workspace,
                    workspace_id=workspace_id,
                    state_dir=state_dir,
                    process=process,
                    port=port,
                    cloud_workspace=cloud_workspace,
                    log_dir=log_dir,
                )
                result.add(
                    "cloud_down_pending_flush",
                    bool(cloud_down["ok"]),
                    started,
                    **{k: v for k, v in cloud_down.items() if k != "ok"},
                )

            remaining_s = deadline - time.monotonic()
            if sleep_s > 0 and remaining_s > 0:
                time.sleep(min(sleep_s, remaining_s))

        started = time.perf_counter()
        with httpx.Client(base_url=base_url, timeout=20.0) as client:
            status = _sync_status(client, workspace_id)
        manifest_pending = _pending_count(state_dir / "agent-manifest.json")
        result.add(
            "final_status",
            manifest_pending == 0
            and status.get("exact_index_ready") is True
            and int(status.get("exact_pending_revisions") or 0) == 0,
            started,
            iterations=iteration,
            ended_by=ended_by,
            duration_target_s=duration_s,
            elapsed_s=round(time.monotonic() - run_started, 3),
            max_iterations=max_iterations,
            sleep_s=sleep_s,
            manifest_pending=manifest_pending,
            accepted_revision=status.get("accepted_revision"),
            exact_indexed_revision=status.get("exact_indexed_revision"),
            exact_index_ready=status.get("exact_index_ready"),
            exact_pending_revisions=status.get("exact_pending_revisions"),
            semantic_pending_revisions=status.get("semantic_pending_revisions"),
        )
    finally:
        _stop_backend(process)
        result.steps.append(
            Step(
                name="backend_logs",
                ok=True,
                elapsed_ms=0,
                details={
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                },
            )
        )
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a hybrid edit/sync durability soak."
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--workspace-id", default="soak-hybrid-durability")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Maximum edit iterations; 0 means run until --duration-s expires.",
    )
    parser.add_argument(
        "--sleep-s",
        type=float,
        default=0.5,
        help="Delay between iterations so duration-bound soaks actually soak.",
    )
    parser.add_argument("--rollback-every", type=int, default=3)
    parser.add_argument("--cloud-down-at", type=int, default=2)
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    result = run_soak(args)
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print()
        print("PASS" if result.ok else "FAIL")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
