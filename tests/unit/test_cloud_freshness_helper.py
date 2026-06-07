"""Cloud-side freshness helper tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "cloud_freshness_under_test",
    _ROOT / "api" / "v1" / "routers" / "freshness.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
freshness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(freshness)


class _Store:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status

    def status(self, workspace_id: str) -> dict[str, Any]:
        assert workspace_id == "repo-a"
        return self._status


def test_cloud_freshness_allows_when_indexed_revision_meets_min(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 5}),
    )

    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
    ) is None


def test_cloud_freshness_returns_structured_stale_error(monkeypatch) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 4}),
    )

    payload = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
    )

    assert payload is not None
    assert payload["ok"] is False
    assert payload["success"] is False
    assert payload["stale"] is True
    assert payload["freshness"] == "stale"
    assert payload["accepted_revision"] == 5
    assert payload["indexed_revision"] == 4
    assert payload["required_revision"] == 5


def test_cloud_freshness_allows_snapshot_fresh_when_semantic_lags(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 4}),
    )

    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
        allow_snapshot_fresh=True,
    ) is None

    state = freshness.cloud_freshness_state(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert state is not None
    assert state["freshness"] == "snapshot_fresh"
    assert state["semantic_stale"] is True


def test_cloud_freshness_is_noop_without_min_revision() -> None:
    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=None,
    ) is None
