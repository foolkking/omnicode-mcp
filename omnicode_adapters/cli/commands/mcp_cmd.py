"""omnicode mcp - start the MCP server."""

import os
import sys
from typing import Optional


def run(
    *,
    transport: str = "stdio",
    host: Optional[str] = None,
    port: Optional[int] = None,
    mount_path: Optional[str] = None,
    auth: str = "auto",
    backend_url: Optional[str] = None,
    backend_token: Optional[str] = None,
    workspace: Optional[str] = None,
    workspace_id: Optional[str] = None,
    executor: str = "remote",
) -> None:
    """Launch the MCP server (mcp_server.py).

    This is what AI editors (Claude Desktop, Cursor, Kiro, Continue) spawn.
    The FastAPI backend must be running separately, either locally or remotely.
    """
    # Ensure offline mode for HuggingFace.
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # TOML config file (optional). See ``omnicode_core/config/toml_loader.py``.
    try:
        from omnicode_core.config.toml_loader import load_toml_config

        load_toml_config(start=os.getcwd())
    except Exception as exc:
        print(f"[mcp] TOML loader skipped: {exc}")

    argv = ["--transport", transport, "--auth", auth]
    if host is not None:
        argv.extend(["--host", host])
    if port is not None:
        argv.extend(["--port", str(port)])
    if mount_path is not None:
        argv.extend(["--mount-path", mount_path])
    if backend_url is not None:
        argv.extend(["--backend-url", backend_url])
    if backend_token is not None:
        argv.extend(["--backend-token", backend_token])
    if workspace is not None:
        argv.extend(["--workspace", workspace])
    if workspace_id is not None:
        argv.extend(["--workspace-id", workspace_id])
    if executor:
        argv.extend(["--executor", executor])

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
