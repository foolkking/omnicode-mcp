"""Unit tests for the read-only middleware (Wave 1, gap §13)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.read_only_middleware import _is_read_ok_post, install


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/files/read")
    async def read():
        return {"ok": True}

    @app.post("/files/write")
    async def write():
        return {"ok": True, "wrote": True}

    @app.post("/search")
    async def search():
        return {"ok": True, "type": "query"}

    @app.post("/intelligence/context")
    async def intel():
        return {"ok": True, "type": "composer"}

    @app.post("/admin/users")
    async def admin_users():
        return {"ok": True, "type": "bootstrap"}

    @app.post("/patch/apply")
    async def patch_apply():
        return {"ok": True, "applied": True}

    install(app)
    yield app

    # Settings is lru-cached; ensure we don't leak monkeypatched env
    # state into the next test in the session.
    from omnicode.config.settings import get_settings

    get_settings.cache_clear()


def test_writes_pass_when_read_only_off(monkeypatch, app):
    monkeypatch.setenv("OMNICODE_READ_ONLY", "false")
    # Settings is a singleton — re-import to refresh.
    from omnicode.config.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(app)
    assert client.post("/files/write").status_code == 200
    assert client.post("/patch/apply").status_code == 200


def test_writes_blocked_when_read_only_on(monkeypatch, app):
    monkeypatch.setenv("OMNICODE_READ_ONLY", "true")
    from omnicode.config.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post("/files/write")
    assert r.status_code == 403
    assert "read-only" in r.json()["error"].lower()
    r2 = client.post("/patch/apply")
    assert r2.status_code == 403


def test_reads_always_pass(monkeypatch, app):
    monkeypatch.setenv("OMNICODE_READ_ONLY", "true")
    from omnicode.config.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/files/read").status_code == 200


def test_query_only_posts_pass_when_read_only_on(monkeypatch, app):
    monkeypatch.setenv("OMNICODE_READ_ONLY", "true")
    from omnicode.config.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(app)
    assert client.post("/search").status_code == 200
    assert client.post("/intelligence/context").status_code == 200
    assert client.post("/admin/users").status_code == 200


def test_is_read_ok_post_helper():
    assert _is_read_ok_post("POST", "/search") is True
    assert _is_read_ok_post("POST", "/intelligence/context") is True
    assert _is_read_ok_post("POST", "/patch/preview") is True
    assert _is_read_ok_post("POST", "/patch/apply") is False
    assert _is_read_ok_post("GET", "/search") is False
    assert _is_read_ok_post("POST", "/files/write") is False
