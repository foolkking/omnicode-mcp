"""STAGE 11.2 — Unit tests for ProviderRegistry + ProviderSelectionStore."""

from __future__ import annotations

import pytest

from omnicode.llm.provider_registry import ProviderConfig, ProviderRegistry
from omnicode.llm.provider_selection import (
    VALID_ROLES,
    ProviderSelectionStore,
)


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------
class TestProviderRegistry:
    def test_upsert_and_list(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        cfg = ProviderConfig(
            name="my-provider",
            model="openai/gpt-4o",
            api_key="sk-test-1234567890",
            api_base="https://api.example.com/v1",
            provider_type="openai-compatible",
            group="balanced",
            extra_headers={"X-Smoke": "1"},
        )
        registry.upsert(cfg)
        all_providers = registry.list_providers()
        assert len(all_providers) == 1
        assert all_providers[0].name == "my-provider"
        assert all_providers[0].api_key == "sk-test-1234567890"

    def test_redacted_dict(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        cfg = ProviderConfig(name="x", model="m", api_key="sk-abcdefghij1234")
        d = cfg.to_dict(redact_secret=True)
        assert d["api_key_set"] is True
        assert "abcdefghij1234" not in d["api_key"]
        assert d["api_key"].startswith("sk-a") and d["api_key"].endswith("1234")

    def test_get_returns_full_config(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        registry.upsert(ProviderConfig(name="a", model="m1", group="quality"))
        cfg = registry.get("a")
        assert cfg is not None and cfg.group == "quality"
        assert registry.get("does-not-exist") is None

    def test_upsert_updates_existing(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        registry.upsert(ProviderConfig(name="a", model="v1", group="quality"))
        registry.upsert(ProviderConfig(name="a", model="v2", group="cost"))
        cfg = registry.get("a")
        assert cfg.model == "v2"
        assert cfg.group == "cost"
        assert len(registry.list_providers()) == 1

    def test_delete_only_custom(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        registry.upsert(ProviderConfig(name="custom", model="m", built_in=False))
        registry.upsert(ProviderConfig(name="builtin", model="m", built_in=True))

        assert registry.delete("custom") is True
        assert registry.get("custom") is None
        # Built-ins must NOT be deletable.
        assert registry.delete("builtin") is False
        assert registry.get("builtin") is not None

    def test_set_enabled(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        registry.upsert(ProviderConfig(name="x", model="m", enabled=True))
        assert registry.set_enabled("x", False) is True
        assert registry.get("x").enabled is False
        # Toggling a missing one returns False rather than raising.
        assert registry.set_enabled("ghost", True) is False

    def test_extra_headers_persist_as_dict(self, tmp_db_path):
        registry = ProviderRegistry(tmp_db_path)
        registry.upsert(
            ProviderConfig(
                name="hdr",
                model="m",
                extra_headers={"Authorization": "Bearer x", "X-Tenant": "abc"},
            )
        )
        cfg = registry.get("hdr")
        assert cfg.extra_headers == {"Authorization": "Bearer x", "X-Tenant": "abc"}

    def test_corrupted_extra_headers_falls_back_to_empty(self, tmp_db_path):
        """If somehow extra_headers contains non-dict JSON, we should not crash."""
        registry = ProviderRegistry(tmp_db_path)
        # Insert manually via the underlying connection to bypass dataclass
        # validation and simulate DB corruption.
        with registry._connect() as conn:
            conn.execute(
                "INSERT INTO providers (name, model, extra_headers) VALUES (?, ?, ?)",
                ("bad", "m", '"not-an-object"'),
            )
            conn.commit()
        cfg = registry.get("bad")
        assert cfg is not None and cfg.extra_headers == {}


# ---------------------------------------------------------------------------
# ProviderSelectionStore
# ---------------------------------------------------------------------------
class TestProviderSelectionStore:
    def test_default_empty(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        sel = store.get_all()
        assert sel.assignments == {}
        assert sel.get("default") is None

    def test_set_and_get_role(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        store.set_role("edit", "claude")
        sel = store.get_all()
        assert sel.assignments == {"edit": "claude"}
        assert sel.get("edit") == "claude"

    def test_default_falls_back(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        store.set_role("default", "gemini")
        sel = store.get_all()
        # An unset role should resolve via the default mapping.
        assert sel.get("scan") == "gemini"
        assert sel.get("default") == "gemini"

    def test_clear_role(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        store.set_role("edit", "claude")
        store.set_role("edit", "")  # empty value → clear
        assert store.get_all().assignments == {}

    def test_set_many(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        store.set_many({"edit": "claude", "scan": "deepseek", "review": "claude"})
        sel = store.get_all().assignments
        assert sel == {"edit": "claude", "scan": "deepseek", "review": "claude"}

    def test_invalid_role_raises(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        with pytest.raises(ValueError, match="Unknown role"):
            store.set_role("not-a-role", "claude")

    def test_clear_all(self, tmp_db_path):
        store = ProviderSelectionStore(tmp_db_path)
        store.set_many({"edit": "a", "scan": "b"})
        store.clear()
        assert store.get_all().assignments == {}

    def test_valid_roles_includes_canonical_set(self):
        # If new roles are added in code, this test will fail and remind the
        # author to keep the user-facing UI / docs in sync.
        canonical = {
            "default",
            "quality",
            "cost",
            "fastest",
            "edit",
            "scan",
            "review",
            "summary",
            "chat",
        }
        assert canonical.issubset(set(VALID_ROLES))
