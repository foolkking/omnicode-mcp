"""Workspace path sandboxing — prevent directory traversal attacks.

Cloud / shared deployments may expose the HTTP API to untrusted callers.
Without a sandbox, a request like ``file_path=../../../etc/passwd`` would
resolve to a real path and the read endpoint would happily serve it.

Use ``ensure_within_workspace(path, working_dir)`` at the top of every
endpoint that accepts a user-supplied path. Returns the resolved
absolute path on success, raises ``WorkspacePathError`` otherwise.
"""

from omnicode_core.security.sandbox import (
    WorkspacePathError,
    ensure_within_workspace,
    safe_join,
)

__all__ = ["WorkspacePathError", "ensure_within_workspace", "safe_join"]
