"""omnicode serve — start the HTTP API server."""

import os
import sys

# Mode → environment-variable overlay. Set BEFORE main:app imports
# Pydantic settings so the new defaults take effect.
_MODE_PRESETS: dict[str, dict[str, str]] = {
    "local": {
        "OMNICODE_MODE": "local",
        "OMNICODE_READ_ONLY": "false",
        "OMNICODE_ALLOW_APPLY_PATCH": "true",
    },
    "cloud": {
        "OMNICODE_MODE": "cloud",
        # Cloud is shared/multi-user by default → don't write to disk
        # without an explicit opt-in via the configured editor role
        # (RBAC) AND OMNICODE_ALLOW_APPLY_PATCH=true.
        "OMNICODE_READ_ONLY": "true",
        "OMNICODE_ALLOW_APPLY_PATCH": "false",
    },
    "hybrid": {
        # Cloud index + local apply. The remote process accepts writes
        # ONLY through the agent endpoints (the local agent is the
        # source of truth for the codebase) and blocks /patch/apply on
        # the wire — the editor applies patches locally instead.
        "OMNICODE_MODE": "hybrid",
        "OMNICODE_READ_ONLY": "false",
        "OMNICODE_ALLOW_APPLY_PATCH": "false",
    },
}


def _apply_mode_preset(mode: str) -> None:
    preset = _MODE_PRESETS.get(mode, _MODE_PRESETS["local"])
    for key, value in preset.items():
        # User-set env vars win — presets are only a *default*.
        os.environ.setdefault(key, value)


def run(
    headless: bool = False,
    host: str = "127.0.0.1",
    port: int = 6789,
    reload: bool = False,
    mode: str = "local",
):
    """Start the FastAPI server.

    Parameters
    ----------
    headless: True hides the Web Console (API-only).
    mode:     'local' | 'cloud' | 'hybrid'. Drives default
              ``OMNICODE_READ_ONLY`` / ``OMNICODE_ALLOW_APPLY_PATCH``
              presets, see ``_MODE_PRESETS``.
    """
    if headless:
        os.environ["OMNICODE_WEB_CONSOLE"] = "false"

    _apply_mode_preset(mode)

    # TOML config file (optional). Applied AFTER mode preset so the
    # preset acts as a baseline and the TOML can override individual
    # keys; explicit env vars still win because the loader uses
    # ``setdefault``.
    try:
        from omnicode_core.config.toml_loader import load_toml_config

        load_toml_config(start=os.getcwd())
    except Exception as exc:
        # Never block startup over a config-file issue.
        print(f"[serve] TOML loader skipped: {exc}")

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    read_only = os.environ.get("OMNICODE_READ_ONLY", "false").lower() == "true"
    allow_apply = os.environ.get("OMNICODE_ALLOW_APPLY_PATCH", "true").lower() == "true"

    print(f"""
================================================================================
 OmniCode-MCP {'(headless)' if headless else '(console)'}
   host         : {host}
   port         : {port}
   reload       : {reload}
   ui           : {'API only' if headless else 'API + Web Console'}
   deploy mode  : {mode}
   read-only    : {'yes' if read_only else 'no'}
   apply patch  : {'enabled' if allow_apply else 'BLOCKED'}
   URL          : http://{host}:{port}/
================================================================================
""")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
