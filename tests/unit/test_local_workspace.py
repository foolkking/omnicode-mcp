"""Tests for the local workspace path model."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.workspace.local import LocalWorkspace, WorkspacePathError


def test_relative_paths_normalize_to_posix(tmp_path: Path) -> None:
    ws = LocalWorkspace(root=tmp_path, workspace_id="repo-a")

    assert ws.normalize_rel("src/app.py") == "src/app.py"
    assert ws.normalize_rel(r".\src\app.py") == "src/app.py"
    assert ws.to_absolute(r".\src\app.py") == (tmp_path / "src" / "app.py").resolve()


def test_absolute_inside_path_converts_to_relative(tmp_path: Path) -> None:
    file = tmp_path / "src" / "app.py"
    file.parent.mkdir()
    file.write_text("print('ok')\n", encoding="utf-8")
    ws = LocalWorkspace(root=tmp_path, workspace_id="repo-a")

    assert ws.to_relative(file) == "src/app.py"


@pytest.mark.parametrize("bad", ["../secret.py", "src/../../secret.py", "", "."])
def test_escape_and_empty_paths_are_rejected(tmp_path: Path, bad: str) -> None:
    ws = LocalWorkspace(root=tmp_path, workspace_id="repo-a")

    with pytest.raises(WorkspacePathError):
        ws.normalize_rel(bad)


def test_absolute_outside_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ws = LocalWorkspace(root=root, workspace_id="repo-a")
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(WorkspacePathError):
        ws.to_relative(outside)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = True\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    ws = LocalWorkspace(root=root, workspace_id="repo-a")
    with pytest.raises(WorkspacePathError):
        ws.to_absolute("link/secret.py")
