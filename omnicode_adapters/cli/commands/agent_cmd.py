"""omnicode agent — local-side file-sync agent (Wave 2, W2-2)."""

from __future__ import annotations

import os
import sys
from typing import Optional


def run(
    *,
    remote: str,
    token: Optional[str] = None,
    workspace: str = ".",
    workspace_id: Optional[str] = None,
    initial_sync: bool = True,
    debounce_ms: int = 800,
    exclude: tuple[str, ...] = (),
) -> None:
    """Watch ``workspace`` and push changes to ``remote``."""
    # TOML config support — the agent honours [agent] section if present.
    try:
        from omnicode_core.config.toml_loader import load_toml_config

        load_toml_config(start=os.getcwd())
    except Exception as exc:
        print(f"[agent] TOML loader skipped: {exc}", file=sys.stderr)

    if not token:
        token = os.environ.get("OMNICODE_API_KEY", "") or os.environ.get(
            "OMNICODE_AGENT_TOKEN", ""
        )

    if not remote:
        remote = os.environ.get("OMNICODE_REMOTE", "")

    if not remote:
        print(
            "ERROR: --remote URL is required (or set OMNICODE_REMOTE).",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        from omnicode_adapters.agent import run_agent

        run_agent(
            remote=remote,
            token=token,
            workspace=workspace,
            workspace_id=workspace_id,
            initial_sync=initial_sync,
            excludes=exclude,
            debounce_ms=debounce_ms,
        )
    except KeyboardInterrupt:
        print("\n[agent] stopping…")
    except Exception as exc:
        print(f"[agent] fatal: {exc}", file=sys.stderr)
        sys.exit(1)
