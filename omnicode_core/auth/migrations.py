"""Tiny SQLite migration runner for OmniCode-MCP (Wave 2 W2-4).

Both ``users.db`` (RBAC) and ``providers.db`` (encrypted provider keys)
need to evolve their schemas without losing rows. We use SQLite's
built-in ``PRAGMA user_version`` as the version counter — no extra
table required.

Each migration is a callable that takes a live connection and brings
the schema from version ``N`` to ``N+1``. Migrations are kept simple
and idempotent enough that re-running on a half-applied DB is safe;
in practice they each happen inside a single transaction.

Usage:

    from omnicode_core.auth.migrations import run_migrations
    run_migrations(conn, MIGRATIONS_USERS)
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)


# A migration is just (version, name, applier). ``version`` is the
# *target* version after the applier has run, so to add migration N we
# also bump user_version → N at the end.
Migration = Tuple[int, str, Callable[[sqlite3.Connection], None]]


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    # SQLite doesn't allow parameterised PRAGMA — interpolate the int
    # value, which is safe because we control its source.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def run_migrations(
    conn: sqlite3.Connection,
    migrations: List[Migration],
) -> int:
    """Apply each migration whose version is greater than the current
    ``user_version``. Returns the version after the run."""
    current = _current_version(conn)
    for version, name, applier in migrations:
        if version <= current:
            continue
        try:
            with conn:
                logger.info(
                    "DB migration → v%d (%s): applying", version, name
                )
                applier(conn)
                _set_version(conn, version)
            current = version
        except Exception as exc:
            logger.error(
                "DB migration v%d (%s) failed: %s — rolling back",
                version,
                name,
                exc,
            )
            raise
    return current


# ---------------------------------------------------------------------------
# users.db — schema is created fresh in UserStore._init_schema(). The
# migrations below add fields we tack on later.
# ---------------------------------------------------------------------------
def _users_v1_add_token_expiry(conn: sqlite3.Connection) -> None:
    """v1 → adds ``tokens.expires_at`` (NULL = never expires).

    SQLite ALTER TABLE is limited but adding a NULL column is supported.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tokens)").fetchall()}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN expires_at TEXT")


MIGRATIONS_USERS: List[Migration] = [
    (1, "tokens.expires_at", _users_v1_add_token_expiry),
]


__all__ = ["Migration", "run_migrations", "MIGRATIONS_USERS"]
