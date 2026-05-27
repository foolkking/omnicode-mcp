"""Unit tests for the multi-user RBAC middleware (P2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import omnicode_core.auth.users as users_mod
from core.rbac_middleware import install
from omnicode_core.auth.users import Role, UserStore


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch):
    """Replace the process-wide singleton so each test gets a fresh DB."""
    store = UserStore(db_path=tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_DEFAULT_STORE", store)
    return store


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/data")
    async def get_data():
        return {"ok": True}

    @app.post("/data")
    async def post_data():
        return {"ok": True, "wrote": True}

    @app.get("/admin/users")
    async def admin():
        return {"ok": True}

    install(app)
    return app


def test_no_users_means_no_auth_required(isolated_store):
    client = TestClient(_build_app())
    assert client.get("/data").status_code == 200
    assert client.post("/data").status_code == 200
    # /admin still passes through when no users exist (bootstrap mode).
    assert client.get("/admin/users").status_code == 200


def test_health_always_public(isolated_store):
    isolated_store.create_user("a", Role.ADMIN)
    client = TestClient(_build_app())
    assert client.get("/health").status_code == 200


def test_unauthenticated_after_users_exist(isolated_store):
    isolated_store.create_user("a", Role.ADMIN)
    client = TestClient(_build_app())
    r = client.get("/data")
    assert r.status_code == 401
    assert r.json()["success"] is False


def test_viewer_can_read_but_not_write(isolated_store):
    isolated_store.create_user("v", Role.VIEWER)
    issued = isolated_store.issue_token("v")
    client = TestClient(_build_app())
    headers = {"X-API-Key": issued.token}
    assert client.get("/data", headers=headers).status_code == 200
    r = client.post("/data", headers=headers)
    assert r.status_code == 403


def test_editor_can_write(isolated_store):
    isolated_store.create_user("e", Role.EDITOR)
    issued = isolated_store.issue_token("e")
    client = TestClient(_build_app())
    r = client.post("/data", headers={"X-API-Key": issued.token})
    assert r.status_code == 200


def test_only_admin_hits_admin_routes(isolated_store):
    isolated_store.create_user("a", Role.ADMIN)
    isolated_store.create_user("e", Role.EDITOR)
    a_tok = isolated_store.issue_token("a").token
    e_tok = isolated_store.issue_token("e").token
    client = TestClient(_build_app())
    assert client.get("/admin/users", headers={"X-API-Key": a_tok}).status_code == 200
    r = client.get("/admin/users", headers={"X-API-Key": e_tok})
    assert r.status_code == 403


def test_bearer_token_accepted(isolated_store):
    isolated_store.create_user("a", Role.EDITOR)
    issued = isolated_store.issue_token("a")
    client = TestClient(_build_app())
    r = client.get("/data", headers={"Authorization": f"Bearer {issued.token}"})
    assert r.status_code == 200


def test_revoked_token_rejected(isolated_store):
    isolated_store.create_user("a", Role.ADMIN)
    issued = isolated_store.issue_token("a")
    isolated_store.revoke_token(issued.token_hash)
    client = TestClient(_build_app())
    r = client.get("/data", headers={"X-API-Key": issued.token})
    assert r.status_code == 401
