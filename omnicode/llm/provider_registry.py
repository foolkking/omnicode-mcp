"""
Provider Registry (External API Integration)
============================================

A SQLite-backed registry of LLM provider configurations.

Each entry describes how to connect to a LiteLLM-compatible model:
  * name        — unique identifier used by the router (e.g. "my-azure-gpt4")
  * model       — LiteLLM model string (e.g. "azure/gpt-4o", "openai/gpt-4o",
                  "ollama/llama3", "claude-3-opus-20240229")
  * api_key     — secret token (stored in plain text in the local DB; this is
                  a single-user developer tool, not a multi-tenant service)
  * api_base    — optional custom base URL for self-hosted / proxy endpoints
  * provider_type — informational tag: openai-compatible / anthropic / gemini /
                  ollama / azure / bedrock / custom
  * group       — routing group: "quality" / "cost" / "balanced"
  * extra_headers — optional JSON object of headers to send with every request
  * enabled     — whether the provider participates in routing
  * built_in    — True for env-var-derived providers (Anthropic/OpenAI/Gemini/
                  DeepSeek), False for user-added customs.  Built-ins cannot
                  be deleted but can be disabled.

The registry survives restarts and supports hot-add via LLMRouter.add_provider.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .secret_box import SecretBox

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------
@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""

    name: str
    model: str
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    provider_type: str = "openai-compatible"
    group: str = "balanced"  # quality / cost / balanced
    extra_headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    built_in: bool = False
    description: str = ""

    def to_dict(self, redact_secret: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        if redact_secret and d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = (
                f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "***"
            )
            d["api_key_set"] = True
        else:
            d["api_key_set"] = bool(d.get("api_key"))
        return d


# ---------------------------------------------------------------------------
# SQLite-backed registry
# ---------------------------------------------------------------------------
class ProviderRegistry:
    """Thread-safe SQLite-backed provider registry."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS providers (
        name           TEXT PRIMARY KEY,
        model          TEXT NOT NULL,
        api_key        TEXT,
        api_base       TEXT,
        provider_type  TEXT NOT NULL DEFAULT 'openai-compatible',
        group_name     TEXT NOT NULL DEFAULT 'balanced',
        extra_headers  TEXT NOT NULL DEFAULT '{}',
        enabled        INTEGER NOT NULL DEFAULT 1,
        built_in       INTEGER NOT NULL DEFAULT 0,
        description    TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    def __init__(
        self,
        db_path: str | Path,
        secret_box: Optional["SecretBox"] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Lazy import so importing this module doesn't fail when the
        # cryptography stack isn't installed yet.
        from .secret_box import get_secret_box  # noqa: PLC0415

        if secret_box is None:
            # Default to a key sitting next to the db file.
            key_path = self.db_path.parent / (self.db_path.stem + ".key")
            secret_box = get_secret_box(key_path)
        self._box: SecretBox = secret_box
        self._init_db()

    # ------------------------------------------------------------------ db
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(self._SCHEMA)
            conn.commit()
        # Best-effort: re-encrypt any plain-text rows lying around from
        # before STAGE 2.13 was wired in.
        self._encrypt_legacy_rows()

    def _encrypt_legacy_rows(self) -> None:
        """Walk existing rows and migrate plain-text api_key values to ciphertext.

        Idempotent: rows already wrapped with the ENCRYPTED_PREFIX are
        skipped, so this is safe to run on every startup.
        """
        if not self._box.available:
            return
        from .secret_box import SecretBox  # noqa: PLC0415

        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT name, api_key FROM providers WHERE api_key IS NOT NULL"
                ).fetchall()
                migrated = 0
                for row in rows:
                    raw = row["api_key"]
                    if raw and not SecretBox.is_encrypted(raw):
                        encrypted = self._box.encrypt(raw)
                        if encrypted and encrypted != raw:
                            conn.execute(
                                "UPDATE providers SET api_key = ? WHERE name = ?",
                                (encrypted, row["name"]),
                            )
                            migrated += 1
                if migrated:
                    conn.commit()
                    logger.info(
                        "Provider registry: migrated %d plain-text api_key value(s) to encrypted form",
                        migrated,
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Legacy api_key migration skipped: %s", exc)

    # ------------------------------------------------------------------ row helpers
    def _row_to_config(self, row: sqlite3.Row) -> ProviderConfig:
        try:
            extra_headers = json.loads(row["extra_headers"] or "{}")
            if not isinstance(extra_headers, dict):
                extra_headers = {}
        except Exception:
            extra_headers = {}
        # Decrypt api_key on the way out — the rest of the app sees plain text.
        api_key = self._box.decrypt(row["api_key"])
        return ProviderConfig(
            name=row["name"],
            model=row["model"],
            api_key=api_key,
            api_base=row["api_base"],
            provider_type=row["provider_type"],
            group=row["group_name"],
            extra_headers=extra_headers,
            enabled=bool(row["enabled"]),
            built_in=bool(row["built_in"]),
            description=row["description"] or "",
        )

    # ------------------------------------------------------------------ CRUD
    def list_providers(self) -> List[ProviderConfig]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM providers ORDER BY built_in DESC, name ASC"
            ).fetchall()
        return [self._row_to_config(r) for r in rows]

    def get(self, name: str) -> Optional[ProviderConfig]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE name = ?", (name,)
            ).fetchone()
        return self._row_to_config(row) if row else None

    def upsert(self, cfg: ProviderConfig) -> ProviderConfig:
        # Encrypt the api_key before it touches the DB.
        encrypted_key = self._box.encrypt(cfg.api_key)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO providers
                  (name, model, api_key, api_base, provider_type, group_name,
                   extra_headers, enabled, built_in, description, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET
                  model         = excluded.model,
                  api_key       = excluded.api_key,
                  api_base      = excluded.api_base,
                  provider_type = excluded.provider_type,
                  group_name    = excluded.group_name,
                  extra_headers = excluded.extra_headers,
                  enabled       = excluded.enabled,
                  built_in      = excluded.built_in,
                  description   = excluded.description,
                  updated_at    = CURRENT_TIMESTAMP
                """,
                (
                    cfg.name,
                    cfg.model,
                    encrypted_key,
                    cfg.api_base,
                    cfg.provider_type,
                    cfg.group,
                    json.dumps(cfg.extra_headers or {}),
                    1 if cfg.enabled else 0,
                    1 if cfg.built_in else 0,
                    cfg.description or "",
                ),
            )
            conn.commit()
        return cfg

    def delete(self, name: str, *, force: bool = False) -> bool:
        """Delete a provider row.

        Built-in providers are protected by default — pass ``force=True`` to
        delete them too (used by the router on startup to clean up stale
        built-ins whose env keys have become placeholders).
        """
        with self._lock, self._connect() as conn:
            if force:
                cur = conn.execute(
                    "DELETE FROM providers WHERE name = ?", (name,)
                )
            else:
                cur = conn.execute(
                    "DELETE FROM providers WHERE name = ? AND built_in = 0", (name,)
                )
            conn.commit()
            return cur.rowcount > 0

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE providers SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE name = ?",
                (1 if enabled else 0, name),
            )
            conn.commit()
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
_default_registry: Optional[ProviderRegistry] = None
_singleton_lock = threading.Lock()


def get_provider_registry(db_path: Optional[str | Path] = None) -> ProviderRegistry:
    """Return a process-wide singleton ProviderRegistry."""
    global _default_registry
    with _singleton_lock:
        if _default_registry is None:
            if db_path is None:
                # Default — only used as a last resort.  Lifespan resolves the
                # real path via :func:`omnicode.config.settings.resolve_provider_db_path`
                # so the user-level shared DB at ``~/.kiro/codebase-mcp/providers.db``
                # is preferred.
                from omnicode.config.settings import _user_data_dir
                db_path = _user_data_dir() / "providers.db"
            _default_registry = ProviderRegistry(db_path)
            logger.info("Provider registry initialised at %s", db_path)
        return _default_registry


def reset_provider_registry() -> None:
    """Clear the cached singleton.

    Called when switching working directories so the next
    ``get_provider_registry()`` call rebuilds the registry against the
    newly resolved DB path.
    """
    global _default_registry
    with _singleton_lock:
        _default_registry = None
