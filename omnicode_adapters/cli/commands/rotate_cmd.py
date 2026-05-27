"""omnicode rotate-master-key — rotate the provider DB encryption key.

See ``omnicode_core/auth/rotation.py`` for the algorithm. This module
just resolves paths, prints a summary, and exits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional


def run(*, db_path: Optional[str] = None, key_path: Optional[str] = None,
        new_key: Optional[str] = None) -> None:
    from omnicode_core.auth.rotation import rotate_master_key

    # Default: shared user-level provider store.
    home_root = Path.home() / ".kiro" / "codebase-mcp"
    if db_path is None:
        db_path = str(home_root / "providers.db")
    if key_path is None:
        key_path = str(home_root / "providers.key")

    new_bytes = new_key.encode("utf-8") if new_key else None

    try:
        report = rotate_master_key(
            db_path=db_path,
            key_path=key_path,
            new_key_bytes=new_bytes,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(report.to_dict(), indent=2))
    print(
        "\nThe old key was backed up at "
        f"{report.backup_key_path} — keep it until you've confirmed the "
        "rotation worked, then delete it."
    )
