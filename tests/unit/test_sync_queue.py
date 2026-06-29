"""Tests for LocalManifest-backed SyncQueue."""

from __future__ import annotations

from pathlib import Path

from omnicode_core.workspace.local import LocalWorkspace
from omnicode_core.workspace.manifest import LocalManifest
from omnicode_core.workspace.sync_queue import SyncQueue


def _manifest(tmp_path: Path) -> LocalManifest:
    root = tmp_path / "repo"
    root.mkdir()
    ws = LocalWorkspace(root=root, workspace_id="repo-a")
    return LocalManifest.load(workspace=ws, path=tmp_path / "manifest.json")


def test_next_batch_builds_sync_payload(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    file = manifest.workspace.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('hello')\n", encoding="utf-8")
    change = manifest.mark_changed("src/app.py")
    assert change is not None

    queue = SyncQueue(manifest)
    batch = queue.next_batch()

    assert batch is not None
    payload = batch.to_payload()
    assert payload["client_id"] == manifest.data["client_id"]
    assert payload["base_revision"] == 0
    assert payload["client_revision"] == 1
    assert payload["files"][0]["path"] == "src/app.py"
    assert payload["files"][0]["hash"] == change.hash
    assert payload["files"][0]["content"].encode("utf-8") == file.read_bytes()
    assert payload["deletes"] == []


def test_delete_op_is_included_in_batch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    file = manifest.workspace.root / "src" / "old.py"
    file.parent.mkdir()
    file.write_text("old = True\n", encoding="utf-8")
    manifest.mark_changed("src/old.py")
    manifest.pending.clear()
    file.unlink()
    manifest.mark_changed("src/old.py")

    batch = SyncQueue(manifest).next_batch()

    assert batch is not None
    assert batch.files == []
    assert [d.path for d in batch.deletes] == ["src/old.py"]


def test_missing_pending_upsert_is_sent_as_delete(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    file = manifest.workspace.root / "src" / "gone.py"
    file.parent.mkdir()
    file.write_text("gone = True\n", encoding="utf-8")
    manifest.mark_changed("src/gone.py")
    file.unlink()

    batch = SyncQueue(manifest).next_batch()

    assert batch is not None
    assert batch.files == []
    assert [d.path for d in batch.deletes] == ["src/gone.py"]


def test_mark_accepted_removes_deleted_file_metadata(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    file = manifest.workspace.root / "src" / "gone.py"
    file.parent.mkdir()
    file.write_text("gone = True\n", encoding="utf-8")
    manifest.mark_changed("src/gone.py")
    file.unlink()
    queue = SyncQueue(manifest)
    batch = queue.next_batch()
    assert batch is not None

    queue.mark_accepted(batch, accepted_revision=2, indexed_revision=2)

    assert manifest.pending == []
    assert "src/gone.py" not in manifest.files


def test_next_batch_respects_max_files(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    for name in ("a.py", "b.py"):
        (manifest.workspace.root / name).write_text(name, encoding="utf-8")
        manifest.mark_changed(name)

    batch = SyncQueue(manifest).next_batch(max_files=1)

    assert batch is not None
    assert len(batch.files) == 1
    assert batch.files[0].path == "a.py"


def test_next_batch_respects_max_bytes_after_first_file(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (manifest.workspace.root / "a.py").write_text("aaaa", encoding="utf-8")
    (manifest.workspace.root / "b.py").write_text("bbbb", encoding="utf-8")
    manifest.mark_changed("a.py")
    manifest.mark_changed("b.py")

    batch = SyncQueue(manifest).next_batch(max_bytes=5)

    assert batch is not None
    assert [f.path for f in batch.files] == ["a.py"]


def test_mark_accepted_removes_only_sent_paths(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    for name in ("a.py", "b.py"):
        (manifest.workspace.root / name).write_text(name, encoding="utf-8")
        manifest.mark_changed(name)
    queue = SyncQueue(manifest)
    batch = queue.next_batch(max_files=1)
    assert batch is not None

    queue.mark_accepted(batch, accepted_revision=10, indexed_revision=9)

    assert manifest.data["last_accepted_revision"] == 10
    assert manifest.data["last_indexed_revision"] == 9
    assert manifest.pending == [
        {
            "op": "upsert",
            "path": "b.py",
            "hash": manifest.files["b.py"]["hash"],
        }
    ]
    assert manifest.files["a.py"]["last_uploaded_revision"] == 10


def test_mark_failed_preserves_pending(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (manifest.workspace.root / "a.py").write_text("a", encoding="utf-8")
    manifest.mark_changed("a.py")
    queue = SyncQueue(manifest)
    batch = queue.next_batch()
    assert batch is not None

    failure = queue.mark_failed(batch, error="network down")

    assert failure["ok"] is False
    assert failure["pending_preserved"] is True
    assert manifest.pending
