"""Idempotency-Key cache for write endpoints.

The classic problem this fixes: an AI editor (or its retry layer)
sends ``POST /patch/apply`` with the same body twice because it
didn't see the first response. Without idempotency we'd apply the
same patch twice — once for real, once on top of the new content,
producing a confusing ``+0/-0`` second EditSession.

Solution: clients pass an ``Idempotency-Key`` header with a stable
UUID per logical operation. The first request with a given key
executes normally and the response is cached; subsequent requests
with the same key + same payload return the cached response. A
different payload with the same key returns 409.

Storage: a small SQLite table at ``<wd>/.data/idempotency.db``. Rows
TTL after 24 h to keep the table bounded — the Idempotency-Key spec
treats keys as ephemeral.

Failure modes are silent: any DB error degrades to "always run the
operation", which is the safe behaviour (worse latency, never
correctness).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Cache TTL — Idempotency-Key spec treats keys as ephemeral (RFC draft)
_TTL_SECONDS = 24 * 60 * 60  # 24h


class IdempotencyConflict(Exception):
    """Raised when the same key is reused with a different payload."""


class IdempotencyStore:
    """SQLite-backed Idempotency-Key cache.

    Schema::

        CREATE TABLE keys (
            key TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else Path(".data") / "idempotency.db"
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS keys (
                        key TEXT PRIMARY KEY,
                        payload_hash TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_created_at ON keys(created_at)"
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Idempotency store init failed: %s", exc)

    @staticmethod
    def _hash_payload(payload: Any) -> str:
        """Stable hash of a JSON-serialisable payload."""
        try:
            blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        except (TypeError, ValueError):
            blob = repr(payload).encode("utf-8", errors="replace")
        return hashlib.sha256(blob).hexdigest()[:32]

    def lookup(self, key: str, payload: Any) -> Optional[dict]:
        """Return cached response if (key, payload) matches; raise on conflict."""
        if not key:
            return None
        payload_hash = self._hash_payload(payload)
        cutoff = time.time() - _TTL_SECONDS
        try:
            with self._lock, closing(sqlite3.connect(self.db_path)) as conn:
                # Lazily prune expired rows so the table stays bounded.
                conn.execute("DELETE FROM keys WHERE created_at < ?", (cutoff,))
                row = conn.execute(
                    "SELECT payload_hash, response_json FROM keys WHERE key = ?",
                    (key,),
                ).fetchone()
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Idempotency lookup failed: %s", exc)
            return None
        if not row:
            return None
        stored_hash, response_json = row
        if stored_hash != payload_hash:
            raise IdempotencyConflict(
                f"Idempotency-Key '{key}' reused with a different payload "
                f"(expected hash={stored_hash[:12]}…, got {payload_hash[:12]}…)."
            )
        try:
            return json.loads(response_json)
        except json.JSONDecodeError:
            return None

    def store(self, key: str, payload: Any, response: Any) -> None:
        """Cache the response for future identical (key, payload) requests."""
        if not key:
            return
        payload_hash = self._hash_payload(payload)
        try:
            response_json = json.dumps(response, default=str)
        except (TypeError, ValueError) as exc:
            logger.debug("Idempotency response not JSON-serialisable: %s", exc)
            return
        try:
            with self._lock, closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO keys "
                    "(key, payload_hash, response_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, payload_hash, response_json, time.time()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Idempotency store failed: %s", exc)


_DEFAULT: Optional[IdempotencyStore] = None


def get_idempotency_store(working_dir: Optional[str] = None) -> IdempotencyStore:
    """Return the process-wide IdempotencyStore singleton.

    First call resolves the DB path from ``<working_dir>/.data/idempotency.db``
    where ``working_dir`` defaults to the current working directory.
    """
    global _DEFAULT
    if _DEFAULT is None:
        wd = Path(working_dir) if working_dir else Path.cwd()
        _DEFAULT = IdempotencyStore(db_path=wd / ".data" / "idempotency.db")
    return _DEFAULT


__all__ = ["IdempotencyConflict", "IdempotencyStore", "get_idempotency_store"]
