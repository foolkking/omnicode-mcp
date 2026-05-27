"""omnicode serve — start the HTTP API server."""

import os
import sys


def run(headless: bool = False, host: str = "127.0.0.1", port: int = 6789, reload: bool = False):
    """Start the FastAPI server.

    In headless mode, the Web Console is disabled (API-only).
    In console mode (default), the full Web UI is served.
    """
    # Set feature flags before importing the app
    if headless:
        os.environ["OMNICODE_WEB_CONSOLE"] = "false"

    # Delegate to uvicorn with the existing main:app
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    print(f"""
================================================================================
 OmniCode-MCP {'(headless)' if headless else '(console)'}
   host    : {host}
   port    : {port}
   reload  : {reload}
   mode    : {'API only' if headless else 'API + Web Console'}
   URL     : http://{host}:{port}/
================================================================================
""")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
