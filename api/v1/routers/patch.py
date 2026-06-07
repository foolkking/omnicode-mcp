"""
Patch operations API — preview, validate, apply, rollback, sessions.

These endpoints provide the safe-edit layer that does NOT require an LLM.
External AI editors generate patches; OmniCode validates and applies them.
"""


from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from core.config import get_settings
from omnicode_core.observability import (
    IdempotencyConflict,
    get_audit_log,
    get_idempotency_store,
    get_metrics_registry,
)
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
async def apply_patch(
    request: PatchRequest,
    http_request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    """Apply a patch to a file with snapshot backup.

    Creates a snapshot before overwriting so rollback is always possible.
    Returns a session_id for tracking and rollback.

    Gated by ``OMNICODE_READ_ONLY`` and ``OMNICODE_ALLOW_APPLY_PATCH`` —
    cloud deployments typically turn ``allow_apply_patch`` off so remote
    callers can only preview/validate/explain.

    Idempotency: pass an ``Idempotency-Key`` header (any stable string,
    a UUID is recommended). The same key + same payload returns the
    cached response without a second write. Same key + different
    payload returns 409.
    """
    from omnicode_core.edit.patch import PatchManager

    settings = get_settings()
    metrics = get_metrics_registry()
    audit = get_audit_log()
    actor_ip = (http_request.client.host if http_request.client else "") or ""

    if settings.OMNICODE_READ_ONLY:
        metrics.inc("patch_apply_total", labels={"outcome": "denied_read_only"})
        audit.emit(
            actor="anonymous", action="POST /patch/apply",
            target=request.file_path, ip=actor_ip,
            outcome="denied", extra="read-only mode",
        )
        return create_error_response(
            "Server is in read-only mode (OMNICODE_READ_ONLY=true).",
            status_code=403,
        )
    if not settings.OMNICODE_ALLOW_APPLY_PATCH:
        metrics.inc("patch_apply_total", labels={"outcome": "denied_apply_off"})
        audit.emit(
            actor="anonymous", action="POST /patch/apply",
            target=request.file_path, ip=actor_ip,
            outcome="denied", extra="allow_apply_patch=false",
        )
        return create_error_response(
            "Patch apply is disabled on this deployment "
            "(OMNICODE_ALLOW_APPLY_PATCH=false). Use /patch/preview + "
            "/patch/validate + /patch/explain to inspect changes; apply "
            "them with the editor or a local tool.",
            status_code=403,
        )

    # ---- Idempotency check (P2 — 1.1 polish) ----------------------------
    idem_store = get_idempotency_store(settings.WORKING_DIR)
    idem_payload = {
        "file_path": request.file_path,
        "content": request.content,
        "source": request.source,
    }
    if idempotency_key:
        try:
            cached = idem_store.lookup(idempotency_key, idem_payload)
        except IdempotencyConflict as exc:
            metrics.inc("patch_apply_total", labels={"outcome": "idem_conflict"})
            return create_error_response(
                f"Idempotency conflict: {exc}", status_code=409,
            )
        if cached is not None:
            metrics.inc("patch_apply_total", labels={"outcome": "idem_replay"})
            audit.emit(
                actor="anonymous", action="POST /patch/apply",
                target=request.file_path, ip=actor_ip,
                outcome="ok", extra=f"idem_replay key={idempotency_key[:24]}",
            )
            # Cached value is the inner payload dict; wrap it back.
            return create_success_response(cached)

    pm = PatchManager(settings.WORKING_DIR)
    with metrics.timer("patch_apply_seconds", labels={}):
        result = pm.apply_patch(
            request.file_path,
            request.content,
            source=request.source,
            metadata=request.metadata,
        )

    if not result.success:
        message = result.message or "Patch apply failed"
        status_code = 409 if "conflict" in message.lower() else 400
        metrics.inc("patch_apply_total", labels={"outcome": "error"})
        audit.emit(
            actor="anonymous", action="POST /patch/apply",
            target=request.file_path, ip=actor_ip,
            outcome="error", extra=message[:200],
        )
        return create_error_response(message, status_code=status_code)

    response_payload = {
        "success": result.success,
        "message": result.message,
        "session_id": result.session_id,
        "diff": result.diff,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "file_path": result.file_path,
        "rollback_available": result.rollback_available,
    }

    if idempotency_key:
        # Cache the inner payload (a plain dict) so a replay can wrap it
        # back into create_success_response cleanly.
        idem_store.store(idempotency_key, idem_payload, response_payload)

    metrics.inc(
        "patch_apply_total",
        labels={"outcome": "ok" if result.success else "error"},
    )
    audit.emit(
        actor="anonymous", action="POST /patch/apply",
        target=request.file_path, ip=actor_ip,
        outcome="ok" if result.success else "error",
        extra=(
            f"+{result.lines_added}/-{result.lines_removed} "
            f"sid={result.session_id or '?'}"
        ),
    )
    return create_success_response(response_payload)


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
