"""Workspace path sandboxing primitives.

Design goals:
* O(1) — just a `Path.resolve()` + `is_relative_to` check.
* No special cases for symlinks: `resolve()` follows them, so a
  symlink that points outside the workspace is rejected.
* Cross-platform: works the same on Windows and POSIX.

Block list (rejected before any filesystem access):
* Absolute paths (the caller can pass repo-relative paths only).
* Any path that, after resolution, escapes the workspace root.
* Empty / whitespace-only inputs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union


class WorkspacePathError(ValueError):
    """Raised when a caller-supplied path escapes the workspace."""


def _normalise(path: Union[str, Path]) -> str:
    if path is None:
        raise WorkspacePathError("Path is required.")
    s = str(path).strip()
    if not s:
        raise WorkspacePathError("Path is empty.")
    return s


def ensure_within_workspace(
    user_path: Union[str, Path],
    working_dir: Union[str, Path],
) -> str:
    """Resolve ``user_path`` against ``working_dir`` and return the absolute path.

    Raises ``WorkspacePathError`` if the resolved path falls outside the
    workspace. Does **not** require the path to exist — the caller decides
    whether to require ``os.path.exists`` afterwards (e.g. read endpoints
    do, write endpoints might not).
    """
    raw = _normalise(user_path)
    root = Path(_normalise(working_dir)).resolve()

    # Reject obvious absolute paths so a clueless caller can't do
    # `file_path="/etc/passwd"` even on POSIX without a sandbox.
    p = Path(raw)
    if p.is_absolute():
        raise WorkspacePathError(
            f"Absolute paths are not allowed: {raw}"
        )

    candidate = (root / raw).resolve()

    # Python 3.9+ has Path.is_relative_to; we target 3.11.
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"Path escapes workspace: {raw} → {candidate}"
        ) from exc

    return str(candidate)


def safe_join(working_dir: Union[str, Path], *parts: str) -> str:
    """Convenience: like ``os.path.join`` but enforces the sandbox."""
    return ensure_within_workspace(os.path.join(*parts), working_dir)


__all__ = ["WorkspacePathError", "ensure_within_workspace", "safe_join"]
