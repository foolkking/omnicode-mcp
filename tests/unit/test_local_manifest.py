"""Tests for the local manifest sync state."""

from __future__ import annotations

from pathlib import Path

from omnicode_core.workspace.local import LocalWorkspace
from omnicode_core.workspace.manifest import LocalManifest


def _workspace(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root, workspace_id="repo-a")


def test_changed_file_enters_pending_queue(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('v1')\n", encoding="utf-8")
    manifest = LocalManifest.load(
        workspace=ws, path=tmp_path / "manifest.json",
    )

    change = manifest.mark_changed("src/app.py")

    assert change is not None
    assert change.op == "upsert"
    assert change.path == "src/app.py"
    assert manifest.local_revision == 1
    assert manifest.pending == [
        {"op": "upsert", "path": "src/app.py", "hash": change.hash}
    ]
    assert manifest.files["src/app.py"]["hash"] == change.hash


def test_same_hash_is_noop(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('v1')\n", encoding="utf-8")
    manifest = LocalManifest.load(
        workspace=ws, path=tmp_path / "manifest.json",
    )

    assert manifest.mark_changed("src/app.py") is not None
    manifest.pending.clear()
    assert manifest.mark_changed("src/app.py") is None

    assert manifest.local_revision == 1
    assert manifest.pending == []


def test_changed_hash_replaces_pending_for_same_path(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('v1')\n", encoding="utf-8")
    manifest = LocalManifest.load(
        workspace=ws, path=tmp_path / "manifest.json",
    )

    first = manifest.mark_changed("src/app.py")
    file.write_text("print('v2')\n", encoding="utf-8")
    second = manifest.mark_changed("src/app.py")

    assert first is not None and second is not None
    assert first.hash != second.hash
    assert manifest.local_revision == 2
    assert manifest.pending == [
        {"op": "upsert", "path": "src/app.py", "hash": second.hash}
    ]


def test_deleted_file_generates_delete_op(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('v1')\n", encoding="utf-8")
    manifest = LocalManifest.load(
        workspace=ws, path=tmp_path / "manifest.json",
    )

    manifest.mark_changed("src/app.py")
    manifest.pending.clear()
    file.unlink()
    change = manifest.mark_changed("src/app.py")

    assert change is not None
    assert change.op == "delete"
    assert change.hash is None
    assert manifest.pending == [{"op": "delete", "path": "src/app.py"}]
    assert "src/app.py" not in manifest.files


def test_ignore_paths_skip_changes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "dist" / "app.js"
    file.parent.mkdir()
    file.write_text("console.log('built')\n", encoding="utf-8")
    manifest = LocalManifest.load(
        workspace=ws,
        path=tmp_path / "manifest.json",
        ignore_paths=("dist/",),
    )

    assert manifest.mark_changed("dist/app.js") is None
    assert manifest.local_revision == 0
    assert manifest.pending == []


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    file = ws.root / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('v1')\n", encoding="utf-8")
    path = tmp_path / "manifest.json"
    manifest = LocalManifest.load(workspace=ws, path=path)
    manifest.mark_changed("src/app.py")
    manifest.save()

    loaded = LocalManifest.load(workspace=ws, path=path)

    assert loaded.local_revision == 1
    assert loaded.pending == manifest.pending
    assert loaded.files == manifest.files
