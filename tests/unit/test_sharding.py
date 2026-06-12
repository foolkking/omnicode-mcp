"""Unit tests for per-workspace FAISS sharding (Wave 2 W2-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.index.sharding import (
    DEFAULT_SHARD_ID,
    auto_migrate_legacy,
    drop_shard,
    list_shards,
    resolve_shard_dir,
)


def test_resolve_shard_dir_creates_path(tmp_path: Path):
    out = resolve_shard_dir(tmp_path, "wk_abc")
    assert Path(out).is_dir()
    assert Path(out).name == "wk_abc"
    assert Path(out).parent.name == "shards"
    assert Path(out).parent.parent.name == ".data"


def test_resolve_shard_dir_default_when_blank(tmp_path: Path):
    out_a = resolve_shard_dir(tmp_path)
    out_b = resolve_shard_dir(tmp_path, "")
    out_c = resolve_shard_dir(tmp_path, "   ")
    assert out_a == out_b == out_c
    assert Path(out_a).name == DEFAULT_SHARD_ID


def test_resolve_shard_dir_idempotent(tmp_path: Path):
    a = resolve_shard_dir(tmp_path, "wk_x")
    b = resolve_shard_dir(tmp_path, "wk_x")
    assert a == b
    assert Path(a).is_dir()


def test_resolve_shard_dir_uses_state_dir_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "repo"
    state_dir = tmp_path / "state"
    workspace.mkdir()
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("OMNICODE_CONTENT_STORE", raising=False)
    monkeypatch.delenv("OMNICODE_SEARCH_STORE", raising=False)

    out = Path(resolve_shard_dir(workspace, "wk_state"))

    assert out == state_dir / "search-indexes" / "wk_state"
    assert out.is_dir()
    assert not (workspace / ".data" / "shards").exists()
    assert list(list_shards(workspace)) == ["wk_state"]


def test_resolve_shard_dir_prefers_explicit_search_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "repo"
    search_store = tmp_path / "custom-search"
    workspace.mkdir()
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_CONTENT_STORE", str(tmp_path / "content"))
    monkeypatch.setenv("OMNICODE_SEARCH_STORE", str(search_store))

    out = Path(resolve_shard_dir(workspace, "wk_search"))

    assert out == search_store / "wk_search"
    assert out.is_dir()


def test_auto_migrate_skips_when_external_state_dir_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "repo"
    legacy = workspace / ".data"
    legacy.mkdir(parents=True)
    (legacy / "vector_store.faiss").write_bytes(b"FAKEFAISS")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("OMNICODE_SEARCH_STORE", raising=False)
    monkeypatch.delenv("OMNICODE_CONTENT_STORE", raising=False)

    report = auto_migrate_legacy(workspace)

    assert report["skipped"] == "external shard root configured"
    assert (legacy / "vector_store.faiss").read_bytes() == b"FAKEFAISS"
    assert report["default_shard_dir"] == str(
        state_dir / "search-indexes" / DEFAULT_SHARD_ID
    )
    assert not (state_dir / "search-indexes").exists()


def test_auto_migrate_legacy_moves_known_files(tmp_path: Path):
    legacy = tmp_path / ".data"
    legacy.mkdir()
    (legacy / "vector_store.faiss").write_bytes(b"FAKEFAISS")
    (legacy / "vector_store.db").write_bytes(b"SQLITE3-FAKE")
    (legacy / "file_tracker.db").write_bytes(b"")
    (legacy / "snapshots").mkdir()
    (legacy / "snapshots" / "snap1").write_text("hi", encoding="utf-8")

    report = auto_migrate_legacy(tmp_path)
    assert report["migrated_files"] == 3
    assert report["migrated_dirs"] == 1
    default_dir = Path(report["default_shard_dir"])
    assert (default_dir / "vector_store.faiss").read_bytes() == b"FAKEFAISS"
    assert (default_dir / "snapshots" / "snap1").read_text(encoding="utf-8") == "hi"
    # Legacy paths are gone after the move.
    assert not (legacy / "vector_store.faiss").exists()


def test_auto_migrate_idempotent_when_default_already_populated(tmp_path: Path):
    (tmp_path / ".data").mkdir()
    (tmp_path / ".data" / "vector_store.faiss").write_bytes(b"x")
    # Pre-create the default shard with a file → migration must skip.
    default_dir = Path(resolve_shard_dir(tmp_path, DEFAULT_SHARD_ID))
    (default_dir / "marker").write_text("kept", encoding="utf-8")

    report = auto_migrate_legacy(tmp_path)
    assert report["skipped"] == "default shard already populated"
    # Original file still in place.
    assert (tmp_path / ".data" / "vector_store.faiss").is_file()


def test_auto_migrate_no_data_dir(tmp_path: Path):
    report = auto_migrate_legacy(tmp_path)
    assert report["skipped"] == "no .data"


def test_drop_shard_refuses_default(tmp_path: Path):
    resolve_shard_dir(tmp_path, DEFAULT_SHARD_ID)
    with pytest.raises(ValueError):
        drop_shard(tmp_path, DEFAULT_SHARD_ID)
    with pytest.raises(ValueError):
        drop_shard(tmp_path, "")


def test_drop_shard_removes_named_shard(tmp_path: Path):
    target = Path(resolve_shard_dir(tmp_path, "wk_remove_me"))
    (target / "vector_store.faiss").write_bytes(b"x")
    assert target.is_dir()
    assert drop_shard(tmp_path, "wk_remove_me") is True
    assert not target.exists()


def test_drop_shard_returns_false_when_missing(tmp_path: Path):
    # Create the shards root but not the shard.
    resolve_shard_dir(tmp_path, "decoy")
    assert drop_shard(tmp_path, "wk_does_not_exist") is False


def test_list_shards(tmp_path: Path):
    resolve_shard_dir(tmp_path, "wk_a")
    resolve_shard_dir(tmp_path, "wk_b")
    resolve_shard_dir(tmp_path, DEFAULT_SHARD_ID)
    out = list(list_shards(tmp_path))
    assert sorted(out) == ["default", "wk_a", "wk_b"]


def test_list_shards_no_dir(tmp_path: Path):
    assert list(list_shards(tmp_path)) == []
