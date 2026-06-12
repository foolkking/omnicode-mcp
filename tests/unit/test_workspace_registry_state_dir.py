from __future__ import annotations

from pathlib import Path

from omnicode_core.workspace import registry


def test_default_workspace_registry_uses_state_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("OMNICODE_WORKSPACE_REGISTRY", raising=False)
    registry._DEFAULT_REGISTRY = None
    registry._DEFAULT_REGISTRY_PATH = None

    store = registry.get_workspace_registry()

    assert store.store_path == state_dir / "workspaces.json"


def test_explicit_workspace_registry_overrides_state_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "custom" / "workspaces.json"
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_REGISTRY", str(explicit))
    registry._DEFAULT_REGISTRY = None
    registry._DEFAULT_REGISTRY_PATH = None

    store = registry.get_workspace_registry()

    assert store.store_path == explicit
