"""Tests for the hybrid MCP tool routing policy."""

from __future__ import annotations

import pytest

from omnicode_core.workspace.tool_router import HybridToolRouter, SyncRevisionState


def test_local_authority_tools_stay_local_in_hybrid_mode() -> None:
    router = HybridToolRouter(executor="hybrid")
    state = SyncRevisionState(local_revision=5, accepted_revision=5, indexed_revision=5)

    for tool in ("omni_read", "omni_patch"):
        route = router.route(tool, sync_state=state)
        assert route.target == "local"
        assert route.local_authority is True
        assert "local workspace authority" in route.reason


def test_diagnostics_is_local_first_not_local_authority() -> None:
    route = HybridToolRouter(executor="hybrid").route("omni_diagnostics")

    assert route.target == "local"
    assert route.local_authority is False
    assert route.local_first is True
    assert "local-first diagnostics" in route.reason


def test_read_and_patch_stay_local_even_in_remote_executor() -> None:
    router = HybridToolRouter(executor="remote")

    assert router.route("omni_read").target == "local"
    assert router.route("omni_read").local_authority is True
    assert router.route("omni_patch").target == "local"
    assert router.route("omni_patch").local_authority is True


def test_cloud_tools_route_to_cloud_when_index_is_current() -> None:
    router = HybridToolRouter(executor="hybrid")
    state = SyncRevisionState(local_revision=5, accepted_revision=5, indexed_revision=5)

    route = router.route("omni_search", sync_state=state)

    assert route.target == "cloud"
    assert route.requires_barrier is True
    assert route.barrier_min_revision == 5
    assert route.stale is False
    assert route.indexed_revision == 5


def test_cloud_tools_block_when_index_is_stale_with_barrier_revision() -> None:
    router = HybridToolRouter(executor="hybrid")
    state = SyncRevisionState(local_revision=5, accepted_revision=5, indexed_revision=4)

    route = router.route("omni_context", sync_state=state)

    assert route.target == "blocked"
    assert route.requires_barrier is True
    assert route.barrier_min_revision == 5
    assert route.stale is True
    assert "omni_status()" in route.next_actions[1]


def test_search_context_impact_all_require_cloud_barrier() -> None:
    router = HybridToolRouter(executor="hybrid")
    state = SyncRevisionState(local_revision=7, accepted_revision=7, indexed_revision=7)

    for tool in ("omni_search", "omni_context", "omni_impact"):
        route = router.route(tool, sync_state=state)
        assert route.target == "cloud"
        assert route.requires_barrier is True
        assert route.barrier_min_revision == 7


def test_cloud_unavailable_blocks_cloud_analysis_tools() -> None:
    state = SyncRevisionState(
        local_revision=5,
        accepted_revision=5,
        indexed_revision=5,
        cloud_available=False,
    )

    hybrid_route = HybridToolRouter(executor="hybrid").route(
        "omni_impact",
        sync_state=state,
    )
    assert hybrid_route.target == "blocked"
    assert "cloud backend is unavailable" in hybrid_route.reason
    assert HybridToolRouter(executor="remote").route(
        "omni_impact",
        sync_state=state,
    ).target == "blocked"


def test_status_is_aggregate() -> None:
    route = HybridToolRouter(executor="hybrid").route("omni_status")

    assert route.target == "aggregate"
    assert "combines local runtime" in route.reason


def test_local_executor_routes_cloud_capable_tools_locally() -> None:
    route = HybridToolRouter(executor="local").route(
        "omni_search",
        sync_state=SyncRevisionState(local_revision=5, indexed_revision=0),
    )

    assert route.target == "local"


def test_rejects_unknown_executor() -> None:
    with pytest.raises(ValueError):
        HybridToolRouter(executor="sideways")
