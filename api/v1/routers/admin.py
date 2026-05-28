"""Admin endpoints for user/token management (multi-user mode).

Mounted under ``/admin``. The RBAC middleware ensures only admins can
hit these. When no users exist yet, the *first* call to
``POST /admin/users`` is allowed unauthenticated to bootstrap; that
caller becomes admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from omnicode_core.auth import Role, get_user_store
from omnicode_core.observability import get_audit_log, get_rate_limiter

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Per-IP rate limit dep — applied to every mutating endpoint here.
# Read endpoints (GET) skip rate limiting; the value of audit-logging
# every list_users() request is low and this keeps the limiter table small.
# ---------------------------------------------------------------------------
def _rate_limit_admin(request: Request) -> None:
    """Reject calls from a (per-IP) bucket that exceeds the admin policy.

    Default: 30 mutations / minute / IP, burst of 10. Override via
    ``OMNICODE_ADMIN_RATE_LIMIT`` env var (decimal, requests per minute).
    """
    import os
    rate = float(os.environ.get("OMNICODE_ADMIN_RATE_LIMIT", "30"))
    limiter = get_rate_limiter("admin", rate_per_minute=rate, burst=10)
    ip = (request.client.host if request.client else "") or "anonymous"
    allowed, retry_after = limiter.check("admin", ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded for /admin/* "
                f"(retry after {retry_after:.1f}s). "
                f"Tune OMNICODE_ADMIN_RATE_LIMIT to raise."
            ),
            headers={"Retry-After": f"{int(retry_after) + 1}"},
        )


def _emit_audit(request: Request, action: str, target: str, outcome: str) -> None:
    ip = (request.client.host if request.client else "") or ""
    get_audit_log().emit(
        actor="anonymous",  # actor lookup happens in middleware; admin shape
                            # currently doesn't surface the resolved user
        action=action,
        target=target,
        ip=ip,
        outcome=outcome,
    )


def _ok(payload):
    return {"result": payload, "success": True}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class _CreateUserBody(BaseModel):
    username: str = Field(..., min_length=1)
    role: str = Field(default="viewer", description="admin | editor | viewer")


class _UpdateRoleBody(BaseModel):
    role: str


class _IssueTokenBody(BaseModel):
    username: str
    label: str | None = None
    expires_in_days: int | None = None


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users():
    return _ok({"users": [u.to_dict() for u in get_user_store().list_users()]})


@router.post("/users", dependencies=[Depends(_rate_limit_admin)])
async def create_user(body: _CreateUserBody, request: Request):
    try:
        role = Role.parse(body.role)
    except ValueError as exc:
        _emit_audit(request, "POST /admin/users", body.username, "denied")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store = get_user_store()
    # Bootstrap rule: when no users exist, force the first one to be admin so
    # there is always somebody who can manage the system. The RBAC middleware
    # also allows this call through unauthenticated for the same reason. To
    # avoid a chicken-and-egg lockout we also auto-issue a "bootstrap" token
    # so the caller can authenticate every subsequent admin request.
    is_bootstrap = not store.list_users()
    if is_bootstrap and role != Role.ADMIN:
        role = Role.ADMIN
    try:
        user = store.create_user(body.username, role)
    except ValueError as exc:
        _emit_audit(request, "POST /admin/users", body.username, "denied")
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    payload = {"user": user.to_dict()}
    if is_bootstrap:
        issued = store.issue_token(user.username, label="bootstrap")
        payload["bootstrap_token"] = issued.token
        payload["bootstrap_warning"] = (
            "Save this token now — it cannot be retrieved again. "
            "Send it as X-API-Key on subsequent admin requests."
        )
    _emit_audit(
        request, "POST /admin/users", body.username,
        "ok_bootstrap" if is_bootstrap else "ok",
    )
    return _ok(payload)


@router.put("/users/{username}/role", dependencies=[Depends(_rate_limit_admin)])
async def update_role(username: str, body: _UpdateRoleBody, request: Request):
    try:
        role = Role.parse(body.role)
    except ValueError as exc:
        _emit_audit(request, "PUT /admin/users/{u}/role", username, "denied")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = get_user_store().update_role(username, role)
    if user is None:
        _emit_audit(request, "PUT /admin/users/{u}/role", username, "not_found")
        raise HTTPException(status_code=404, detail="user not found")
    _emit_audit(request, "PUT /admin/users/{u}/role", f"{username}->{role.value}", "ok")
    return _ok({"user": user.to_dict()})


@router.delete("/users/{username}", dependencies=[Depends(_rate_limit_admin)])
async def delete_user(username: str, request: Request):
    if not get_user_store().delete_user(username):
        _emit_audit(request, "DELETE /admin/users/{u}", username, "not_found")
        raise HTTPException(status_code=404, detail="user not found")
    _emit_audit(request, "DELETE /admin/users/{u}", username, "ok")
    return _ok({"deleted": username})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
@router.get("/tokens")
async def list_tokens(username: str | None = None):
    return _ok({"tokens": get_user_store().list_tokens(username)})


@router.post("/tokens", dependencies=[Depends(_rate_limit_admin)])
async def issue_token(body: _IssueTokenBody, request: Request):
    try:
        issued = get_user_store().issue_token(
            body.username,
            body.label,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        _emit_audit(request, "POST /admin/tokens", body.username, "not_found")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _emit_audit(
        request, "POST /admin/tokens",
        f"{issued.username}#{(body.label or '')[:24]}",
        "ok",
    )
    return _ok(
        {
            "token": issued.token,  # plain — only ever returned here
            "token_hash": issued.token_hash,
            "username": issued.username,
            "role": issued.role.value,
            "label": issued.label,
            "warning": "Save this token now — it cannot be retrieved again.",
        }
    )


@router.delete("/tokens/{token_hash}", dependencies=[Depends(_rate_limit_admin)])
async def revoke_token(token_hash: str, request: Request):
    if not get_user_store().revoke_token(token_hash):
        _emit_audit(request, "DELETE /admin/tokens/{h}", token_hash[:24], "not_found")
        raise HTTPException(status_code=404, detail="token not found")
    _emit_audit(request, "DELETE /admin/tokens/{h}", token_hash[:24], "ok")
    return _ok({"revoked": token_hash})


@router.delete("/users/{username}/tokens", dependencies=[Depends(_rate_limit_admin)])
async def revoke_all_user_tokens(username: str, request: Request):
    """Revoke every token belonging to ``username`` in one call.

    Use case: an employee left or a laptop was lost — kill the whole
    user's session set without manually iterating ``/admin/tokens``.
    """
    store = get_user_store()
    if store.get_user(username) is None:
        _emit_audit(request, "DELETE /admin/users/{u}/tokens", username, "not_found")
        raise HTTPException(status_code=404, detail="user not found")
    count = store.revoke_user_tokens(username)
    _emit_audit(
        request, "DELETE /admin/users/{u}/tokens", username,
        f"ok_revoked={count}",
    )
    return _ok({"username": username, "revoked": count})


__all__ = ["router"]
