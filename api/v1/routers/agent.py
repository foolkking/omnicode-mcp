"""Local-agent file-sync endpoints (Wave 2, W2-2 — hybrid mode glue).

In hybrid mode the *real* working tree lives on the user's machine.
A lightweight ``omnicode agent`` watcher pushes changed file bodies to
this remote OmniCode server so the heavy work — embedding, FAISS,
call-graph, memory advisory — can happen on a beefier box without
ever needing direct filesystem access to the project.

Endpoints:

* ``POST   /index/upsert-file`` — agent uploads a single file body.
* ``POST   /index/upsert-batch`` — same but for a batch (debounced
  bursts during edits).
* ``DELETE /index/file``        — agent reports a file deletion.
* ``GET    /index/sync-status`` — agent introspects what the server
  thinks is the current head so it can decide whether a re-push is
  necessary after a restart.
* ``GET    /index/stats``       — same as the existing /search index
  stats, mounted under /index for symmetry.

Path sandbox + read-only middleware still apply:
* The path is validated through :func:`utils.validate_file_path`.
* In ``OMNICODE_READ_ONLY=true`` the read-only middleware blocks every
  write here. That means cloud-mode deployments must explicitly opt-in
  for agent sync — flip ``OMNICODE_READ_ONLY=false`` for the index
  partition only is not yet supported (intentionally; W2-2 is the
  *full* hybrid story, not partial).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core import get_search_engine
from core.config import get_settings
from omnicode_core.workspace import get_workspace_registry
from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from utils import (
    create_error_response,
    create_success_response,
    validate_file_path,
)

router = APIRouter(prefix="/index", tags=["agent"])


def _resolve_agent_workspace(
    workspace_id: Optional[str],
) -> tuple[str, Optional[str]]:
    """Resolve the workspace targeted by an agent request.

    The current service layer is still single-workspace. When a caller sends
    ``X-Omnicode-Workspace`` we require that id to map to the active
    ``WORKING_DIR``; otherwise we fail fast instead of indexing into the wrong
    project.
    """
    try:
        resolved = resolve_workspace_request(
            workspace_id,
            working_dir=get_settings().WORKING_DIR,
            registry=get_workspace_registry(),
        )
    except WorkspaceResolutionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
    return resolved.working_dir, resolved.workspace_id


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class _UpsertBody(BaseModel):
    """Single-file upload from the agent."""

    file_path: str = Field(..., description="Repo-relative path")
    content: str = Field(..., description="Full file body as UTF-8 text")
    content_hash: Optional[str] = Field(
        default=None,
        description="Optional SHA-256 hash; the server may use it for "
        "no-op detection in a future iteration.",
    )


class _UpsertBatchBody(BaseModel):
    files: List[_UpsertBody]


class _DeleteBody(BaseModel):
    file_path: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/upsert-file")
async def upsert_file(
    body: _UpsertBody,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Index a single file's body coming from the local agent.

    Returns the chunk count so the agent can summarise progress.
    """
    try:
        working_dir, workspace_id = _resolve_agent_workspace(x_omnicode_workspace)
        await validate_file_path(body.file_path, working_dir)
    except HTTPException as exc:
        return create_error_response(str(exc.detail), exc.status_code)

    engine = get_search_engine()
    if engine is None:
        return create_error_response("Search engine not initialized", 500)
    chunks = await engine.upsert_content(body.file_path, body.content)
    return create_success_response(
        {
            "file_path": body.file_path,
            "chunks_indexed": chunks,
            "workspace_id": workspace_id,
        }
    )


@router.post("/upsert-batch")
async def upsert_batch(
    body: _UpsertBatchBody,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Index a batch of files — used by the agent's debounce buffer."""
    try:
        working_dir, workspace_id = _resolve_agent_workspace(x_omnicode_workspace)
    except HTTPException as exc:
        return create_error_response(str(exc.detail), exc.status_code)

    engine = get_search_engine()
    if engine is None:
        return create_error_response("Search engine not initialized", 500)

    results = []
    errors: list[dict[str, str]] = []
    for entry in body.files:
        try:
            await validate_file_path(entry.file_path, working_dir)
            chunks = await engine.upsert_content(entry.file_path, entry.content)
            results.append({"file_path": entry.file_path, "chunks_indexed": chunks})
        except HTTPException as exc:
            errors.append({"file_path": entry.file_path, "error": str(exc.detail)})
        except Exception as exc:
            errors.append({"file_path": entry.file_path, "error": str(exc)})

    return create_success_response(
        {
            "indexed": results,
            "errors": errors,
            "total_indexed": len(results),
            "total_errors": len(errors),
            "workspace_id": workspace_id,
        }
    )


@router.delete("/file")
async def delete_file_from_index(
    body: _DeleteBody,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Drop a file from the index — used when the agent observes a
    deletion."""
    try:
        working_dir, workspace_id = _resolve_agent_workspace(x_omnicode_workspace)
        await validate_file_path(body.file_path, working_dir)
    except HTTPException as exc:
        return create_error_response(str(exc.detail), exc.status_code)

    engine = get_search_engine()
    if engine is None:
        return create_error_response("Search engine not initialized", 500)
    removed = await engine.delete_file_index(body.file_path)
    return create_success_response(
        {"file_path": body.file_path, "removed": removed, "workspace_id": workspace_id}
    )


@router.get("/sync-status")
async def sync_status(
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Report what the server currently has indexed.

    The agent calls this on startup to decide whether a full rescan +
    push is needed (e.g. after a long disconnect or a server-side
    rebuild).
    """
    try:
        working_dir, workspace_id = _resolve_agent_workspace(x_omnicode_workspace)
    except HTTPException as exc:
        return create_error_response(str(exc.detail), exc.status_code)

    engine = get_search_engine()
    if engine is None:
        return create_error_response("Search engine not initialized", 500)
    stats = engine.get_stats() or {}
    return create_success_response(
        {
            "indexed_files": stats.get("total_files", 0),
            "indexed_chunks": stats.get("total_chunks", 0),
            "embedding_model": getattr(
                engine.embedding_model, "name", "unknown"
            ),
            "working_dir": working_dir,
            "workspace_id": workspace_id,
        }
    )


@router.get("/stats")
async def index_stats(
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Same shape as `/sync-status` — kept as a stable name for the
    Web Console."""
    return await sync_status(x_omnicode_workspace)


__all__ = ["router"]
