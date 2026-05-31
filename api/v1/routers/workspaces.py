"""Workspace registry REST endpoints (P2 — multi-workspace).

Backs the user-level bookmark store at
``~/.kiro/codebase-mcp/workspaces.json``. Switching the *active*
workspace via ``PUT /workspaces/{id}/activate`` also updates the
in-process ``settings.WORKING_DIR`` so subsequent search / read /
git calls see the new project.

Endpoints:

* ``GET    /workspaces``                 — list all bookmarks
* ``POST   /workspaces``                 — register a new bookmark
* ``DELETE /workspaces/{id}``            — remove a bookmark
* ``GET    /workspaces/active``          — return the active one
* ``PUT    /workspaces/{id}/activate``   — make this one active + reload
* ``PUT    /workspaces/{id}/rename``     — change display name
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from omnicode.config.settings import get_settings
from omnicode_core.workspace import get_workspace_registry

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _ok(payload):
    return {"result": payload, "success": True}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class _AddBody(BaseModel):
    workspace_id: str | None = Field(
        default=None,
        description="Optional stable id shared by MCP/agent/cloud clients",
    )
    name: str = Field(..., description="Display name shown in UIs")
    path: str = Field(..., description="Absolute path on the host")
    set_active: bool = Field(default=False)


class _RenameBody(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("")
async def list_workspaces():
    items = [w.to_dict() for w in get_workspace_registry().list()]
    return _ok({"workspaces": items, "total": len(items)})


@router.get("/active")
async def get_active_workspace():
    active = get_workspace_registry().get_active()
    return _ok({"workspace": active.to_dict() if active else None})


@router.post("")
async def add_workspace(body: _AddBody):
    try:
        ws = get_workspace_registry().add(
            name=body.name,
            path=body.path,
            set_active=body.set_active,
            workspace_id=body.workspace_id,
        )
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=f"Not a directory: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.set_active:
        get_settings().update_working_directory(ws.path)
    return _ok({"workspace": ws.to_dict()})


@router.delete("/{workspace_id}")
async def remove_workspace(workspace_id: str):
    registry = get_workspace_registry()
    target = registry.get(workspace_id)
    ok = registry.remove(workspace_id)
    if not ok:
        raise HTTPException(status_code=404, detail="workspace not found")

    # Drop the per-workspace FAISS shard so the disk footprint matches
    # the registry. Best-effort — log but don't fail the API call.
    if target is not None:
        try:
            from omnicode_core.index.sharding import drop_shard

            drop_shard(target.path, workspace_id)
        except ValueError:
            # drop_shard refuses to drop the default shard; that's
            # fine — only named shards represent registered workspaces.
            pass
        except Exception as exc:
            # Disk error / shard already gone — note it but don't
            # roll back the registry change.
            print(f"[workspaces] shard cleanup skipped: {exc}")
    return _ok({"removed": workspace_id})


@router.put("/{workspace_id}/activate")
async def activate_workspace(workspace_id: str):
    ws = get_workspace_registry().set_active(workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    # Best-effort: update WORKING_DIR so downstream services see it.
    # Heavy services (FAISS index, etc.) require a server restart to fully
    # re-init — the response carries `requires_restart=True` to signal that.
    try:
        get_settings().update_working_directory(ws.path)
        requires_restart = True
    except Exception:
        requires_restart = True
    return _ok({"workspace": ws.to_dict(), "requires_restart": requires_restart})


@router.put("/{workspace_id}/rename")
async def rename_workspace(workspace_id: str, body: _RenameBody):
    ws = get_workspace_registry().rename(workspace_id, body.name)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return _ok({"workspace": ws.to_dict()})


__all__ = ["router"]
