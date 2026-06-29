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


class _Exact:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status

    def status(self, *, workspace_id: str) -> dict[str, Any]:
        assert workspace_id == "repo-a"
        return self._status


class _Graph:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status
        self.readiness_calls = 0

    def try_readiness(
        self,
        *,
        workspace_id: str,
        accepted_revision: int,
        lock_timeout_ms: int,
    ) -> dict[str, Any]:
        assert workspace_id == "repo-a"
        assert accepted_revision == 5
        assert lock_timeout_ms == 75
        self.readiness_calls += 1
        return self._status

    def status(
        self,
        *,
        workspace_id: str,
        accepted_revision: int,
    ) -> dict[str, Any]:
        assert workspace_id == "repo-a"
        assert accepted_revision == 5
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
    assert payload["freshness"] == "snapshot_fresh"
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


def test_snapshot_fresh_short_circuit_skips_index_status(monkeypatch) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 0}),
    )
    monkeypatch.setattr(
        freshness,
        "SnapshotExactIndex",
        lambda: (_ for _ in ()).throw(
            AssertionError("exact status should not run")
        ),
    )
    monkeypatch.setattr(
        freshness,
        "WorkspaceGraphIndex",
        lambda *, store: (_ for _ in ()).throw(
            AssertionError("graph status should not run")
        ),
    )

    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
        allow_snapshot_fresh=True,
    ) is None


def test_cloud_freshness_state_can_skip_graph_status(monkeypatch) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 4}),
    )
    monkeypatch.setattr(
        freshness,
        "SnapshotExactIndex",
        lambda: _Exact({"exact_indexed_revision": 5}),
    )
    monkeypatch.setattr(
        freshness,
        "WorkspaceGraphIndex",
        lambda *, store: (_ for _ in ()).throw(
            AssertionError("graph status should not run")
        ),
    )

    state = freshness.cloud_freshness_state(
        workspace_id="repo-a",
        min_revision=5,
        include_graph=False,
    )

    assert state is not None
    assert state["freshness"] == "exact_fresh"
    assert state["graph_fresh"] is False


def test_cloud_freshness_allows_exact_fresh_when_semantic_lags(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store({"accepted_revision": 5, "indexed_revision": 4}),
    )
    monkeypatch.setattr(
        freshness,
        "SnapshotExactIndex",
        lambda: _Exact({"exact_indexed_revision": 5}),
    )

    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
        allow_exact_fresh=True,
    ) is None

    strict = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert strict is not None
    assert strict["freshness"] == "exact_fresh"
    assert strict["error"] == "Cloud semantic index is stale"
    assert strict["exact_indexed_revision"] == 5
    assert strict["recommended_query_mode"] == "exact_first"
    assert strict["exact_query_safe"] is True
    assert strict["strict_semantic_safe"] is False
    assert "exact symbol/text search" in strict["next_actions"][0]


def test_cloud_freshness_treats_exact_only_initial_sync_as_semantic_stale(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        freshness,
        "CloudSnapshotStore",
        lambda: _Store(
            {
                "accepted_revision": 5,
                "indexed_revision": 5,
                "semantic_initial_exact_only": True,
                "semantic_index_coverage": "exact_only_initial_sync",
            }
        ),
    )
    monkeypatch.setattr(
        freshness,
        "SnapshotExactIndex",
        lambda: _Exact({"exact_indexed_revision": 5}),
    )

    state = freshness.cloud_freshness_state(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert state is not None
    assert state["freshness"] == "exact_fresh"
    assert state["semantic_fresh"] is False
    assert state["semantic_stale"] is True
    assert state["semantic_initial_exact_only"] is True
    assert state["semantic_index_coverage"] == "exact_only_initial_sync"

    exact_allowed = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
        allow_exact_fresh=True,
    )
    assert exact_allowed is None

    strict = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert strict is not None
    assert strict["freshness"] == "exact_fresh"
    assert strict["semantic_initial_exact_only"] is True
    assert strict["semantic_index_coverage"] == "exact_only_initial_sync"
    assert strict["recommended_query_mode"] == "exact_first"
    assert strict["query_mode_reason"] == "exact_only_initial_sync"


def test_cloud_freshness_allows_current_persisted_graph(
    monkeypatch,
) -> None:
    store = _Store(
        {
            "accepted_revision": 5,
            "indexed_revision": 5,
            "semantic_initial_exact_only": True,
            "semantic_index_coverage": "exact_only_initial_sync",
        }
    )
    monkeypatch.setattr(freshness, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        freshness,
        "SnapshotExactIndex",
        lambda: _Exact({"exact_indexed_revision": 5}),
    )
    graph = _Graph({"ready": True, "graph_indexed_revision": 5})
    monkeypatch.setattr(
        freshness,
        "WorkspaceGraphIndex",
        lambda *, store: graph,
    )

    state = freshness.cloud_freshness_state(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert state is not None
    assert state["semantic_fresh"] is False
    assert state["graph_fresh"] is True
    assert state["graph_indexed_revision"] == 5
    assert graph.readiness_calls == 1

    allowed = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
        allow_graph_fresh=True,
    )
    assert allowed is None

    strict = freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=5,
    )
    assert strict is not None
    assert strict["graph_query_safe"] is True


def test_cloud_freshness_is_noop_without_min_revision() -> None:
    assert freshness.cloud_freshness_error(
        workspace_id="repo-a",
        min_revision=None,
    ) is None
