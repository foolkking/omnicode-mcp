"""Bearer-token gate for the MCP-over-HTTP transports (Wave 2, W2-5).

The FastMCP ``sse_app()`` and ``streamable_http_app()`` return raw
Starlette ASGI apps. They have no auth — anyone who can reach the
listening port can call any tool, which is fine on stdio (single
client, single process) but unacceptable for the cloud SSE / HTTP
transports.

This module wraps either app with an ASGI middleware that enforces:

* ``Authorization: Bearer <token>`` *or* ``X-API-Key: <token>``
* The token must equal ``OMNICODE_API_KEY`` (when set) **or** match a
  valid RBAC token in ``omnicode_core.auth.users.UserStore``.
* When neither auth source is configured (no env key, no users) the
  middleware is a no-op so local dev still works.
* When auth is required AND the token is missing/invalid, every
  request gets a 401 JSON response — including SSE handshakes.

Wire it via ``omnicode mcp --transport sse|streamable-http`` (see
``omnicode_adapters/cli/commands/mcp_cmd.py``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# A valid auth source either (a) sets the legacy single key or (b) has
# at least one row in the user store. We probe lazily on every request
# so the operator can add users / rotate the key without restarting.
_PUBLIC_PATHS: tuple[str, ...] = ("/health", "/ping")


def _extract_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for raw_name, raw_val in headers:
        name = raw_name.decode("latin-1").lower()
        val = raw_val.decode("latin-1")
        if name == "x-api-key":
            return val.strip()
        if name == "authorization":
            v = val.strip()
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return None


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PATHS)


def _auth_configured() -> bool:
    """Check whether ANY auth source is configured."""
    if os.environ.get("OMNICODE_API_KEY", ""):
        return True
    try:
        from omnicode_core.auth.users import get_user_store

        return bool(get_user_store().list_users())
    except Exception:  # pragma: no cover - defensive
        return False


def _validate(token: str) -> bool:
    """Return True if ``token`` matches the legacy single key or any
    RBAC token in the user store."""
    expected = os.environ.get("OMNICODE_API_KEY", "")
    if expected and token == expected:
        return True
    try:
        from omnicode_core.auth.users import get_user_store

        return get_user_store().authenticate(token) is not None
    except Exception:
        return False


async def _send_401(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    body = json.dumps(
        {
            "error": (
                "Unauthorized: MCP-over-HTTP requires X-API-Key or "
                "Authorization: Bearer <token>."
            ),
            "success": False,
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def make_auth_middleware(inner_app: Callable):
    """Return a Starlette/ASGI app that wraps ``inner_app`` behind auth.

    Designed for FastMCP's ``sse_app()`` and ``streamable_http_app()``.
    Returns the inner app unchanged when no auth source is configured
    so local dev keeps working.
    """

    async def app(scope: dict, receive, send):
        # Only intercept HTTP-style scopes (FastMCP's SSE app is one).
        if scope.get("type") != "http":
            await inner_app(scope, receive, send)
            return

        if not _auth_configured():
            await inner_app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_public(path):
            await inner_app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        token = _extract_token(headers)
        if not token or not _validate(token):
            logger.warning(
                "MCP-over-HTTP rejected request to %s — no/invalid token.",
                path,
            )
            await _send_401(send)
            return

        await inner_app(scope, receive, send)

    return app


__all__ = ["make_auth_middleware"]
