"""Tests for the cloud-side snapshot store."""

from __future__ import annotations

import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    SnapshotStoreError,
    default_snapshot_store_path,
    default_workspace_store_path,
    normalize_snapshot_path,
)


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_upsert_persists_content_and_index(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)
    content = "print('hello')\n"

    record = store.upsert(
        workspace_id="repo-a",
        path="src\\app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=7,
    )

    assert record.path == "src/app.py"
    assert record.revision == 7
    assert (tmp_path / "workspaces" / "repo-a" / "index.json").is_file()
    assert store.read_text(workspace_id="repo-a", path="src/app.py") == content
    assert store.status("repo-a") == {
        "workspace_id": "repo-a",
        "latest_revision": 7,
        "accepted_revision": 7,
        "indexed_revision": 0,
        "semantic_index_coverage": "unknown",
        "semantic_initial_exact_only": False,
        "file_count": 1,
        "delete_count": 0,
    }


def test_delete_removes_logical_file_but_keeps_revision(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)
    content = "x = 1\n"
    store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=2,
    )

    store.delete(workspace_id="repo-a", path="src/app.py", revision=3)

    assert store.read_text(workspace_id="repo-a", path="src/app.py") is None
    assert store.status("repo-a")["latest_revision"] == 3
    assert store.status("repo-a")["accepted_revision"] == 3
    assert store.status("repo-a")["file_count"] == 0
    assert store.status("repo-a")["delete_count"] == 1


def test_reloads_existing_index(tmp_path: Path) -> None:
    content = "x = 1\n"
    CloudSnapshotStore(root=tmp_path).upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=2,
    )

    reloaded = CloudSnapshotStore(root=tmp_path)

    assert reloaded.read_text(workspace_id="repo-a", path="src/app.py") == content
    assert reloaded.status("repo-a")["latest_revision"] == 2


def test_mark_indexed_revision_is_persisted(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)
    content = "x = 1\n"
    store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=4,
    )

    assert store.mark_indexed(workspace_id="repo-a", revision=3) == 3
    assert CloudSnapshotStore(root=tmp_path).status("repo-a")["indexed_revision"] == 3

    with pytest.raises(SnapshotStoreError, match="exceeds accepted"):
        store.mark_indexed(workspace_id="repo-a", revision=5)


def test_mark_indexed_persists_semantic_coverage(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)
    content = "x = 1\n"
    store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=4,
    )

    assert (
        store.mark_indexed(
            workspace_id="repo-a",
            revision=4,
            semantic_coverage="exact_only_initial_sync",
        )
        == 4
    )
    status = CloudSnapshotStore(root=tmp_path).status("repo-a")
    assert status["indexed_revision"] == 4
    assert status["semantic_index_coverage"] == "exact_only_initial_sync"
    assert status["semantic_initial_exact_only"] is True

    assert (
        store.mark_indexed(
            workspace_id="repo-a",
            revision=4,
            semantic_coverage="selected_files",
        )
        == 4
    )
    status = CloudSnapshotStore(root=tmp_path).status("repo-a")
    assert status["semantic_index_coverage"] == "selected_files"
    assert status["semantic_initial_exact_only"] is False

    assert (
        store.mark_indexed(
            workspace_id="repo-a",
            revision=4,
            semantic_coverage="semantic_full",
        )
        == 4
    )
    status = CloudSnapshotStore(root=tmp_path).status("repo-a")
    assert status["semantic_index_coverage"] == "semantic_full"
    assert status["semantic_initial_exact_only"] is False


def test_mark_indexed_promotes_filtered_after_exact_only(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)
    content = "x = 1\n"
    store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=4,
    )

    store.mark_indexed(
        workspace_id="repo-a",
        revision=4,
        semantic_coverage="exact_only_initial_sync",
    )
    store.mark_indexed(
        workspace_id="repo-a",
        revision=4,
        semantic_coverage="filtered",
    )

    status = CloudSnapshotStore(root=tmp_path).status("repo-a")
    assert status["semantic_index_coverage"] == "filtered"
    assert status["semantic_initial_exact_only"] is False


def test_apply_batch_serializes_concurrent_workspace_writes(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path, materialize_mirror=True)

    def _push(idx: int) -> int:
        content = f'VALUE_{idx} = "r30-{idx}"\n'
        result = store.apply_batch(
            workspace_id="repo-a",
            files=[
                {
                    "path": f"tests/tmp_cloudsim_r30_{idx}.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": idx,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            deletes=[],
            revision=idx + 1,
        )
        return result.accepted_revision

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(_push, range(12)))

    assert max(accepted) == 12
    assert store.status("repo-a")["accepted_revision"] == 12
    assert store.status("repo-a")["file_count"] == 12
    for idx in range(12):
        path = f"tests/tmp_cloudsim_r30_{idx}.py"
        assert store.read_text(workspace_id="repo-a", path=path) == (
            f'VALUE_{idx} = "r30-{idx}"\n'
        )


def test_snapshot_index_reads_are_serialized_with_writes(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path, materialize_mirror=True)

    def _push(idx: int) -> int:
        content = f'VALUE_{idx} = "r30-read-write-{idx}"\n'
        result = store.apply_batch(
            workspace_id="repo-a",
            files=[
                {
                    "path": f"tests/tmp_cloudsim_r30_rw_{idx}.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": idx,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            deletes=[],
            revision=idx + 1,
        )
        return result.accepted_revision

    def _read(_idx: int) -> int:
        status = store.status("repo-a")
        store.file_hashes("repo-a")
        store.list_records(workspace_id="repo-a")
        store.read_text(
            workspace_id="repo-a",
            path="tests/tmp_cloudsim_r30_rw_0.py",
        )
        return int(status["accepted_revision"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            *(pool.submit(_push, idx) for idx in range(16)),
            *(pool.submit(_read, idx) for idx in range(32)),
        ]
        for future in futures:
            future.result()

    assert store.status("repo-a")["accepted_revision"] == 16
    assert store.status("repo-a")["file_count"] == 16


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", ""])
def test_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(SnapshotStoreError):
        normalize_snapshot_path(path)


def test_hash_and_size_are_checked(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path)

    with pytest.raises(SnapshotStoreError, match="hash mismatch"):
        store.upsert(
            workspace_id="repo-a",
            path="src/app.py",
            content="x = 1\n",
            hash_value="sha256:wrong",
            size=6,
            mtime_ms=123,
            encoding="utf-8",
            revision=1,
        )


def test_default_path_uses_workspace_store_before_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    workspace_store = tmp_path / "custom-workspaces"
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("OMNICODE_WORKSPACE_STORE", raising=False)

    assert default_snapshot_store_path() == state_dir / "cloud-sync"
    assert default_workspace_store_path() == state_dir / "cloud-sync" / "workspaces"

    monkeypatch.setenv("OMNICODE_WORKSPACE_STORE", str(workspace_store))

    assert default_snapshot_store_path() == state_dir / "cloud-sync"
    assert default_workspace_store_path() == workspace_store
    assert CloudSnapshotStore()._workspace_dir("repo-a") == workspace_store / "repo-a"


def test_upsert_materializes_readonly_mirror_and_delete_removes_it(
    tmp_path: Path,
) -> None:
    store = CloudSnapshotStore(
        root=tmp_path,
        materialize_mirror=True,
        mirror_readonly=True,
    )
    content = "x = 1\n"

    record = store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=2,
    )

    mirror = tmp_path / "workspaces" / "repo-a" / "mirror" / "src" / "app.py"
    assert record.mirror_path == "mirror/src/app.py"
    assert mirror.read_text(encoding="utf-8") == content
    assert mirror.stat().st_mode & stat.S_IWRITE == 0

    updated = "x = 2\n"
    updated_record = store.upsert(
        workspace_id="repo-a",
        path="src/app.py",
        content=updated,
        hash_value=_sha(updated),
        size=len(updated),
        mtime_ms=124,
        encoding="utf-8",
        revision=3,
    )

    assert updated_record.mirror_path == "mirror/src/app.py"
    assert mirror.read_text(encoding="utf-8") == updated
    assert mirror.stat().st_mode & stat.S_IWRITE == 0

    store.delete(workspace_id="repo-a", path="src/app.py", revision=4)

    assert not mirror.exists()
    assert store.status("repo-a")["file_count"] == 0

    with pytest.raises(SnapshotStoreError, match="size mismatch"):
        store.upsert(
            workspace_id="repo-a",
            path="src/app.py",
            content="x = 1\n",
            hash_value=_sha("x = 1\n"),
            size=999,
            mtime_ms=123,
            encoding="utf-8",
            revision=1,
        )
