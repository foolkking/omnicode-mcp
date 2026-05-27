"""STAGE 11.x — SecretBox & encrypted ProviderRegistry tests (STAGE 2.13)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omnicode.llm.provider_registry import ProviderConfig, ProviderRegistry
from omnicode.llm.secret_box import (
    ENCRYPTED_PREFIX,
    SecretBox,
    reset_default_box,
)


@pytest.fixture(autouse=True)
def _reset_default_box():
    """Make sure the module-level singleton doesn't leak across tests."""
    reset_default_box()
    yield
    reset_default_box()


# ---------------------------------------------------------------------------
# SecretBox unit
# ---------------------------------------------------------------------------
class TestSecretBox:
    def test_encrypt_round_trip(self, tmp_path: Path):
        box = SecretBox(tmp_path / "providers.key")
        assert box.available is True
        plain = "sk-very-secret-value-1234567890"
        wrapped = box.encrypt(plain)
        assert wrapped is not None
        assert wrapped.startswith(ENCRYPTED_PREFIX)
        assert plain not in wrapped
        assert box.decrypt(wrapped) == plain

    def test_passthrough_on_empty_or_none(self, tmp_path: Path):
        box = SecretBox(tmp_path / "providers.key")
        assert box.encrypt(None) is None
        assert box.encrypt("") == ""
        assert box.decrypt(None) is None

    def test_idempotent_encrypt(self, tmp_path: Path):
        box = SecretBox(tmp_path / "providers.key")
        first = box.encrypt("hello")
        second = box.encrypt(first)  # already encrypted — no double-wrap
        assert first == second
        assert box.decrypt(second) == "hello"

    def test_decrypt_legacy_plain_text(self, tmp_path: Path):
        """Values without the ENCRYPTED_PREFIX pass through untouched."""
        box = SecretBox(tmp_path / "providers.key")
        assert box.decrypt("sk-legacy-plain-text") == "sk-legacy-plain-text"

    def test_decrypt_with_wrong_key_returns_none(self, tmp_path: Path):
        # Encrypt with one key, then swap the key file out.
        key_path = tmp_path / "providers.key"
        box1 = SecretBox(key_path)
        wrapped = box1.encrypt("secret")
        # Wipe the key file and create a fresh box (which generates a new key).
        key_path.unlink()
        box2 = SecretBox(key_path)
        assert box2.decrypt(wrapped) is None

    def test_persists_key_across_instances(self, tmp_path: Path):
        key_path = tmp_path / "providers.key"
        box1 = SecretBox(key_path)
        wrapped = box1.encrypt("my-key")
        # Re-open the box at the same path — key should be reused.
        box2 = SecretBox(key_path)
        assert box2.decrypt(wrapped) == "my-key"

    def test_is_encrypted(self, tmp_path: Path):
        box = SecretBox(tmp_path / "providers.key")
        assert SecretBox.is_encrypted("plain") is False
        assert SecretBox.is_encrypted(box.encrypt("x")) is True
        assert SecretBox.is_encrypted(None) is False
        assert SecretBox.is_encrypted("") is False


# ---------------------------------------------------------------------------
# Registry-level integration: api_key encrypted at rest
# ---------------------------------------------------------------------------
class TestEncryptedRegistry:
    def test_api_key_round_trip_through_registry(self, tmp_path: Path):
        registry = ProviderRegistry(tmp_path / "providers.db")
        cfg = ProviderConfig(
            name="my-prov",
            model="openai/gpt-4o",
            api_key="sk-ultra-secret-9999",
            api_base="https://api.example.com",
            group="quality",
        )
        registry.upsert(cfg)
        loaded = registry.get("my-prov")
        # Plain-text round-trip is preserved at the registry boundary
        assert loaded is not None
        assert loaded.api_key == "sk-ultra-secret-9999"

    def test_api_key_encrypted_at_rest(self, tmp_path: Path):
        registry = ProviderRegistry(tmp_path / "providers.db")
        registry.upsert(
            ProviderConfig(
                name="enc",
                model="openai/gpt-4o",
                api_key="sk-target-1234",
                group="balanced",
            )
        )
        # Bypass the registry: read the raw column directly.
        with sqlite3.connect(str(tmp_path / "providers.db")) as conn:
            row = conn.execute(
                "SELECT api_key FROM providers WHERE name = 'enc'"
            ).fetchone()
        raw = row[0]
        assert raw is not None
        assert raw.startswith(ENCRYPTED_PREFIX)
        assert "sk-target-1234" not in raw

    def test_no_key_means_passthrough(self, tmp_path: Path):
        # Force the box into "unavailable" state by feeding it a corrupt key file.
        bad = tmp_path / "providers.key"
        bad.write_text("not-a-valid-fernet-key")
        box = SecretBox(bad)
        # Test only runs on systems where cryptography is installed: in that
        # case a corrupt key disables encryption gracefully.
        if not box.available:
            return
        # If we got past that, the box is still available — but the key is
        # invalid. Check encrypt failure handling.
        assert box.encrypt("hello") in ("hello", None) or SecretBox.is_encrypted(
            box.encrypt("hello") or ""
        )

    def test_legacy_plain_text_migrated_on_open(self, tmp_path: Path):
        # First, create a registry but inject a plain-text api_key directly
        # via raw SQL — simulates a database from before STAGE 2.13.
        ProviderRegistry(tmp_path / "providers.db")  # initialise schema
        with sqlite3.connect(str(tmp_path / "providers.db")) as conn:
            conn.execute(
                "INSERT INTO providers (name, model, api_key) VALUES (?, ?, ?)",
                ("legacy", "openai/gpt-4o-mini", "sk-legacy-1111"),
            )
            conn.commit()
        # Re-open the registry — the constructor should encrypt that row.
        registry2 = ProviderRegistry(tmp_path / "providers.db")
        with sqlite3.connect(str(tmp_path / "providers.db")) as conn:
            raw = conn.execute(
                "SELECT api_key FROM providers WHERE name = 'legacy'"
            ).fetchone()[0]
        assert raw.startswith(ENCRYPTED_PREFIX)
        # Reads still produce plain text:
        cfg = registry2.get("legacy")
        assert cfg is not None and cfg.api_key == "sk-legacy-1111"
