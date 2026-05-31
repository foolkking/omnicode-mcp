"""Unit tests for the user-level workspace registry (P2 step 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.workspace.registry import WorkspaceRegistry


@pytest.fixture
def reg(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(store_path=tmp_path / "workspaces.json")


def test_add_and_list_round_trip(reg: WorkspaceRegistry, tmp_path: Path):
    ws = reg.add(name="proj", path=str(tmp_path))
    assert ws.id.startswith("wk_")
    listed = reg.list()
    assert len(listed) == 1
    assert listed[0].path == str(tmp_path.resolve())


def test_add_accepts_stable_workspace_id(reg: WorkspaceRegistry, tmp_path: Path):
    ws = reg.add(name="proj", path=str(tmp_path), workspace_id="repo-a")
    assert ws.id == "repo-a"
    assert reg.get("repo-a").path == str(tmp_path.resolve())


def test_add_rejects_unsafe_workspace_id(reg: WorkspaceRegistry, tmp_path: Path):
    with pytest.raises(ValueError):
        reg.add(name="proj", path=str(tmp_path), workspace_id="../escape")


def test_add_rejects_non_directory(reg: WorkspaceRegistry, tmp_path: Path):
    bogus = tmp_path / "does-not-exist"
    with pytest.raises(NotADirectoryError):
        reg.add(name="x", path=str(bogus))


def test_add_dedupes_by_path(reg: WorkspaceRegistry, tmp_path: Path):
    a = reg.add(name="first", path=str(tmp_path))
    b = reg.add(name="second", path=str(tmp_path))
    assert a.id == b.id
    assert len(reg.list()) == 1


def test_set_active_flips_flag(reg: WorkspaceRegistry, tmp_path: Path):
    sub_a = tmp_path / "a"
    sub_b = tmp_path / "b"
    sub_a.mkdir()
    sub_b.mkdir()

    a = reg.add(name="a", path=str(sub_a), set_active=True)
    b = reg.add(name="b", path=str(sub_b))

    assert reg.get_active() is not None
    assert reg.get_active().id == a.id

    reg.set_active(b.id)
    assert reg.get_active().id == b.id
    # Only one active at a time
    assert sum(1 for w in reg.list() if w.active) == 1


def test_remove_promotes_first_remaining(reg: WorkspaceRegistry, tmp_path: Path):
    sub_a = tmp_path / "a"
    sub_b = tmp_path / "b"
    sub_a.mkdir()
    sub_b.mkdir()

    a = reg.add(name="a", path=str(sub_a), set_active=True)
    reg.add(name="b", path=str(sub_b))
    assert reg.remove(a.id) is True

    # The surviving one should be promoted to active so callers always
    # have a target.
    surviving = reg.list()
    assert len(surviving) == 1
    assert surviving[0].active is True


def test_remove_nonexistent_returns_false(reg: WorkspaceRegistry):
    assert reg.remove("wk_does_not_exist") is False


def test_rename(reg: WorkspaceRegistry, tmp_path: Path):
    ws = reg.add(name="old", path=str(tmp_path))
    out = reg.rename(ws.id, "new")
    assert out is not None
    assert out.name == "new"
    # Persisted
    assert reg.get(ws.id).name == "new"


def test_persistence_across_instances(tmp_path: Path):
    store = tmp_path / "workspaces.json"
    a = WorkspaceRegistry(store_path=store)
    a.add(name="proj", path=str(tmp_path), set_active=True)

    b = WorkspaceRegistry(store_path=store)
    items = b.list()
    assert len(items) == 1
    assert items[0].name == "proj"
    assert items[0].active is True
