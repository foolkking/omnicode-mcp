"""Utility functions for path validation and file operations.

Delegates to ``omnicode_core.security.sandbox`` so every endpoint and
the bare ``validate_file_path`` helper share the same sandbox semantics.
"""

import os
from pathlib import Path

from fastapi import HTTPException, status

from omnicode_core.security import WorkspacePathError, ensure_within_workspace


async def validate_file_path(file_path: str, working_dir: str) -> Path:
    """Validate and resolve a caller-supplied file path.

    Behaviour:
    * Repo-relative paths are joined to ``working_dir`` and resolved.
    * Absolute paths are accepted **only** when they already resolve
      inside the workspace — otherwise rejected with 403.
    * Symlinks that point outside the workspace are rejected because
      ``Path.resolve()`` follows them.

    Returns the resolved ``Path``. Raises ``HTTPException(403)`` for any
    sandbox violation, ``HTTPException(400)`` for malformed input.
    """
    try:
        if os.path.isabs(file_path):
            # Allow absolute paths only if they're inside the workspace
            # already (e.g. tools that report absolute paths back).
            abs_path = Path(file_path).resolve()
            workspace = Path(working_dir).resolve()
            try:
                abs_path.relative_to(workspace)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: path outside working directory",
                ) from exc
            return abs_path

        # Repo-relative path → enforce sandbox.
        resolved = ensure_within_workspace(file_path, working_dir)
        return Path(resolved)
    except HTTPException:
        raise
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file path: {exc}",
        ) from exc
