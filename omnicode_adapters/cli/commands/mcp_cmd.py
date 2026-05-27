"""omnicode mcp — start the MCP stdio server."""

import os
import sys


def run():
    """Launch the MCP stdio server (mcp_server.py).

    This is what AI editors (Claude Desktop, Cursor, Kiro, Continue) spawn.
    The FastAPI backend must be running separately on port 6789.
    """
    # Ensure offline mode for HuggingFace
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # Import and run the existing mcp_server
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, project_root)

    try:
        from mcp_server import mcp
        mcp.run()
    except ImportError as e:
        print(f"ERROR: Cannot import mcp_server: {e}")
        print("Make sure you're running from the project root directory.")
        sys.exit(1)
