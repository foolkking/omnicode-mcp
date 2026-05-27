"""Unit tests for token expiry + revoke-by-user (Wave 2 W2-4)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from omnicode_core.auth.users import Role, UserStore


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    return UserStore(db_path=tmp_path / "users.db")


def test_migration_adds_expires_at_column(tmp_path: Path):
    """A fresh store must end up with an `expires_at` column even if
    `_init_schema` only created the tables without it."""
    store = UserStore(db_path=tmp_path / "users.db")
    with sqlite3.connect(str(store.db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tokens)").fetchall()}
    assert "expires_at" in cols


def test_user_version_is_bumped(tmp_path: Path):
    store = UserStore(db_path=tmp_path / "users.db")
    with sqlite3.connect(str(store.db_path)) as conn:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
    assert v >= 1


def test_default_token_has_no_expiry(store: UserStore):
    store.create_user("alice", Role.ADMIN)
    issued = store.issue_token("alice")
    rows = store.list_tokens("alice")
    assert len(rows) == 1
    assert rows[0]["expires_at"] is None
    assert store.authenticate(issued.token) is not None


def test_token_with_expiry_works_until_due(store: UserStore):
    store.create_user("bob", Role.EDITOR)
    issued = store.issue_token("bob", expires_in_days=7)
    rows = store.list_tokens("bob")
    assert rows[0]["expires_at"] is not None
    expiry = datetime.fromisoformat(rows[0]["expires_at"])
    now = datetime.now(timezone.utc)
    delta = expiry - now
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7, minutes=1)
    assert store.authenticate(issued.token) is not None


def test_expired_token_is_auto_revoked(store: UserStore):
    """Forge a row with an already-past expiry and verify auth refuses
    it AND deletes the row."""
    store.create_user("carol", Role.ADMIN)
    issued = store.issue_token("carol")
    # Mutate the row directly to simulate elapsed time.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "UPDATE tokens SET expires_at = ? WHERE token_hash = ?",
            (past, issued.token_hash),
        )
        conn.commit()

    assert store.authenticate(issued.token) is None
    # Auto-revoked → row gone.
    assert store.list_tokens("carol") == []


def test_malformed_expires_at_does_not_lock_user_out(store: UserStore):
    """If somehow a non-ISO value lands in `expires_at`, treat the
    token as not expiring rather than refusing it."""
    store.create_user("dave", Role.VIEWER)
    issued = store.issue_token("dave")
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "UPDATE tokens SET expires_at = ? WHERE token_hash = ?",
            ("not-a-date", issued.token_hash),
        )
        conn.commit()
    assert store.authenticate(issued.token) is not None


def test_revoke_user_tokens_clears_set(store: UserStore):
    store.create_user("eve", Role.EDITOR)
    a = store.issue_token("eve").token
    b = store.issue_token("eve").token
    assert store.authenticate(a) is not None
    assert store.authenticate(b) is not None

    n = store.revoke_user_tokens("eve")
    assert n == 2
    assert store.authenticate(a) is None
    assert store.authenticate(b) is None


def test_revoke_user_tokens_for_nonexistent_user(store: UserStore):
    """Should be a no-op (count 0) — not an error — so this is a
    safe operation to call from a "kill the user" admin script."""
    assert store.revoke_user_tokens("ghost") == 0
