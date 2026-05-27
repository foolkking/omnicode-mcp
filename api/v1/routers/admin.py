"""Admin endpoints for user/token management (multi-user mode).

Mounted under ``/admin``. The RBAC middleware ensures only admins can
hit these. When no users exist yet, the *first* call to
``POST /admin/users`` is allowed unauthenticated to bootstrap; that
caller becomes admin.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from omnicode_core.auth import Role, get_user_store

router = APIRouter(prefix="/admin", tags=["admin"])


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


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users():
    return _ok({"users": [u.to_dict() for u in get_user_store().list_users()]})


@router.post("/users")
async def create_user(body: _CreateUserBody):
    try:
        role = Role.parse(body.role)
    except ValueError as exc:
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    payload = {"user": user.to_dict()}
    if is_bootstrap:
        issued = store.issue_token(user.username, label="bootstrap")
        payload["bootstrap_token"] = issued.token
        payload["bootstrap_warning"] = (
            "Save this token now — it cannot be retrieved again. "
            "Send it as X-API-Key on subsequent admin requests."
        )
    return _ok(payload)


@router.put("/users/{username}/role")
async def update_role(username: str, body: _UpdateRoleBody):
    try:
        role = Role.parse(body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = get_user_store().update_role(username, role)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _ok({"user": user.to_dict()})


@router.delete("/users/{username}")
async def delete_user(username: str):
    if not get_user_store().delete_user(username):
        raise HTTPException(status_code=404, detail="user not found")
    return _ok({"deleted": username})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
@router.get("/tokens")
async def list_tokens(username: str | None = None):
    return _ok({"tokens": get_user_store().list_tokens(username)})


@router.post("/tokens")
async def issue_token(body: _IssueTokenBody):
    try:
        issued = get_user_store().issue_token(body.username, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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


@router.delete("/tokens/{token_hash}")
async def revoke_token(token_hash: str):
    if not get_user_store().revoke_token(token_hash):
        raise HTTPException(status_code=404, detail="token not found")
    return _ok({"revoked": token_hash})


__all__ = ["router"]
