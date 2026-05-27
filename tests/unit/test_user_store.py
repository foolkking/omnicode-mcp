"""Unit tests for the multi-user / RBAC user store (P2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.auth.users import Role, UserStore


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    return UserStore(db_path=tmp_path / "users.db")


def test_role_parse_lowercases():
    assert Role.parse("ADMIN") is Role.ADMIN
    assert Role.parse("editor") is Role.EDITOR
    with pytest.raises(ValueError):
        Role.parse("super-user")


def test_create_and_get_user(store: UserStore):
    u = store.create_user("alice", Role.ADMIN)
    assert u.username == "alice"
    assert u.role is Role.ADMIN
    fetched = store.get_user("alice")
    assert fetched is not None
    assert fetched.role is Role.ADMIN


def test_create_duplicate_user_raises(store: UserStore):
    store.create_user("bob", Role.VIEWER)
    with pytest.raises(ValueError):
        store.create_user("bob", Role.EDITOR)


def test_update_role(store: UserStore):
    store.create_user("carol", Role.VIEWER)
    out = store.update_role("carol", Role.EDITOR)
    assert out is not None
    assert out.role is Role.EDITOR


def test_delete_user_cascades_tokens(store: UserStore):
    store.create_user("dave", Role.EDITOR)
    issued = store.issue_token("dave", label="laptop")
    assert store.authenticate(issued.token) is not None
    assert store.delete_user("dave") is True
    # Tokens removed via FK cascade
    assert store.authenticate(issued.token) is None


def test_issue_and_authenticate_token(store: UserStore):
    store.create_user("eve", Role.EDITOR)
    issued = store.issue_token("eve")
    user = store.authenticate(issued.token)
    assert user is not None
    assert user.username == "eve"
    assert user.role is Role.EDITOR


def test_authenticate_wrong_token(store: UserStore):
    store.create_user("frank", Role.VIEWER)
    assert store.authenticate("omn_does-not-exist") is None
    assert store.authenticate("") is None


def test_revoke_token(store: UserStore):
    store.create_user("grace", Role.VIEWER)
    issued = store.issue_token("grace")
    assert store.authenticate(issued.token) is not None
    assert store.revoke_token(issued.token_hash) is True
    assert store.authenticate(issued.token) is None


def test_issue_token_for_unknown_user(store: UserStore):
    with pytest.raises(ValueError):
        store.issue_token("ghost")


def test_list_tokens_filtered_by_user(store: UserStore):
    store.create_user("u1", Role.VIEWER)
    store.create_user("u2", Role.VIEWER)
    store.issue_token("u1")
    store.issue_token("u2")
    store.issue_token("u2")
    assert len(store.list_tokens("u1")) == 1
    assert len(store.list_tokens("u2")) == 2
    assert len(store.list_tokens()) == 3


def test_token_format_is_prefixed(store: UserStore):
    store.create_user("hank", Role.ADMIN)
    issued = store.issue_token("hank")
    assert issued.token.startswith("omn_")
    assert len(issued.token) > 16


def test_token_hash_is_deterministic(store: UserStore):
    store.create_user("ivy", Role.ADMIN)
    issued1 = store.issue_token("ivy")
    # The same token must hash the same way every call.
    import hashlib
    assert hashlib.sha256(issued1.token.encode()).hexdigest() == issued1.token_hash
