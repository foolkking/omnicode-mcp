"""SQLite-backed user store with role-based tokens.

Default DB location: ``~/.kiro/codebase-mcp/users.db``.
Tokens are hashed (SHA-256) at rest; the plain-text token is shown to
the caller only once at creation time and never stored. This mirrors
the GitHub PAT model and avoids the SecretBox dependency we already use
for provider keys.

Schema:

```
users(
  username TEXT PRIMARY KEY,
  role     TEXT NOT NULL CHECK (role IN ('admin','editor','viewer')),
  created_at TEXT NOT NULL
)

tokens(
  token_hash TEXT PRIMARY KEY,
  username   TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  label      TEXT,
  created_at TEXT NOT NULL,
  last_used_at TEXT
)
```
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"

    @classmethod
    def parse(cls, value: str) -> "Role":
        try:
            return cls(value.lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown role: {value!r}") from exc


@dataclass
class User:
    username: str
    role: Role
    created_at: str

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "role": self.role.value,
            "created_at": self.created_at,
        }


@dataclass
class TokenIssued:
    """Returned once at creation; the plain ``token`` is never persisted."""

    token: str
    token_hash: str
    username: str
    role: Role
    label: Optional[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _default_store_path() -> Path:
    return Path.home() / ".kiro" / "codebase-mcp" / "users.db"


class UserStore:
    """Thread-safe SQLite-backed user/token registry."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path: Path = db_path or _default_store_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------ schema
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with _LOCK, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username   TEXT PRIMARY KEY,
                    role       TEXT NOT NULL CHECK (role IN ('admin','editor','viewer')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash   TEXT PRIMARY KEY,
                    username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    label        TEXT,
                    created_at   TEXT NOT NULL,
                    last_used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS tokens_by_user ON tokens(username);
                """
            )

    # ------------------------------------------------------------ users
    def list_users(self) -> List[User]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT username, role, created_at FROM users ORDER BY created_at"
            ).fetchall()
        return [User(username=r[0], role=Role.parse(r[1]), created_at=r[2]) for r in rows]

    def get_user(self, username: str) -> Optional[User]:
        with _LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT username, role, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return User(username=row[0], role=Role.parse(row[1]), created_at=row[2])

    def create_user(self, username: str, role: Role) -> User:
        if not username or not username.strip():
            raise ValueError("username must be non-empty")
        username = username.strip()
        with _LOCK, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (username, role, created_at) VALUES (?, ?, ?)",
                    (username, role.value, _now()),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"user already exists: {username}") from exc
        return self.get_user(username)  # type: ignore[return-value]

    def update_role(self, username: str, role: Role) -> Optional[User]:
        with _LOCK, self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (role.value, username),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_user(username)

    def delete_user(self, username: str) -> bool:
        with _LOCK, self._connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------ tokens
    def issue_token(self, username: str, label: Optional[str] = None) -> TokenIssued:
        user = self.get_user(username)
        if user is None:
            raise ValueError(f"unknown user: {username}")
        token = "omn_" + secrets.token_urlsafe(32)
        digest = _hash(token)
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO tokens (token_hash, username, label, created_at) VALUES (?, ?, ?, ?)",
                (digest, username, label, _now()),
            )
            conn.commit()
        return TokenIssued(
            token=token,
            token_hash=digest,
            username=username,
            role=user.role,
            label=label,
        )

    def revoke_token(self, token_hash: str) -> bool:
        with _LOCK, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tokens WHERE token_hash = ?", (token_hash,)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_tokens(self, username: Optional[str] = None) -> List[dict]:
        with _LOCK, self._connect() as conn:
            if username:
                rows = conn.execute(
                    "SELECT token_hash, username, label, created_at, last_used_at "
                    "FROM tokens WHERE username = ? ORDER BY created_at",
                    (username,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT token_hash, username, label, created_at, last_used_at "
                    "FROM tokens ORDER BY created_at"
                ).fetchall()
        return [
            {
                "token_hash": r[0],
                "username": r[1],
                "label": r[2],
                "created_at": r[3],
                "last_used_at": r[4],
            }
            for r in rows
        ]

    def authenticate(self, token: str) -> Optional[User]:
        """Return the user associated with ``token`` or None.

        Updates ``last_used_at`` as a side effect (best-effort; failures
        are swallowed since auth must remain fast).
        """
        if not token:
            return None
        digest = _hash(token)
        with _LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT u.username, u.role, u.created_at "
                "FROM tokens t JOIN users u ON t.username = u.username "
                "WHERE t.token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            try:
                conn.execute(
                    "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?",
                    (_now(), digest),
                )
                conn.commit()
            except Exception:
                pass
        return User(username=row[0], role=Role.parse(row[1]), created_at=row[2])


_DEFAULT_STORE: Optional[UserStore] = None


def get_user_store() -> UserStore:
    """Return the process-wide default UserStore (lazy)."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = UserStore()
    return _DEFAULT_STORE


__all__ = ["Role", "User", "UserStore", "TokenIssued", "get_user_store"]
