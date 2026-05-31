"""Workspace identity routing for local-agent index endpoints."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)


class _FakeRegistry:
    def __init__(self, entries, active=None):
        self._entries = entries
        self._active = active

    def get(self, workspace_id):
        return self._entries.get(workspace_id)

    def get_active(self):
        return self._active


def test_agent_workspace_header_must_match_active_workdir(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    registry = _FakeRegistry(
        {"repo-a": SimpleNamespace(id="repo-a", path=str(active))}
    )

    resolved = resolve_workspace_request(
        "repo-a",
        working_dir=str(active),
        registry=registry,
    )

    assert Path(resolved.working_dir) == active
    assert resolved.workspace_id == "repo-a"


def test_agent_workspace_header_rejects_inactive_workspace(tmp_path):
    active = tmp_path / "active"
    other = tmp_path / "other"
    active.mkdir()
    other.mkdir()
    registry = _FakeRegistry(
        {"repo-b": SimpleNamespace(id="repo-b", path=str(other))}
    )

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        resolve_workspace_request(
            "repo-b",
            working_dir=str(active),
            registry=registry,
        )

    assert exc_info.value.status_code == 409
    assert "active backend WORKING_DIR" in exc_info.value.detail


def test_agent_workspace_header_rejects_unknown_workspace(tmp_path):
    active = tmp_path / "active"
    active.mkdir()

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        resolve_workspace_request(
            "missing",
            working_dir=str(active),
            registry=_FakeRegistry({}),
        )

    assert exc_info.value.status_code == 404
