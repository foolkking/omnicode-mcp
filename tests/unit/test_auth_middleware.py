"""Unit tests for the optional API key auth middleware (P1)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth_middleware import APIKeyAuthMiddleware, install


def _build_app(key: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/secret")
    async def secret():
        return {"ok": True, "data": "secret"}

    if key:
        install(app, key)
    return TestClient(app)


def test_disabled_when_key_empty():
    client = _build_app(None)
    assert client.get("/secret").status_code == 200


def test_health_always_public():
    client = _build_app("abc")
    assert client.get("/health").status_code == 200


def test_rejects_missing_key():
    client = _build_app("abc")
    r = client.get("/secret")
    assert r.status_code == 401
    body = r.json()
    assert body["success"] is False
    assert "API key" in body["error"]


def test_accepts_x_api_key_header():
    client = _build_app("abc")
    r = client.get("/secret", headers={"X-API-Key": "abc"})
    assert r.status_code == 200


def test_accepts_bearer_token():
    client = _build_app("abc")
    r = client.get("/secret", headers={"Authorization": "Bearer abc"})
    assert r.status_code == 200


def test_rejects_wrong_key():
    client = _build_app("abc")
    r = client.get("/secret", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_options_preflight_always_passes():
    client = _build_app("abc")
    # OPTIONS without auth must still succeed so browsers can negotiate CORS.
    r = client.options("/secret")
    # Starlette returns 405 for OPTIONS on routes without an explicit
    # OPTIONS handler, but the middleware must not turn it into 401.
    assert r.status_code != 401


def test_install_returns_false_when_key_blank():
    app = FastAPI()
    assert install(app, "") is False


def test_middleware_init_rejects_blank_key():
    app = FastAPI()
    try:
        APIKeyAuthMiddleware(app, expected_key="")
    except ValueError:
        return
    raise AssertionError("Expected ValueError for blank key")
