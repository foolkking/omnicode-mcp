"""
Patch operations API — preview, validate, apply, rollback, sessions.

These endpoints provide the safe-edit layer that does NOT require an LLM.
External AI editors generate patches; OmniCode validates and applies them.
"""


from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.config import get_settings
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/patch", tags=["patch"])


class PatchRequest(BaseModel):
    """Request body for patch operations."""
    file_path: str = Field(..., description="Relative path to the file")
    content: str = Field(..., description="New file content (full replacement)")
    source: str = Field(default="external", description="Who generated this patch")
    metadata: dict = Field(default_factory=dict, description="Extra metadata")


@router.post("/preview")
async def preview_patch(request: PatchRequest):
    """Preview what a patch would change (unified diff).

    Does NOT modify the file. Use this to show the user what will happen.
    """
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    pm = PatchManager(settings.WORKING_DIR)
    result = pm.preview_patch(request.file_path, request.content)

    return create_success_response({
        "success": result.success,
        "message": result.message,
        "diff": result.diff,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "file_path": result.file_path,
    })


@router.post("/validate")
async def validate_patch(request: PatchRequest):
    """Validate a patch by running static analysis on the result.

    Writes to a temp file, runs ruff/eslint, then deletes the temp.
    Does NOT modify the original file.
    """
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    pm = PatchManager(settings.WORKING_DIR)
    result = await pm.validate_patch(request.file_path, request.content)

    return create_success_response({
        "success": result.success,
        "message": result.message,
        "diagnostics": result.diagnostics,
        "file_path": result.file_path,
    })


@router.post("/apply")
async def apply_patch(request: PatchRequest):
    """Apply a patch to a file with snapshot backup.

    Creates a snapshot before overwriting so rollback is always possible.
    Returns a session_id for tracking and rollback.

    Gated by ``OMNICODE_READ_ONLY`` and ``OMNICODE_ALLOW_APPLY_PATCH`` —
    cloud deployments typically turn ``allow_apply_patch`` off so remote
    callers can only preview/validate/explain.
    """
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    if settings.OMNICODE_READ_ONLY:
        return create_error_response(
            "Server is in read-only mode (OMNICODE_READ_ONLY=true).",
            status_code=403,
        )
    if not settings.OMNICODE_ALLOW_APPLY_PATCH:
        return create_error_response(
            "Patch apply is disabled on this deployment "
            "(OMNICODE_ALLOW_APPLY_PATCH=false). Use /patch/preview + "
            "/patch/validate + /patch/explain to inspect changes; apply "
            "them with the editor or a local tool.",
            status_code=403,
        )

    pm = PatchManager(settings.WORKING_DIR)
    result = pm.apply_patch(
        request.file_path,
        request.content,
        source=request.source,
        metadata=request.metadata,
    )

    return create_success_response({
        "success": result.success,
        "message": result.message,
        "session_id": result.session_id,
        "diff": result.diff,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "file_path": result.file_path,
        "rollback_available": result.rollback_available,
    })


@router.post("/rollback")
async def rollback_patch(
    session_id: str = Query(..., description="Session ID to rollback"),
):
    """Rollback a previously applied patch using its snapshot.

    Restores the file to its pre-edit state. Gated by the same
    read-only / allow-apply flags as ``/patch/apply``.
    """
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    if settings.OMNICODE_READ_ONLY:
        return create_error_response(
            "Server is in read-only mode (OMNICODE_READ_ONLY=true).",
            status_code=403,
        )
    if not settings.OMNICODE_ALLOW_APPLY_PATCH:
        return create_error_response(
            "Rollback is disabled on this deployment "
            "(OMNICODE_ALLOW_APPLY_PATCH=false).",
            status_code=403,
        )

    pm = PatchManager(settings.WORKING_DIR)
    result = pm.rollback_patch(session_id)

    return create_success_response({
        "success": result.success,
        "message": result.message,
        "session_id": result.session_id,
        "file_path": result.file_path,
    })


@router.post("/explain")
async def explain_patch(request: PatchRequest):
    """Generate a human-readable explanation of what a patch does."""
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    pm = PatchManager(settings.WORKING_DIR)
    result = pm.explain_patch(request.file_path, request.content)

    return create_success_response({
        "success": result.success,
        "message": result.message,
        "diff": result.diff,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "file_path": result.file_path,
    })


@router.get("/sessions")
async def list_edit_sessions(
    limit: int = Query(20, description="Max sessions to return"),
):
    """List recent edit sessions."""
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    pm = PatchManager(settings.WORKING_DIR)
    sessions = pm.list_sessions(limit=limit)

    return create_success_response({
        "sessions": sessions,
        "total": len(sessions),
    })


@router.get("/sessions/{session_id}")
async def get_edit_session(session_id: str):
    """Get full details of a specific edit session (including diff)."""
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    pm = PatchManager(settings.WORKING_DIR)
    session = pm.get_session(session_id)

    if not session:
        return create_error_response(f"Session not found: {session_id}", 404)

    return create_success_response(session)
