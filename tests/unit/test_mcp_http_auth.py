"""Unit tests for the MCP-over-HTTP auth gate (Wave 2, W2-5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import omnicode_core.auth.users as users_mod
from omnicode_adapters.mcp_server.http_auth import (
    _extract_token,
    _is_public,
    make_auth_middleware,
)
from omnicode_core.auth.users import Role, UserStore


def _build_inner_app() -> Starlette:
    async def health(request):
        return JSONResponse({"ok": True})

    async def tool_call(request):
        return JSONResponse({"ok": True, "type": "tool"})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/tools/x", tool_call, methods=["POST"]),
        ]
    )


@pytest.fixture(autouse=True)
def isolated_user_store(tmp_path: Path, monkeypatch):
    """Replace the user-store singleton so each test starts empty."""
    store = UserStore(db_path=tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_DEFAULT_STORE", store)
    return store


# ---------------------------------------------------------------------------
# Token extraction helpers
# ---------------------------------------------------------------------------
def test_extract_token_from_bearer():
    headers = [(b"authorization", b"Bearer abc123")]
    assert _extract_token(headers) == "abc123"


def test_extract_token_from_x_api_key():
    headers = [(b"x-api-key", b"secret")]
    assert _extract_token(headers) == "secret"


def test_extract_token_x_api_key_wins():
    """When both are present, X-API-Key wins (single source of truth)."""
    headers = [
        (b"x-api-key", b"explicit"),
        (b"authorization", b"Bearer fallback"),
    ]
    assert _extract_token(headers) == "explicit"


def test_extract_token_missing():
    assert _extract_token([]) is None
    assert _extract_token([(b"content-type", b"text/plain")]) is None


def test_is_public_paths():
    assert _is_public("/health") is True
    assert _is_public("/health/ready") is True
    assert _is_public("/ping") is True
    assert _is_public("/tools/list") is False


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------
def test_no_auth_configured_means_passthrough(monkeypatch):
    """When no key + no users, middleware is a no-op."""
    monkeypatch.delenv("OMNICODE_API_KEY", raising=False)
    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)
    assert client.get("/health").status_code == 200
    assert client.post("/tools/x").status_code == 200


def test_legacy_key_required_when_set(monkeypatch):
    monkeypatch.setenv("OMNICODE_API_KEY", "k1")
    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)
    # Health is always public.
    assert client.get("/health").status_code == 200
    # Anonymous tool call → 401.
    r = client.post("/tools/x")
    assert r.status_code == 401
    # Wrong key → 401.
    r = client.post("/tools/x", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    # Right key → 200.
    r = client.post("/tools/x", headers={"X-API-Key": "k1"})
    assert r.status_code == 200


def test_bearer_format_accepted(monkeypatch):
    monkeypatch.setenv("OMNICODE_API_KEY", "k1")
    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)
    r = client.post("/tools/x", headers={"Authorization": "Bearer k1"})
    assert r.status_code == 200


def test_rbac_token_accepted(monkeypatch, isolated_user_store):
    """When users exist, RBAC tokens authenticate even if legacy key is unset."""
    monkeypatch.delenv("OMNICODE_API_KEY", raising=False)
    isolated_user_store.create_user("alice", Role.ADMIN)
    issued = isolated_user_store.issue_token("alice")

    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)

    # Anonymous → 401 (because users exist, auth IS configured).
    r = client.post("/tools/x")
    assert r.status_code == 401

    # With RBAC token → 200.
    r = client.post("/tools/x", headers={"X-API-Key": issued.token})
    assert r.status_code == 200


def test_rbac_revoked_token_rejected(monkeypatch, isolated_user_store):
    monkeypatch.delenv("OMNICODE_API_KEY", raising=False)
    isolated_user_store.create_user("a", Role.ADMIN)
    issued = isolated_user_store.issue_token("a")
    isolated_user_store.revoke_token(issued.token_hash)

    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)
    r = client.post("/tools/x", headers={"X-API-Key": issued.token})
    assert r.status_code == 401


def test_health_always_passes_even_with_auth(monkeypatch):
    monkeypatch.setenv("OMNICODE_API_KEY", "k1")
    wrapped = make_auth_middleware(_build_inner_app())
    client = TestClient(wrapped)
    assert client.get("/health").status_code == 200
