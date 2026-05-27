"""Read-only mode middleware.

Author: fool
Date: 2026-05-27 23:33:04
LastEditors: fool
LastEditTime: 2026-05-27 23:49:15
FilePath: codebase-mcp/core/read_only_middleware.py

When ``OMNICODE_READ_ONLY=true``, every mutating HTTP method
(POST / PUT / PATCH / DELETE) is blocked with 403, *except* on a small
allow-list of endpoints that don't actually mutate state on disk:

* ``POST /search``               — query-only (legacy verb)
* ``POST /intelligence/context`` — composer (read-only)
* ``POST /patch/preview``        — diff render
* ``POST /patch/validate``       — static analysis only
* ``POST /patch/explain``        — text summary only
* ``POST /admin/users``          — needed for bootstrap

Designed for cloud / shared deployments where you want callers to be
able to ask questions about the codebase but never modify it. Pair with
``OMNICODE_ALLOW_APPLY_PATCH=false`` for a stricter "look but don't
touch" posture.
"""

from __future__ import annotations

import logging
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# POSTs that are query-only — listed exactly. Anything else with a
# write method gets blocked under read-only mode.
_READ_OK_POSTS: tuple[str, ...] = (
    "/search",
    "/intelligence/context",
    "/patch/preview",
    "/patch/validate",
    "/patch/explain",
    "/admin/users",  # bootstrap path
)


def _is_read_ok_post(method: str, path: str, allowlist: Iterable[str] = _READ_OK_POSTS) -> bool:
    if method != "POST":
        return False
    return any(path == p or path.startswith(p + "/") for p in allowlist)


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """Block writes when read-only mode is on."""

    async def dispatch(self, request: Request, call_next):
        from omnicode.config.settings import get_settings

        settings = get_settings()
        if not settings.OMNICODE_READ_ONLY:
            return await call_next(request)

        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        if _is_read_ok_post(request.method, request.url.path):
            return await call_next(request)

        return JSONResponse(
            status_code=403,
            content={
                "error": (
                    "Server is in read-only mode (OMNICODE_READ_ONLY=true). "
                    f"{request.method} {request.url.path} is blocked."
                ),
                "success": False,
            },
        )


def install(app) -> None:
    """Always-attach: behaviour depends on OMNICODE_READ_ONLY at request time."""
    app.add_middleware(ReadOnlyMiddleware)
    logger.info("🔒 Read-only middleware installed (active when OMNICODE_READ_ONLY=true).")


__all__ = ["ReadOnlyMiddleware", "install"]
