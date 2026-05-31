"""Request-scoped workspace identity helpers.

The current runtime still has single-workspace service instances. These helpers
make workspace identity explicit and fail closed when a request targets a
workspace that is not the active backend root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class WorkspaceRequest:
    working_dir: str
    workspace_id: Optional[str]


class WorkspaceResolutionError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def resolve_workspace_request(
    workspace_id: Optional[str],
    *,
    working_dir: str,
    registry,
) -> WorkspaceRequest:
    """Resolve a logical workspace id against the active backend root."""
    if not workspace_id:
        active = registry.get_active()
        return WorkspaceRequest(working_dir=working_dir, workspace_id=active.id if active else None)

    requested = workspace_id.strip()
    ws = registry.get(requested)
    if ws is None:
        raise WorkspaceResolutionError(
            404,
            f"workspace_id not registered: {requested}",
        )

    active_root = Path(working_dir).resolve()
    target_root = Path(ws.path).resolve()
    if target_root != active_root:
        raise WorkspaceResolutionError(
            409,
            (
                f"workspace_id {requested!r} is registered at {target_root}, "
                f"but the active backend WORKING_DIR is {active_root}. "
                "Activate the workspace or start the backend with that workspace."
            ),
        )
    return WorkspaceRequest(working_dir=str(target_root), workspace_id=ws.id)


__all__ = [
    "WorkspaceRequest",
    "WorkspaceResolutionError",
    "resolve_workspace_request",
]
