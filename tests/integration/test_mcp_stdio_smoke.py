from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 45
    last_error = ""
    with httpx.Client(base_url=base_url, timeout=3.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"backend exited early with code {process.returncode}"
                )
            try:
                payload = client.get("/health").json()
                result = payload.get("result", payload)
                if result.get("status") == "healthy":
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(0.25)
    raise TimeoutError(f"backend did not become healthy: {last_error}")


def _tool_text(result: Any) -> str:
    return "".join(getattr(item, "text", "") for item in result.content)


@pytest.mark.anyio
async def test_mcp_stdio_lists_tools_and_runs_compact_status(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "OMNICODE_STATE_DIR": str(tmp_path / "state"),
            "OMNICODE_WORKSPACE_ROOT": str(repo_root),
            "OMNICODE_WORKSPACE_ID": "stdio-smoke",
            "OMNICODE_EXECUTOR_MODE": "local",
            "OMNICODE_AGENT_MODE": "off",
            "OMNICODE_SYNC_MODE": "off",
            "OMNICODE_LLM_MODE": "off",
            "OMNICODE_EMBEDDING_MODE": "off",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    params = StdioServerParameters(
        command=os.environ.get("PYTHON", os.sys.executable),
        args=[
            "-m",
            "omnicode_adapters.cli.main",
            "mcp",
            "--transport",
            "stdio",
            "--workspace",
            str(repo_root),
            "--workspace-id",
            "stdio-smoke",
            "--executor",
            "local",
            "--sync-mode",
            "off",
            "--agent",
            "off",
            "--llm-mode",
            "off",
            "--embedding-mode",
            "off",
        ],
        env=env,
        cwd=str(repo_root),
        encoding="utf-8",
        encoding_error_handler="replace",
    )

    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=10),
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "omni_status" in names
                assert "omni_read" in names
                assert "omni_patch" in names

                result = await session.call_tool(
                    "omni_status",
                    {"detail": "compact"},
                )
                text = "".join(
                    getattr(item, "text", "")
                    for item in result.content
                )
                payload = json.loads(text)
                assert payload["handler_version"]
                assert payload["detail"] == "compact"
                assert payload["contract_version"] == "status.v1"
                assert payload["workspace_id"] == "stdio-smoke"
                assert payload["executor_mode"] == "local"


@pytest.mark.anyio
async def test_mcp_stdio_read_patch_apply_rollback_round_trip(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    state_dir = tmp_path / "backend-state"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stdout_path = log_dir / "backend.stdout.log"
    stderr_path = log_dir / "backend.stderr.log"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "OMNICODE_STATE_DIR": str(state_dir),
            "OMNICODE_WORKSPACE_ROOT": str(repo_root),
            "OMNICODE_WORKSPACE_ID": "stdio-roundtrip",
            "OMNICODE_MODE": "local",
            "OMNICODE_READ_ONLY": "false",
            "OMNICODE_ALLOW_APPLY_PATCH": "true",
            "OMNICODE_LLM_MODE": "off",
            "OMNICODE_EMBEDDING_MODE": "off",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    stdout = stdout_path.open("w", encoding="utf-8", newline="\n")
    stderr = stderr_path.open("w", encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        [
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
            "local",
            "--state-dir",
            str(state_dir),
        ],
        cwd=str(repo_root),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    stdout.close()
    stderr.close()
    target = repo_root / "tests" / "tmp_mcp_stdio_smoke.py"
    try:
        _wait_for_health(base_url, process)
        params = StdioServerParameters(
            command=os.environ.get("PYTHON", sys.executable),
            args=[
                "-m",
                "omnicode_adapters.cli.main",
                "mcp",
                "--transport",
                "stdio",
                "--backend-url",
                base_url,
                "--workspace",
                str(repo_root),
                "--workspace-id",
                "stdio-roundtrip",
                "--executor",
                "local",
                "--sync-mode",
                "off",
                "--agent",
                "off",
                "--llm-mode",
                "off",
                "--embedding-mode",
                "off",
            ],
            env=env,
            cwd=str(repo_root),
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        content_before = "def add(a, b):\n    return a + b\n"
        content_after = (
            "def add(a, b):\n"
            "    \"\"\"Return the sum.\"\"\"\n"
            "    return a + b\n"
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=20),
            ) as session:
                await session.initialize()
                apply_new = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "apply",
                                "file": "tests/tmp_mcp_stdio_smoke.py",
                                "content": content_before,
                                "format": "json",
                            },
                        )
                    )
                )
                assert apply_new["ok"] is True
                assert apply_new["rollback_available"] is True
                assert apply_new["session_id"]

                read_result = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_read",
                            {
                                "file": "tests/tmp_mcp_stdio_smoke.py",
                                "mode": "full",
                                "format": "json",
                            },
                        )
                    )
                )
                assert read_result["ok"] is True
                assert "return a + b" in read_result["content"]

                preview = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "preview",
                                "file": "tests/tmp_mcp_stdio_smoke.py",
                                "content": content_after,
                                "format": "json",
                            },
                        )
                    )
                )
                assert preview["ok"] is True

                validate = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "validate",
                                "file": "tests/tmp_mcp_stdio_smoke.py",
                                "content": content_after,
                                "format": "json",
                            },
                        )
                    )
                )
                assert validate["ok"] is True

                apply_update = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "apply",
                                "file": "tests/tmp_mcp_stdio_smoke.py",
                                "content": content_after,
                                "format": "json",
                            },
                        )
                    )
                )
                assert apply_update["ok"] is True
                assert apply_update["rollback_available"] is True

                rollback_update = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "rollback",
                                "session_id": apply_update["session_id"],
                                "format": "json",
                            },
                        )
                    )
                )
                assert rollback_update["ok"] is True

                rollback_new = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "rollback",
                                "session_id": apply_new["session_id"],
                                "format": "json",
                            },
                        )
                    )
                )
                assert rollback_new["ok"] is True
                assert rollback_new.get("new_file_unlinked") is True
        assert not target.exists()
    finally:
        if target.exists():
            target.unlink()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.anyio
async def test_mcp_stdio_hybrid_cloud_down_keeps_local_read_patch(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "repo"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pkg" / "example.py").write_text(
        "def local_only():\n"
        "    return 'cloud-down-local'\n",
        encoding="utf-8",
    )

    unreachable_url = f"http://127.0.0.1:{_free_port()}"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "OMNICODE_STATE_DIR": str(tmp_path / "state"),
            "OMNICODE_WORKSPACE_ROOT": str(workspace),
            "OMNICODE_WORKSPACE_ID": "stdio-cloud-down",
            "OMNICODE_REMOTE": unreachable_url,
            "OMNICODE_BACKEND_URL": unreachable_url,
            "OMNICODE_EXECUTOR_MODE": "hybrid",
            "OMNICODE_AGENT_MODE": "off",
            "OMNICODE_LLM_MODE": "off",
            "OMNICODE_EMBEDDING_MODE": "off",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    params = StdioServerParameters(
        command=os.environ.get("PYTHON", sys.executable),
        args=[
            "-m",
            "omnicode_adapters.cli.main",
            "mcp",
            "--transport",
            "stdio",
            "--backend-url",
            unreachable_url,
            "--workspace",
            str(workspace),
            "--workspace-id",
            "stdio-cloud-down",
            "--executor",
            "hybrid",
            "--sync-mode",
            "smart",
            "--agent",
            "off",
            "--llm-mode",
            "off",
            "--embedding-mode",
            "off",
        ],
        env=env,
        cwd=str(repo_root),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    target = workspace / "tests" / "tmp_mcp_stdio_cloud_down.py"
    content = "VALUE = 'local-still-works'\n"

    try:
        with anyio.fail_after(45):
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=15),
                ) as session:
                    await session.initialize()

                    status = json.loads(
                        _tool_text(
                            await session.call_tool(
                                "omni_status",
                                {"detail": "compact"},
                            )
                        )
                    )
                    assert status["executor_mode"] == "hybrid"
                    assert status["sync"]["cloud_unavailable"] is True
                    assert status["capabilities"]["read.full"]["state"] == "ready"
                    assert status["capabilities"]["patch.safe_edit"]["state"] == "ready"

                    for mode_args in (
                        {"mode": "full"},
                        {"mode": "range", "start_line": 1, "end_line": 2},
                        {"mode": "outline"},
                    ):
                        payload = json.loads(
                            _tool_text(
                                await session.call_tool(
                                    "omni_read",
                                    {
                                        "file": "pkg/example.py",
                                        "format": "json",
                                        **mode_args,
                                    },
                                )
                            )
                        )
                        assert payload["ok"] is True
                        assert payload["source"] in {"local_file", "local_ast"}
                        assert payload["local_authority"] is True
                        rendered = json.dumps(payload, ensure_ascii=False)
                        if mode_args["mode"] == "outline":
                            assert "local_only" in rendered
                        else:
                            assert "cloud-down-local" in rendered

                    for tool_name, tool_args in (
                        (
                            "omni_search",
                            {
                                "query": "cloud-down-local",
                                "mode": "auto",
                                "format": "json",
                            },
                        ),
                        (
                            "omni_context",
                            {
                                "file": "pkg/example.py",
                                "format": "json",
                                "token_budget": 1000,
                            },
                        ),
                        (
                            "omni_impact",
                            {
                                "symbol": "local_only",
                                "format": "json",
                            },
                        ),
                    ):
                        payload = json.loads(
                            _tool_text(
                                await session.call_tool(tool_name, tool_args)
                            )
                        )
                        rendered = json.dumps(payload, ensure_ascii=False).lower()
                        assert "traceback" not in rendered
                        cloud_marked = (
                            payload.get("cloud_unavailable") is True
                            or payload.get("backend_unreachable") is True
                            or payload.get("error_code") == "CLOUD_UNAVAILABLE"
                            or "cloud" in str(payload.get("error", "")).lower()
                            or "unavailable" in str(payload.get("freshness", "")).lower()
                        )
                        degraded_success = (
                            payload.get("ok") is True
                            and (
                                payload.get("risk") == "unknown"
                                or payload.get("confidence") == "low"
                                or payload.get("freshness") in {
                                    "unavailable",
                                    "unknown",
                                }
                            )
                        )
                        assert cloud_marked or degraded_success, {
                            "tool": tool_name,
                            "payload": payload,
                        }
                        assert not (
                            payload.get("ok") is True
                            and payload.get("freshness") == "fresh"
                            and payload.get("confidence") in {
                                "high",
                                "medium",
                            }
                        ), payload

                    applied = json.loads(
                        _tool_text(
                            await session.call_tool(
                                "omni_patch",
                                {
                                    "action": "apply",
                                    "file": "tests/tmp_mcp_stdio_cloud_down.py",
                                    "content": content,
                                    "format": "json",
                                },
                            )
                        )
                    )
                    assert applied["ok"] is True
                    assert applied["source"] == "local"
                    assert applied["local_authority"] is True
                    sync_debug = {
                        key: applied.get(key)
                        for key in (
                            "source",
                            "local_authority",
                            "sync_pending",
                            "sync_pending_warning",
                            "sync_flushed",
                            "sync_flush_error",
                            "sync_flush_status_code",
                        )
                    }
                    assert applied["sync_pending"] is True, json.dumps(
                        sync_debug,
                        sort_keys=True,
                    )
                    assert applied["sync_flushed"] is False
                    assert applied["rollback_available"] is True
                    assert target.read_text(encoding="utf-8") == content

                    rolled_back = json.loads(
                        _tool_text(
                            await session.call_tool(
                                "omni_patch",
                                {
                                    "action": "rollback",
                                    "session_id": applied["session_id"],
                                    "format": "json",
                                },
                            )
                        )
                    )
                    assert rolled_back["ok"] is True
                    assert rolled_back["new_file_unlinked"] is True
        assert not target.exists()
    finally:
        if target.exists():
            target.unlink()


@pytest.mark.anyio
async def test_mcp_stdio_java_javac_and_scala_contracts(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "repo"
    (workspace / "src" / "main" / "java").mkdir(parents=True)
    (workspace / "src" / "main" / "scala").mkdir(parents=True)
    java_file = workspace / "src" / "main" / "java" / "App.java"
    scala_file = workspace / "src" / "main" / "scala" / "App.scala"
    java_file.write_text(
        "import missing.Dependency;\nclass App { Dependency dep; }\n",
        encoding="utf-8",
    )
    scala_file.write_text(
        "object App { def broken( = 1 }\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "OMNICODE_STATE_DIR": str(tmp_path / "state"),
            "OMNICODE_WORKSPACE_ROOT": str(workspace),
            "OMNICODE_WORKSPACE_ID": "stdio-java-scala",
            "OMNICODE_EXECUTOR_MODE": "local",
            "OMNICODE_AGENT_MODE": "off",
            "OMNICODE_SYNC_MODE": "off",
            "OMNICODE_LLM_MODE": "off",
            "OMNICODE_EMBEDDING_MODE": "off",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    params = StdioServerParameters(
        command=os.environ.get("PYTHON", sys.executable),
        args=[
            "-m",
            "omnicode_adapters.cli.main",
            "mcp",
            "--transport",
            "stdio",
            "--workspace",
            str(workspace),
            "--workspace-id",
            "stdio-java-scala",
            "--executor",
            "local",
            "--sync-mode",
            "off",
            "--agent",
            "off",
            "--llm-mode",
            "off",
            "--embedding-mode",
            "off",
        ],
        env=env,
        cwd=str(repo_root),
        encoding="utf-8",
        encoding_error_handler="replace",
    )

    with anyio.fail_after(35):
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=15),
            ) as session:
                await session.initialize()

                status = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_status",
                            {"detail": "compact"},
                        )
                    )
                )
                assert status["handler_version"]
                assert status["executor_mode"] == "local"

                java_diag = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_diagnostics",
                            {
                                "file": "src/main/java/App.java",
                                "format": "json",
                            },
                        )
                    )
                )
                assert java_diag["ok"] is True
                assert java_diag["language"] == "java"
                assert java_diag["diagnostics_status"] in {
                    "environment_incomplete",
                    "target_errors",
                    "partial",
                    "not_performed",
                }
                if java_diag["diagnostics_status"] == "target_errors":
                    assert java_diag["counts"]["error"] >= 1
                    assert any(
                        tool in java_diag["tools_run"]
                        for tool in ("javac", "tree_sitter_java", "jdtls")
                    )
                if java_diag["diagnostics_status"] == "environment_incomplete":
                    assert "javac" in java_diag["tools_run"]
                    assert "java_environment_incomplete" in java_diag["warnings"]
                    assert java_diag["counts"]["error"] >= 1

                java_validate = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "validate",
                                "file": "src/main/java/App.java",
                                "content": (
                                    "import missing.Dependency;\n"
                                    "class App { Dependency dep; }\n"
                                ),
                                "format": "json",
                            },
                        )
                    )
                )
                assert java_validate["ok"] is True
                assert java_validate["validation_passed"] in {None, True}
                if java_validate["validation_passed"] is None:
                    assert java_validate["validation"]["status"] == (
                        "environment_incomplete"
                    )
                    assert "javac" in java_validate["tools_run"]

                scala_validate = json.loads(
                    _tool_text(
                        await session.call_tool(
                            "omni_patch",
                            {
                                "action": "validate",
                                "file": "src/main/scala/App.scala",
                                "content": "object App { def broken( = 1 }\n",
                                "format": "json",
                            },
                        )
                    )
                )
                assert scala_validate["ok"] is True
                assert scala_validate["validation_passed"] is None
                assert scala_validate["validation"]["status"] == "not_performed"
                assert scala_validate["validation"]["reason"] in {
                    "scala_validation_unsupported",
                    "metals_unavailable",
                    "scala_toolchain_unavailable",
                }
                assert scala_validate["tools_skipped"]


@pytest.mark.anyio
async def test_mcp_stdio_hybrid_pending_drains_after_cloud_recovers(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    port = _free_port()
    backend_url = f"http://127.0.0.1:{port}"
    local_state = tmp_path / "local-state"
    cloud_state = tmp_path / "cloud-state"
    cloud_workspace = tmp_path / "cloud-workspace"
    (workspace / "tests").mkdir(parents=True)

    def _params() -> StdioServerParameters:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(repo_root),
                "OMNICODE_STATE_DIR": str(local_state),
                "OMNICODE_WORKSPACE_ROOT": str(workspace),
                "OMNICODE_WORKSPACE_ID": "stdio-drain",
                "OMNICODE_REMOTE": backend_url,
                "OMNICODE_BACKEND_URL": backend_url,
                "OMNICODE_EXECUTOR_MODE": "hybrid",
                "OMNICODE_AGENT_MODE": "off",
                "OMNICODE_LLM_MODE": "off",
                "OMNICODE_EMBEDDING_MODE": "off",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_OFFLINE": "1",
            }
        )
        return StdioServerParameters(
            command=os.environ.get("PYTHON", sys.executable),
            args=[
                "-m",
                "omnicode_adapters.cli.main",
                "mcp",
                "--transport",
                "stdio",
                "--backend-url",
                backend_url,
                "--workspace",
                str(workspace),
                "--workspace-id",
                "stdio-drain",
                "--executor",
                "hybrid",
                "--sync-mode",
                "smart",
                "--agent",
                "off",
                "--llm-mode",
                "off",
                "--embedding-mode",
                "off",
            ],
            env=env,
            cwd=str(repo_root),
            encoding="utf-8",
            encoding_error_handler="replace",
        )

    first = workspace / "tests" / "tmp_mcp_stdio_drain_a.py"
    second = workspace / "tests" / "tmp_mcp_stdio_drain_b.py"
    try:
        with anyio.fail_after(35):
            async with stdio_client(_params()) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=15),
                ) as session:
                    await session.initialize()
                    applied = json.loads(
                        _tool_text(
                            await session.call_tool(
                                "omni_patch",
                                {
                                    "action": "apply",
                                    "file": "tests/tmp_mcp_stdio_drain_a.py",
                                    "content": "VALUE = 'queued-before-cloud'\n",
                                    "format": "json",
                                },
                            )
                        )
                    )
                    assert applied["ok"] is True
                    sync_debug = {
                        key: applied.get(key)
                        for key in (
                            "source",
                            "local_authority",
                            "sync_pending",
                            "sync_pending_warning",
                            "sync_flushed",
                            "sync_flush_error",
                            "sync_flush_status_code",
                        )
                    }
                    assert applied["sync_pending"] is True, json.dumps(
                        sync_debug,
                        sort_keys=True,
                    )
                    assert applied["sync_flushed"] is False
                    assert applied["rollback_available"] is True

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        stdout_path = log_dir / "cloud.stdout.log"
        stderr_path = log_dir / "cloud.stderr.log"
        cloud_env = os.environ.copy()
        cloud_env.update(
            {
                "PYTHONPATH": str(repo_root),
                "OMNICODE_STATE_DIR": str(cloud_state),
                "OMNICODE_WORKSPACE_REGISTRY": str(cloud_state / "workspaces.json"),
                "OMNICODE_LLM_MODE": "off",
                "OMNICODE_EMBEDDING_MODE": "off",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_OFFLINE": "1",
            }
        )
        cloud_workspace.mkdir(parents=True)
        stdout = stdout_path.open("w", encoding="utf-8", newline="\n")
        stderr = stderr_path.open("w", encoding="utf-8", newline="\n")
        process = subprocess.Popen(
            [
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
                str(cloud_state),
            ],
            cwd=str(cloud_workspace),
            env=cloud_env,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        stdout.close()
        stderr.close()
        try:
            _wait_for_health(backend_url, process)
            with anyio.fail_after(45):
                async with stdio_client(_params()) as (read, write):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=20),
                    ) as session:
                        await session.initialize()
                        status_before = json.loads(
                            _tool_text(
                                await session.call_tool(
                                    "omni_status",
                                    {"detail": "compact"},
                                )
                            )
                        )
                        assert status_before["sync"]["cloud_unavailable"] is False
                        assert status_before["sync"]["pending_count"] >= 1

                        drained = json.loads(
                            _tool_text(
                                await session.call_tool(
                                    "omni_patch",
                                    {
                                        "action": "apply",
                                        "file": "tests/tmp_mcp_stdio_drain_b.py",
                                        "content": "VALUE = 'drain-trigger'\n",
                                        "format": "json",
                                    },
                                )
                            )
                        )
                        assert drained["ok"] is True
                        assert drained["sync_flushed"] is True

                        status_after = json.loads(
                            _tool_text(
                                await session.call_tool(
                                    "omni_status",
                                    {"detail": "compact"},
                                )
                            )
                        )
                        assert status_after["sync"]["cloud_unavailable"] is False
                        assert status_after["sync"]["pending_count"] == 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    finally:
        for path in (first, second):
            if path.exists():
                path.unlink()
