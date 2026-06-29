"""Tests for the LLM / embedding / diagnostics capability contract."""

from __future__ import annotations

from pathlib import Path

from omnicode_core.config.capabilities import build_capability_contract
from omnicode_core.config.runtime import RuntimeConfig


def _runtime(**overrides) -> RuntimeConfig:
    values = {
        "workspace_root": Path.cwd(),
        "workspace_id": "repo-a",
        "executor": "hybrid",
        "backend_url": "http://cloud",
        "llm_mode": "off",
        "embedding_mode": "cloud",
        "diagnostics_mode": "local-first",
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_off_llm_is_explicitly_unavailable() -> None:
    contract = build_capability_contract(_runtime(llm_mode="off"))

    assert contract.llm.target == "off"
    assert contract.llm.available is False
    assert contract.llm.reason == "LLM mode is off"


def test_local_llm_uses_probe_result() -> None:
    available = build_capability_contract(
        _runtime(llm_mode="local"),
        local_llm_available=True,
    )
    missing = build_capability_contract(
        _runtime(llm_mode="local"),
        local_llm_available=False,
    )

    assert available.llm.target == "local"
    assert available.llm.available is True
    assert missing.llm.target == "local"
    assert missing.llm.available is False


def test_remote_llm_requires_cloud_backend() -> None:
    ok = build_capability_contract(_runtime(llm_mode="remote"))
    missing = build_capability_contract(
        _runtime(llm_mode="remote", backend_url=None)
    )

    assert ok.llm.target == "cloud"
    assert ok.llm.requires_cloud is True
    assert ok.llm.available is True
    assert missing.llm.target == "cloud"
    assert missing.llm.available is False


def test_auto_llm_prefers_local_then_cloud_then_off() -> None:
    local = build_capability_contract(
        _runtime(llm_mode="auto"),
        local_llm_available=True,
    )
    cloud = build_capability_contract(
        _runtime(llm_mode="auto"),
        local_llm_available=False,
    )
    off = build_capability_contract(
        _runtime(llm_mode="auto", backend_url=None),
        local_llm_available=False,
    )

    assert local.llm.target == "local"
    assert cloud.llm.target == "cloud"
    assert off.llm.target == "off"


def test_embedding_cloud_requires_cloud_backend() -> None:
    ok = build_capability_contract(_runtime(embedding_mode="cloud"))
    missing = build_capability_contract(
        _runtime(embedding_mode="cloud", backend_url=None)
    )

    assert ok.embedding.target == "cloud"
    assert ok.embedding.available is True
    assert missing.embedding.target == "cloud"
    assert missing.embedding.available is False


def test_embedding_cloud_uses_runtime_probe_when_known() -> None:
    available = build_capability_contract(
        _runtime(embedding_mode="cloud"),
        cloud_embedding_available=True,
    )
    missing = build_capability_contract(
        _runtime(embedding_mode="cloud"),
        cloud_embedding_available=False,
    )

    assert available.embedding.target == "cloud"
    assert available.embedding.available is True
    assert available.embedding.reason == "cloud embedding backend is available"
    assert missing.embedding.target == "cloud"
    assert missing.embedding.available is False
    assert missing.embedding.reason == "cloud embedding backend is unavailable"


def test_diagnostics_local_first_is_available_without_cloud() -> None:
    contract = build_capability_contract(
        _runtime(diagnostics_mode="local-first", backend_url=None)
    )

    assert contract.diagnostics.target == "local"
    assert contract.diagnostics.available is True
    assert contract.diagnostics.local_allowed is True
    assert contract.diagnostics.cloud_allowed is False


def test_contract_to_dict_is_stable() -> None:
    data = build_capability_contract(_runtime()).to_dict()

    assert data["executor"] == "hybrid"
    assert data["cloud_configured"] is True
    assert data["llm"]["name"] == "llm"
    assert data["embedding"]["name"] == "embedding"
    assert data["diagnostics"]["name"] == "diagnostics"
