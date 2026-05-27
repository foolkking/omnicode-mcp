"""Role-based access control middleware.

When the user-store has at least one user, every request must carry a
valid token (``X-API-Key`` header or ``Authorization: Bearer <token>``).
Mutating HTTP methods (POST, PUT, PATCH, DELETE) require ``editor`` or
``admin`` role. ``viewer`` users get 403 on writes but full read access.

If the user store is empty (the typical fresh-install state), the
middleware short-circuits to allow the legacy single-key
``OMNICODE_API_KEY`` flow handled by ``core.auth_middleware``. This
keeps existing deployments working and lets users opt-in by creating
their first admin user.

User-management endpoints (``/admin/users``, ``/admin/tokens``) require
admin role.
"""

from __future__ import annotations

import logging
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from omnicode_core.auth import Role, get_user_store

logger = logging.getLogger(__name__)

_PUBLIC_PATHS: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)

_ADMIN_PATHS: tuple[str, ...] = ("/admin",)

_WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_public(path: str, public_paths: Iterable[str] = _PUBLIC_PATHS) -> bool:
    return any(path == p or path.startswith(p + "/") for p in public_paths)


def _is_admin_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_PATHS)


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class RBACMiddleware(BaseHTTPMiddleware):
    """Multi-user RBAC gate. No-op when no users exist."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._store = get_user_store()

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        # No users → fall back to the simpler API-key middleware.
        # We deliberately keep this cheap: a single SELECT COUNT.
        try:
            users = self._store.list_users()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("RBAC: user store unavailable (%s); allowing.", exc)
            return await call_next(request)
        if not users:
            return await call_next(request)

        token = _extract_token(request)
        user = self._store.authenticate(token) if token else None
        if user is None:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: missing or invalid token.",
                    "hint": "Issue one via POST /admin/tokens then send X-API-Key.",
                    "success": False,
                },
            )

        # Admin endpoints — only admin role can hit them.
        if _is_admin_path(path) and user.role != Role.ADMIN:
            return JSONResponse(
                status_code=403,
                content={
                    "error": f"Forbidden: '{user.role.value}' cannot access {path}.",
                    "success": False,
                },
            )

        # Mutations require editor or admin.
        if request.method in _WRITE_METHODS and user.role == Role.VIEWER:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Forbidden: viewer role cannot perform write operations.",
                    "success": False,
                },
            )

        # Stash the user on request.state for downstream handlers if needed.
        request.state.user = user
        return await call_next(request)


def install(app) -> None:
    """Always-attach: behaviour depends on whether the user store has rows."""
    app.add_middleware(RBACMiddleware)
    logger.info("👥 RBAC middleware installed (active when users exist).")


__all__ = ["RBACMiddleware", "install"]
