"""Optional API key authentication middleware.

When ``OMNICODE_API_KEY`` is set in settings (or the environment), every
HTTP request must carry it via one of:

* ``X-API-Key: <key>`` header
* ``Authorization: Bearer <key>`` header

Requests to public paths (health, docs, openapi schema) and CORS preflights
are always allowed. When the setting is empty the middleware is a no-op.

Designed for the "self-hosted MCP gateway exposed on a LAN / via a reverse
proxy" scenario described in P1 of architecture-v2. Treat it as a soft
gate — combine with TLS termination at the proxy for real production use.
"""

from __future__ import annotations

import logging
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that always bypass auth — health probes, generated docs, MCP discovery.
_PUBLIC_PATHS: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


def _is_public(path: str, public_paths: Iterable[str] = _PUBLIC_PATHS) -> bool:
    return any(path == p or path.startswith(p + "/") for p in public_paths)


def _extract_token(request: Request) -> str | None:
    # X-API-Key takes precedence (no parsing required).
    header = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if header:
        return header.strip()

    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request that doesn't present the configured API key.

    Created lazily via ``install`` so that disabling auth (the default)
    has zero overhead — we never even attach the middleware in that case.
    """

    def __init__(self, app, expected_key: str) -> None:
        super().__init__(app)
        if not expected_key:
            raise ValueError("APIKeyAuthMiddleware requires a non-empty key")
        self._expected = expected_key

    async def dispatch(self, request: Request, call_next):
        # CORS preflights MUST be allowed through unauthenticated so the
        # browser can negotiate; the actual GET/POST that follows is gated.
        if request.method == "OPTIONS":
            return await call_next(request)

        if _is_public(request.url.path):
            return await call_next(request)

        token = _extract_token(request)
        if token != self._expected:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: missing or invalid API key.",
                    "hint": "Provide it via X-API-Key or Authorization: Bearer <key>.",
                    "success": False,
                },
            )
        return await call_next(request)


def install(app, expected_key: str) -> bool:
    """Attach the middleware if a key is configured. Returns True if attached."""
    if not expected_key:
        logger.info("🔓 API key auth disabled (OMNICODE_API_KEY unset).")
        return False
    app.add_middleware(APIKeyAuthMiddleware, expected_key=expected_key)
    logger.info("🔐 API key auth enabled.")
    return True


__all__ = ["APIKeyAuthMiddleware", "install"]
