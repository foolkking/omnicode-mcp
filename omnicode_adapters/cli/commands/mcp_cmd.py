"""omnicode mcp - start the MCP server."""

import os
import sys
import threading
from typing import Optional

_embedded_agent_thread: Optional[threading.Thread] = None


def _start_embedded_agent_if_configured(runtime, *, backend_token: Optional[str]) -> None:
    """Start the local sync watcher for hybrid MCP sessions when requested.

    The MCP stdio transport owns stdout, so the embedded agent must never print
    there. Status lines go to stderr and the thread is daemonized so MCP
    shutdown is not blocked by the watch loop.
    """
    global _embedded_agent_thread
    if _embedded_agent_thread and _embedded_agent_thread.is_alive():
        return

    try:
        from omnicode_core.workspace.agent_auto import decide_agent_auto

        decision = decide_agent_auto(runtime)
    except Exception as exc:
        print(f"[mcp-agent] auto-start skipped: {exc}", file=sys.stderr)
        return
    if not decision.should_start:
        return
    if not runtime.backend_url:
        print(
            "[mcp-agent] auto-start skipped: backend_url is not configured",
            file=sys.stderr,
        )
        return

    def _printer(message: str) -> None:
        print(f"[mcp-agent] {message}", file=sys.stderr, flush=True)

    def _run() -> None:
        try:
            from omnicode_adapters.agent.client import AgentClient
            from omnicode_adapters.agent.watcher import Watcher

            client = AgentClient(
                remote=runtime.backend_url,
                token=backend_token,
                workspace=runtime.workspace_root,
                workspace_id=runtime.workspace_id,
                max_file_bytes=runtime.max_file_bytes,
                batch_max_files=runtime.batch_max_files,
                batch_max_bytes=runtime.batch_max_bytes,
                excludes=runtime.ignore_paths,
            )
            watcher = Watcher(
                client=client,
                workspace=runtime.workspace_root,
                debounce_ms=runtime.debounce_ms,
                printer=_printer,
            )
            try:
                if decision.initial_sync:
                    watcher.initial_sync()
                watcher.run()
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            _printer(f"embedded agent stopped: {exc}")

    _embedded_agent_thread = threading.Thread(
        target=_run,
        name="omnicode-embedded-agent",
        daemon=True,
    )
    _embedded_agent_thread.start()
    print(
        "[mcp-agent] embedded watcher started "
        f"(workspace_id={runtime.workspace_id}, sync_mode={runtime.sync_mode})",
        file=sys.stderr,
    )


def run(
    *,
    transport: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    mount_path: Optional[str] = None,
    auth: str = "auto",
    backend_url: Optional[str] = None,
    backend_token: Optional[str] = None,
    workspace: Optional[str] = None,
    workspace_id: Optional[str] = None,
    executor: Optional[str] = None,
    sync_mode: Optional[str] = None,
    agent: Optional[str] = None,
    llm_mode: Optional[str] = None,
    embedding_mode: Optional[str] = None,
) -> None:
    """Launch the MCP server (mcp_server.py).

    This is what AI editors (Claude Desktop, Cursor, Kiro, Continue) spawn.
    The FastAPI backend must be running separately, either locally or remotely.
    """
    # Ensure offline mode for HuggingFace.
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # TOML config file (optional). See ``omnicode_core/config/toml_loader.py``.
    config_start = workspace or os.getcwd()
    try:
        from omnicode_core.config.toml_loader import load_toml_config

        load_toml_config(start=config_start)
    except Exception as exc:
        print(f"[mcp] TOML loader skipped: {exc}")

    try:
        from omnicode_core.config.runtime import build_runtime_config

        runtime = build_runtime_config(
            start=config_start,
            cli_overrides={
                "transport": transport,
                "backend_url": backend_url,
                "workspace": workspace,
                "workspace_id": workspace_id,
                "executor": executor,
                "sync_mode": sync_mode,
                "agent": agent,
                "llm_mode": llm_mode,
                "embedding_mode": embedding_mode,
            },
        )
        for name, value in runtime.as_env().items():
            os.environ[name] = value
        _start_embedded_agent_if_configured(
            runtime,
            backend_token=backend_token
            or os.environ.get("OMNICODE_FASTAPI_TOKEN")
            or os.environ.get("OMNICODE_BACKEND_TOKEN")
            or os.environ.get("OMNICODE_API_KEY")
            or os.environ.get("OMNICODE_AGENT_TOKEN"),
        )
    except Exception as exc:
        print(f"[mcp] runtime config failed: {exc}", file=sys.stderr)
        sys.exit(2)

    argv = ["--transport", runtime.transport, "--auth", auth]
    if host is not None:
        argv.extend(["--host", host])
    if port is not None:
        argv.extend(["--port", str(port)])
    if mount_path is not None:
        argv.extend(["--mount-path", mount_path])
    if runtime.backend_url is not None:
        argv.extend(["--backend-url", runtime.backend_url])
    if backend_token is not None:
        argv.extend(["--backend-token", backend_token])
    argv.extend(["--workspace", str(runtime.workspace_root)])
    argv.extend(["--workspace-id", runtime.workspace_id])
    argv.extend(["--executor", runtime.executor])

    # Import and run the existing mcp_server.
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    sys.path.insert(0, project_root)

    try:
        import mcp_server

        mcp_server.main(argv)
    except ImportError as e:
        print(f"ERROR: Cannot import mcp_server: {e}")
        print("Make sure you're running from the project root directory.")
        sys.exit(1)
