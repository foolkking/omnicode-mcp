"""Regression tests for session-start idempotency and branch detection.

Covers the bugs reported in the 2026-05-22 evening session:

1. Clicking "Start" twice with the same session name returned 502 with
   ``fatal: a branch named '...' already exists``.  Now we should detect
   the existing branch and just check it out (or stay on it).
2. UI showed "未激活" (inactive) when the user was actually on a
   user-named branch like ``你好`` because the detection only looked
   for ``ai-session-*`` / ``session-*`` prefixes.
3. The ``end`` operation hard-coded a switch to ``master``, which fails
   on repos using ``main`` as the default branch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app


# ---------------------------------------------------------------------------
# Branch detection — list_session_branches now includes user-named branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_session_branches_includes_user_named_branches():
    """Any branch other than master/main/trunk/develop should appear in the
    session list — including non-ASCII names like '你好'."""
    from omnicode.git_context.git_manager import GitManager, GitResult

    gm = GitManager.__new__(GitManager)  # bypass __init__
    fake_branches = [
        {"name": "main", "is_current": False},
        {"name": "你好", "is_current": True},
        {"name": "feature-x", "is_current": False},
        {"name": "ai-session-20260522-103000", "is_current": False},
    ]

    async def _fake_get_branches():
        r = GitResult(success=True, output="", error=None, return_code=0)
        r.data = {"branches": fake_branches}
        return r

    gm.get_branches = _fake_get_branches  # type: ignore[assignment]

    res = await gm.list_session_branches()
    assert res.success
    names = [b["name"] for b in res.data["sessions"]]
    assert "你好" in names
    assert "feature-x" in names
    assert "ai-session-20260522-103000" in names
    # main MUST be excluded (it's a trunk branch)
    assert "main" not in names


# ---------------------------------------------------------------------------
# Trunk resolution — picks main / master / trunk / develop in that order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_trunk_branch_prefers_main():
    from api.v1.routers.git import _resolve_trunk_branch
    from omnicode.git_context.git_manager import GitResult

    gm = MagicMock()

    async def _branches():
        r = GitResult(success=True, output="", error=None, return_code=0)
        r.data = {"branches": [
            {"name": "feature-x", "is_current": False},
            {"name": "main", "is_current": True},
            {"name": "master", "is_current": False},
        ]}
        return r

    gm.get_branches = _branches
    assert await _resolve_trunk_branch(gm) == "main"


@pytest.mark.asyncio
async def test_resolve_trunk_branch_falls_back_to_master():
    from api.v1.routers.git import _resolve_trunk_branch
    from omnicode.git_context.git_manager import GitResult

    gm = MagicMock()

    async def _branches():
        r = GitResult(success=True, output="", error=None, return_code=0)
        r.data = {"branches": [
            {"name": "master", "is_current": True},
            {"name": "feature-x", "is_current": False},
        ]}
        return r

    gm.get_branches = _branches
    assert await _resolve_trunk_branch(gm) == "master"


@pytest.mark.asyncio
async def test_resolve_trunk_branch_returns_none_for_empty_repo():
    from api.v1.routers.git import _resolve_trunk_branch
    from omnicode.git_context.git_manager import GitResult

    gm = MagicMock()

    async def _branches():
        r = GitResult(success=True, output="", error=None, return_code=0)
        r.data = {"branches": []}
        return r

    gm.get_branches = _branches
    assert await _resolve_trunk_branch(gm) is None


# ---------------------------------------------------------------------------
# /session POST start — idempotency
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _patch_git_manager(monkeypatch, *, current_branch: str, branch_names: list[str]):
    """Stub get_git_manager() so /session sees a controlled repo state."""
    from omnicode.git_context.git_manager import GitResult

    gm = MagicMock()
    gm.is_git_repo = True

    async def _current():
        r = GitResult(success=True, output=current_branch, error=None, return_code=0)
        r.data = {"current_branch": current_branch}
        return r

    async def _branches():
        r = GitResult(success=True, output="", error=None, return_code=0)
        r.data = {"branches": [{"name": n, "is_current": n == current_branch} for n in branch_names]}
        return r

    gm.get_current_branch = _current
    gm.get_branches = _branches
    gm.checkout_branch = AsyncMock(return_value=GitResult(
        success=True, output="Switched", error=None, return_code=0,
    ))
    gm.create_branch = AsyncMock(return_value=GitResult(
        success=True, output="Created", error=None, return_code=0,
    ))

    import api.v1.routers.git as git_router
    monkeypatch.setattr(git_router, "get_git_manager", lambda: gm)
    return gm


def test_session_start_already_on_target_branch(client, monkeypatch):
    """If we're already on the requested branch, return success (reused=True)
    without calling create_branch."""
    gm = _patch_git_manager(
        monkeypatch, current_branch="你好", branch_names=["main", "你好"]
    )
    r = client.post("/session", json={"operation": "start", "session_name": "你好"})
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["session_name"] == "你好"
    assert body.get("reused") is True
    gm.create_branch.assert_not_called()
    gm.checkout_branch.assert_not_called()


def test_session_start_existing_branch_checks_out(client, monkeypatch):
    """Branch exists but we're on a different branch -> just check it out,
    don't fail with 'a branch named X already exists'."""
    gm = _patch_git_manager(
        monkeypatch, current_branch="main", branch_names=["main", "你好"]
    )
    r = client.post("/session", json={"operation": "start", "session_name": "你好"})
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["session_name"] == "你好"
    assert body.get("reused") is True
    gm.checkout_branch.assert_called_once_with("你好")
    gm.create_branch.assert_not_called()


def test_session_start_new_branch_creates_it(client, monkeypatch):
    gm = _patch_git_manager(
        monkeypatch, current_branch="main", branch_names=["main"]
    )
    r = client.post("/session", json={"operation": "start", "session_name": "fresh-branch"})
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["session_name"] == "fresh-branch"
    assert body.get("reused") is False
    gm.create_branch.assert_called_once_with("fresh-branch", switch_to=True)


def test_session_current_recognises_user_named_branch(client, monkeypatch):
    """User on '你好' should see is_session_branch=True even though it doesn't
    follow the ai-session-* / session-* convention."""
    _patch_git_manager(monkeypatch, current_branch="你好", branch_names=["main", "你好"])
    r = client.get("/session/current")
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["current_branch"] == "你好"
    assert body["is_session_branch"] is True
    assert body["is_conventional_session"] is False
    assert body["session_name"] == "你好"


def test_session_current_main_is_not_a_session(client, monkeypatch):
    _patch_git_manager(monkeypatch, current_branch="main", branch_names=["main"])
    r = client.get("/session/current")
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["is_session_branch"] is False
    assert body["session_name"] is None
