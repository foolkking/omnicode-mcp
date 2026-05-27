"""Unit tests for the workspace path sandbox (Wave 1, gap §13)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnicode_core.security.sandbox import (
    WorkspacePathError,
    ensure_within_workspace,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    sub = tmp_path / "project"
    sub.mkdir()
    (sub / "ok.txt").write_text("hello", encoding="utf-8")
    return sub


def test_simple_relative_path_ok(workspace: Path):
    out = ensure_within_workspace("ok.txt", workspace)
    assert out.endswith("ok.txt")


def test_nested_relative_path_ok(workspace: Path):
    sub = workspace / "deep" / "nested"
    sub.mkdir(parents=True)
    (sub / "x.py").write_text("x", encoding="utf-8")
    out = ensure_within_workspace("deep/nested/x.py", workspace)
    assert os.path.normpath(out) == str(sub / "x.py")


def test_dotdot_traversal_rejected(workspace: Path):
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace("../../../etc/passwd", workspace)


def test_absolute_path_rejected(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace(str(outside), workspace)


def test_empty_path_rejected(workspace: Path):
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace("", workspace)
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace("   ", workspace)


def test_dot_resolves_to_root(workspace: Path):
    out = ensure_within_workspace(".", workspace)
    assert os.path.normpath(out) == str(workspace)


def test_path_with_dotdot_inside_still_inside_ok(workspace: Path):
    """`a/../ok.txt` resolves to `ok.txt` — fine, still inside."""
    out = ensure_within_workspace("deep/../ok.txt", workspace)
    assert out.endswith("ok.txt")


def test_symlink_pointing_outside_rejected(workspace: Path, tmp_path: Path):
    """Symlinks must not be a sandbox bypass."""
    target = tmp_path / "secret.txt"
    target.write_text("nope", encoding="utf-8")
    link = workspace / "evil_link"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/permission level")
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace("evil_link", workspace)


def test_workspace_prefix_collision_not_a_bypass(tmp_path: Path):
    """A sibling directory whose name starts with the workspace name
    must not be considered 'inside' the workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sibling = tmp_path / "ws_admin"
    sibling.mkdir()
    (sibling / "sneaky.txt").write_text("x", encoding="utf-8")

    # Walking from ws to ../ws_admin/sneaky.txt should be rejected even
    # though the resolved string starts with str(ws_admin) which begins
    # with the same prefix as ws.
    with pytest.raises(WorkspacePathError):
        ensure_within_workspace("../ws_admin/sneaky.txt", ws)
