#!/usr/bin/env python
"""Clean-room large-repo hybrid benchmark runner.

This script starts a temporary cloud-index backend, pushes a local large
repository through the hybrid sync agent, and verifies the production contract:

* snapshot/object store receives the repository
* exact symbol/text index is ready after initial sync
* large initial sync stays exact-first by default
* strict semantic search is blocked until explicit semantic bootstrap

Example:

    python scripts/benchmark_large_repo_hybrid.py \
        --repo C:/omnicode-sim/benchmark-repos/django \
        --state-dir C:/omnicode-sim/state-bench-django \
        --workspace-id django-cleanroom

Add --semantic-bootstrap to also force a full semantic snapshot bootstrap.
"""

from __future__ import annotations

import argparse
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
DEFAULT_REPO = Path("C:/omnicode-sim/benchmark-repos/django")
DEFAULT_STATE = Path("C:/omnicode-sim/state-bench-django")
DEFAULT_CLOUD_WORKSPACE = Path("C:/omnicode-sim/cloud-workspaces/large-repo-bench")


@dataclass
class Step:
    name: str
    ok: bool
    elapsed_ms: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchResult:
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


def _safe_rmtree(path: Path) -> None:
    target = path.resolve()
    if not target.exists():
        return
    text = str(target).replace("\\", "/").lower()
    allowed_roots = [
        str(DEFAULT_STATE.parent.resolve()).replace("\\", "/").lower(),
        str((ROOT / ".tmp_benchmarks").resolve()).replace("\\", "/").lower(),
    ]
    if not any(text.startswith(root + "/") for root in allowed_roots):
        raise RuntimeError(f"Refusing to remove non-benchmark path: {target}")
    def _remove_readonly(func: Any, failed_path: str, _exc_info: Any) -> None:
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(target, onerror=_remove_readonly)


def _start_backend(
    *,
    port: int,
    state_dir: Path,
    cloud_workspace: Path,
    log_dir: Path,
) -> tuple[subprocess.Popen[str], Path, Path]:
    cloud_workspace.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "cloud.stdout.log"
    stderr_path = log_dir / "cloud.stderr.log"
    env = os.environ.copy()
    pythonpath = str(ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env.update(
        {
            "PYTHONPATH": pythonpath,
            "OMNICODE_STATE_DIR": str(state_dir),
            "OMNICODE_WORKSPACE_REGISTRY": str(state_dir / "workspaces.json"),
            "OMNICODE_SYNC_SEMANTIC_INITIAL_MODE": "auto",
            "OMNICODE_SYNC_SEMANTIC_INITIAL_FILE_LIMIT": "2000",
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


def _initial_sync(
    *,
    base_url: str,
    repo: Path,
    workspace_id: str,
    state_dir: Path,
    batch_max_files: int,
    batch_max_bytes: int,
) -> dict[str, Any]:
    from omnicode_adapters.agent.client import AgentClient
    from omnicode_adapters.agent.watcher import Watcher

    client = AgentClient(
        remote=base_url,
        workspace=repo,
        workspace_id=workspace_id,
        timeout=60.0,
        max_retries=3,
        batch_max_files=batch_max_files,
        batch_max_bytes=batch_max_bytes,
        manifest_path=state_dir / "agent-manifest.json",
        record_manifest=True,
    )
    try:
        watcher = Watcher(client=client, workspace=repo, printer=lambda _msg: None)
        result = watcher.initial_sync()
        return result.to_dict()
    finally:
        client.close()


def _assert_status_contract(
    *,
    status: dict[str, Any],
    min_files: int,
) -> dict[str, Any]:
    snapshot_files = int((status.get("snapshot_store") or {}).get("files") or 0)
    exact = status.get("exact_index") or {}
    exact_files = int(exact.get("files") or 0)
    exact_symbols = int(exact.get("symbols") or 0)
    line_fts_available = bool(exact.get("line_fts_available", False))
    accepted = int(status.get("accepted_revision") or 0)
    exact_revision = int(status.get("exact_indexed_revision") or 0)
    contract = status.get("index_readiness_contract") or {}
    failures: list[str] = []

    if snapshot_files < min_files:
        failures.append(f"snapshot files below threshold: {snapshot_files} < {min_files}")
    if exact_files < min_files:
        failures.append(f"exact files below threshold: {exact_files} < {min_files}")
    if exact_symbols <= 0:
        failures.append("exact symbols missing")
    if exact_revision < accepted:
        failures.append(f"exact index stale: {exact_revision} < {accepted}")
    if status.get("exact_index_ready") is not True:
        failures.append("exact_index_ready is not true")
    if status.get("recommended_query_mode") != "exact_first":
        failures.append(
            "recommended_query_mode should be exact_first for large initial sync"
        )
    if status.get("query_mode_reason") != "exact_only_initial_sync":
        failures.append("query_mode_reason should be exact_only_initial_sync")
    if contract.get("schema_version") != "index_readiness.v1":
        failures.append("missing index_readiness.v1 contract")

    return {
        "ok": not failures,
        "failures": failures,
        "snapshot_files": snapshot_files,
        "exact_files": exact_files,
        "exact_symbols": exact_symbols,
        "line_fts_available": line_fts_available,
        "accepted_revision": accepted,
        "exact_indexed_revision": exact_revision,
        "recommended_query_mode": status.get("recommended_query_mode"),
        "query_mode_reason": status.get("query_mode_reason"),
    }


def _details_without_ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "ok"}


def _assert_exact_symbol(
    client: httpx.Client,
    *,
    workspace_id: str,
    symbol: str,
    expected_file: str,
) -> dict[str, Any]:
    result = _json_request(
        client,
        "POST",
        "/search/symbols",
        headers={"X-Omnicode-Workspace": workspace_id},
        params={"query": symbol, "fuzzy": "false", "max_results": 5},
        timeout=20.0,
    )
    rows = result.get("results") or []
    first = rows[0] if rows else {}
    ok = (
        bool(rows)
        and first.get("file_path") == expected_file
        and first.get("symbol_name") == symbol
        and first.get("source") in {"exact_index", "snapshot_store"}
    )
    return {
        "ok": ok,
        "first": first,
        "count": len(rows),
        "snapshot_fast_path": result.get("snapshot_fast_path"),
        "exact_index_used": result.get("exact_index_used"),
    }


def _assert_text_search(
    client: httpx.Client,
    *,
    workspace_id: str,
    query: str,
    expected_file: str,
    file_pattern: str,
) -> dict[str, Any]:
    result = _json_request(
        client,
        "POST",
        "/search/text",
        headers={"X-Omnicode-Workspace": workspace_id},
        params={
            "query": query,
            "file_pattern": file_pattern,
            "case_sensitive": "true",
            "max_results": 5,
            "context_lines": 1,
        },
        timeout=20.0,
    )
    rows = result.get("results") or []
    first = rows[0] if rows else {}
    ok = first.get("file_path") == expected_file
    return {"ok": ok, "first": first, "count": len(rows)}


def _assert_semantic_exact_rank(
    client: httpx.Client,
    *,
    workspace_id: str,
    symbol: str,
    expected_file: str,
) -> dict[str, Any]:
    response = client.post(
        "/search",
        headers={"X-Omnicode-Workspace": workspace_id},
        json={"query": symbol, "search_type": "semantic", "max_results": 5},
        timeout=30.0,
    )
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else {}
    if not isinstance(result, dict):
        result = {}
    rows = result.get("results") or []
    first = rows[0] if rows else {}
    why = first.get("why_matched") or []

    if response.status_code == 409:
        ok = (
            result.get("ok") is False
            and result.get("error_code") == "SEMANTIC_INDEX_NOT_READY"
            and result.get("fallback_used") is True
            and bool(rows)
            and first.get("file_path") == expected_file
            and first.get("symbol_name") == symbol
        )
        return {
            "ok": ok,
            "policy": "semantic_not_ready_exact_fallback",
            "status_code": response.status_code,
            "error_code": result.get("error_code"),
            "first": first,
            "count": len(rows),
            "fallback_used": result.get("fallback_used"),
            "fallback_reason": result.get("fallback_reason"),
            "capabilities_missing": result.get("capabilities_missing"),
            "snapshot_exact_boost": result.get("snapshot_exact_boost"),
            "snapshot_lexical_boost": result.get("snapshot_lexical_boost"),
        }

    response.raise_for_status()
    ok = (
        bool(rows)
        and first.get("file_path") == expected_file
        and first.get("symbol_name") == symbol
        and first.get("source") == "exact_index"
        and first.get("rank_reason") == "exact_symbol_before_semantic"
        and "semantic:exact_boost" in why
    )
    return {
        "ok": ok,
        "policy": "semantic_ready_exact_rank",
        "status_code": response.status_code,
        "first": first,
        "count": len(rows),
        "snapshot_exact_boost": result.get("snapshot_exact_boost"),
        "snapshot_lexical_boost": result.get("snapshot_lexical_boost"),
    }


def _assert_context_snapshot_anchor(
    client: httpx.Client,
    *,
    workspace_id: str,
    revision: int,
    symbol: str,
    expected_file: str,
) -> dict[str, Any]:
    result = _json_request(
        client,
        "POST",
        "/intelligence/context",
        headers={
            "X-Omnicode-Workspace": workspace_id,
            "X-Omnicode-Min-Revision": str(revision),
        },
        json={
            "file_path": expected_file,
            "symbol": symbol,
            "query": symbol,
            "include_memory": False,
            "include_git_history": False,
            "max_search_results": 5,
        },
        timeout=60.0,
    )
    search = result.get("search") or {}
    rows = search.get("results") or []
    first = rows[0] if rows else {}
    quality = result.get("context_quality") or {}
    ok = (
        result.get("snapshot_exact_symbol") is True
        and (first.get("file") or first.get("file_path")) == expected_file
        and first.get("symbol") == symbol
        and quality.get("primary_anchor") == "snapshot_exact_symbol"
    )
    return {
        "ok": ok,
        "snapshot_exact_symbol": result.get("snapshot_exact_symbol"),
        "freshness": result.get("freshness"),
        "first": first,
        "context_quality": quality,
        "context_fast_path": result.get("context_fast_path"),
    }


def _assert_strict_semantic_stale(
    client: httpx.Client,
    *,
    workspace_id: str,
    revision: int,
    query: str,
) -> dict[str, Any]:
    payload = _json_request(
        client,
        "POST",
        "/search",
        headers={
            "X-Omnicode-Workspace": workspace_id,
            "X-Omnicode-Min-Revision": str(revision),
        },
        json={"query": query, "search_type": "semantic", "max_results": 5},
        timeout=20.0,
    )
    ok = (
        payload.get("ok") is False
        and payload.get("stale") is True
        and payload.get("recommended_query_mode") == "exact_first"
        and payload.get("strict_semantic_safe") is False
    )
    return {
        "ok": ok,
        "error": payload.get("error"),
        "freshness": payload.get("freshness"),
        "recommended_query_mode": payload.get("recommended_query_mode"),
        "query_mode_reason": payload.get("query_mode_reason"),
    }


def _run_semantic_bootstrap(
    client: httpx.Client,
    *,
    workspace_id: str,
    timeout_s: int,
) -> dict[str, Any]:
    result = _json_request(
        client,
        "POST",
        "/search/index",
        headers={"X-Omnicode-Workspace": workspace_id},
        params={
            "workspace_id": workspace_id,
            "force": "true",
            "scope": "semantic",
            "background": "true",
        },
        timeout=30.0,
    )
    job = result.get("job") or {}
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, Any] = {"state": result.get("state"), "job": job}
    while time.monotonic() < deadline:
        status = _json_request(
            client,
            "GET",
            "/search/index/status",
            headers={"X-Omnicode-Workspace": workspace_id},
            params={"workspace_id": workspace_id},
            timeout=10.0,
        )
        last_status = status
        state = status.get("state")
        if state == "completed":
            sync_status = _json_request(
                client,
                "GET",
                "/sync/status",
                params={"workspace_id": workspace_id},
                timeout=10.0,
            )
            return {
                "ok": sync_status.get("semantic_index_ready") is True
                and sync_status.get("recommended_query_mode") == "semantic_first",
                "job": status.get("job"),
                "semantic_index_ready": sync_status.get("semantic_index_ready"),
                "recommended_query_mode": sync_status.get("recommended_query_mode"),
                "semantic_index_coverage": sync_status.get("semantic_index_coverage"),
            }
        if state == "failed":
            break
        time.sleep(2)
    return {"ok": False, "last_status": last_status}


def run_benchmark(args: argparse.Namespace) -> BenchResult:
    repo = Path(args.repo).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    cloud_workspace = Path(args.cloud_workspace).expanduser().resolve()
    log_dir = Path(args.log_dir).expanduser().resolve()
    port = int(args.port or _free_port())
    base_url = f"http://127.0.0.1:{port}"
    result = BenchResult()

    if not repo.is_dir():
        raise FileNotFoundError(f"benchmark repo not found: {repo}")

    if args.reset_state:
        _safe_rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    process, stdout_path, stderr_path = _start_backend(
        port=port,
        state_dir=state_dir,
        cloud_workspace=cloud_workspace,
        log_dir=log_dir,
    )
    try:
        started = time.perf_counter()
        _wait_for_health(base_url, process)
        result.add("backend_health", True, started, backend_url=base_url)

        started = time.perf_counter()
        sync = _initial_sync(
            base_url=base_url,
            repo=repo,
            workspace_id=args.workspace_id,
            state_dir=state_dir,
            batch_max_files=args.batch_max_files,
            batch_max_bytes=args.batch_max_bytes,
        )
        result.add("initial_sync", not sync.get("errors"), started, **sync)

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            started = time.perf_counter()
            status = _json_request(
                client,
                "GET",
                "/sync/status",
                params={"workspace_id": args.workspace_id},
                timeout=10.0,
            )
            status_check = _assert_status_contract(
                status=status,
                min_files=args.min_files,
            )
            result.add(
                "status_contract",
                bool(status_check["ok"]),
                started,
                **_details_without_ok(status_check),
            )

            started = time.perf_counter()
            symbol_check = _assert_exact_symbol(
                client,
                workspace_id=args.workspace_id,
                symbol=args.symbol,
                expected_file=args.expected_file,
            )
            symbol_elapsed_ms = int((time.perf_counter() - started) * 1000)
            result.add(
                f"exact_symbol_{args.symbol}",
                bool(symbol_check["ok"])
                and symbol_elapsed_ms <= args.max_symbol_search_ms,
                started,
                max_elapsed_ms=args.max_symbol_search_ms,
                **_details_without_ok(symbol_check),
            )

            started = time.perf_counter()
            text_check = _assert_text_search(
                client,
                workspace_id=args.workspace_id,
                query=args.text_query,
                expected_file=args.expected_file,
                file_pattern=args.text_file_pattern,
            )
            text_elapsed_ms = int((time.perf_counter() - started) * 1000)
            result.add(
                f"exact_text_{args.symbol}",
                bool(text_check["ok"]) and text_elapsed_ms <= args.max_text_search_ms,
                started,
                max_elapsed_ms=args.max_text_search_ms,
                **_details_without_ok(text_check),
            )

            accepted = int(status.get("accepted_revision") or 0)
            started = time.perf_counter()
            semantic_rank = _assert_semantic_exact_rank(
                client,
                workspace_id=args.workspace_id,
                symbol=args.symbol,
                expected_file=args.expected_file,
            )
            semantic_rank_elapsed_ms = int((time.perf_counter() - started) * 1000)
            result.add(
                f"semantic_exact_rank_{args.symbol}",
                bool(semantic_rank["ok"])
                and semantic_rank_elapsed_ms <= args.max_semantic_rank_ms,
                started,
                max_elapsed_ms=args.max_semantic_rank_ms,
                **_details_without_ok(semantic_rank),
            )

            started = time.perf_counter()
            context_anchor = _assert_context_snapshot_anchor(
                client,
                workspace_id=args.workspace_id,
                revision=accepted,
                symbol=args.symbol,
                expected_file=args.expected_file,
            )
            context_elapsed_ms = int((time.perf_counter() - started) * 1000)
            result.add(
                f"context_snapshot_anchor_{args.symbol}",
                bool(context_anchor["ok"])
                and context_elapsed_ms <= args.max_context_ms,
                started,
                max_elapsed_ms=args.max_context_ms,
                **_details_without_ok(context_anchor),
            )

            started = time.perf_counter()
            semantic_stale = _assert_strict_semantic_stale(
                client,
                workspace_id=args.workspace_id,
                revision=accepted,
                query=args.symbol,
            )
            result.add(
                "strict_semantic_stale",
                bool(semantic_stale["ok"]),
                started,
                **_details_without_ok(semantic_stale),
            )

            if args.semantic_bootstrap:
                started = time.perf_counter()
                bootstrap = _run_semantic_bootstrap(
                    client,
                    workspace_id=args.workspace_id,
                    timeout_s=args.semantic_timeout_s,
                )
                result.add(
                    "semantic_bootstrap",
                    bool(bootstrap["ok"]),
                    started,
                    **_details_without_ok(bootstrap),
                )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
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
        description="Run clean-room large-repo hybrid benchmark."
    )
    parser.add_argument("--repo", default=str(DEFAULT_REPO))
    parser.add_argument("--workspace-id", default="django-cleanroom-bench")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    parser.add_argument("--cloud-workspace", default=str(DEFAULT_CLOUD_WORKSPACE))
    parser.add_argument("--log-dir", default=str(DEFAULT_STATE / "logs"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--min-files", type=int, default=6000)
    parser.add_argument("--symbol", default="BaseHandler")
    parser.add_argument(
        "--expected-file",
        default="django/core/handlers/base.py",
        help="Workspace-relative file expected to contain --symbol.",
    )
    parser.add_argument("--text-query", default="class BaseHandler:")
    parser.add_argument("--text-file-pattern", default="*.py")
    parser.add_argument("--batch-max-files", type=int, default=100)
    parser.add_argument("--batch-max-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-symbol-search-ms", type=int, default=1000)
    parser.add_argument("--max-text-search-ms", type=int, default=3000)
    parser.add_argument("--max-semantic-rank-ms", type=int, default=3000)
    parser.add_argument("--max-context-ms", type=int, default=10000)
    parser.add_argument("--semantic-bootstrap", action="store_true")
    parser.add_argument("--semantic-timeout-s", type=int, default=1800)
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    result = run_benchmark(args)
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
