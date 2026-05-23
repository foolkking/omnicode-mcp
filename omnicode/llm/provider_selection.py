"""
Provider Selection (Active Routing Assignments)
===============================================

The Provider Registry stores *what* models are configured.
This module stores *which* of those models is actually used for each role.

Roles
-----
* ``default``  — fallback model used when no task hint is given
* ``quality``  — used for high-stakes work (refactor / review / edit)
* ``cost``     — used when latency/price matter more than quality
* ``fastest``  — used for tight-loop tasks (autocomplete-style)
* ``edit``     — overrides ``quality`` for code-editing pipelines
* ``scan``     — overrides ``cost`` for whole-codebase indexing / scanning
* ``review``   — overrides ``quality`` for security & guard reviews
* ``summary``  — overrides ``fastest`` for short-form summarisation
* ``chat``     — overrides ``fastest`` for conversational answers

A selection is just a mapping ``role -> provider_name``.  An empty value
(or a name that does not exist in the registry) means "fall back to the
auto-built chain for the corresponding strategy".

Storage
-------
A single ``selections`` table inside the same SQLite file used by the
provider registry.  Single-row design — there is exactly one selection
profile per installation; the user can change it at any time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


VALID_ROLES = (
    "default",
    "quality",
    "cost",
    "fastest",
    "edit",
    "scan",
    "review",
    "summary",
    "chat",
)


@dataclass
class ProviderSelection:
    """Mapping from role → provider name."""

    assignments: Dict[str, str] = field(default_factory=dict)

    def get(self, role: str) -> Optional[str]:
        return self.assignments.get(role) or self.assignments.get("default")

    def to_dict(self) -> Dict[str, str]:
        return dict(self.assignments)


class ProviderSelectionStore:
    """Thread-safe SQLite-backed store for the active selection profile."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS selections (
        role           TEXT PRIMARY KEY,
        provider_name  TEXT NOT NULL,
        updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ---------------------------------------------------------- db
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(self._SCHEMA)
            conn.commit()

    # ---------------------------------------------------------- CRUD
    def get_all(self) -> ProviderSelection:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT role, provider_name FROM selections"
            ).fetchall()
        return ProviderSelection(
            assignments={r["role"]: r["provider_name"] for r in rows}
        )

    def set_role(self, role: str, provider_name: Optional[str]) -> None:
        if role not in VALID_ROLES:
            raise ValueError(
                f"Unknown role '{role}'. Valid roles: {', '.join(VALID_ROLES)}"
            )
        with self._lock, self._connect() as conn:
            if provider_name:
                conn.execute(
                    """
                    INSERT INTO selections (role, provider_name, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(role) DO UPDATE SET
                        provider_name = excluded.provider_name,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (role, provider_name),
                )
            else:
                # Empty value -> clear the role (revert to auto-routing)
                conn.execute("DELETE FROM selections WHERE role = ?", (role,))
            conn.commit()

    def set_many(self, assignments: Dict[str, str]) -> None:
        for role, provider_name in assignments.items():
            self.set_role(role, provider_name)

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM selections")
            conn.commit()


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
_default_store: Optional[ProviderSelectionStore] = None
_singleton_lock = threading.Lock()


def get_provider_selection_store(
    db_path: Optional[str | Path] = None,
) -> ProviderSelectionStore:
    """Return a process-wide singleton ProviderSelectionStore."""
    global _default_store
    with _singleton_lock:
        if _default_store is None:
            if db_path is None:
                from omnicode.config.settings import _user_data_dir
                db_path = _user_data_dir() / "selections.db"
            _default_store = ProviderSelectionStore(db_path)
            logger.info("Provider selection store initialised at %s", db_path)
        return _default_store


def reset_provider_selection_store() -> None:
    """Clear the cached singleton (used on working-directory switches)."""
    global _default_store
    with _singleton_lock:
        _default_store = None


def list_valid_roles() -> List[str]:
    """Return the canonical list of routing roles."""
    return list(VALID_ROLES)
