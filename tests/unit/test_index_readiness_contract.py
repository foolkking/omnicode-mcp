from __future__ import annotations

from omnicode_core.workspace.readiness import build_index_readiness_contract


def test_readiness_contract_recommends_exact_first_for_large_initial_sync() -> None:
    contract = build_index_readiness_contract(
        workspace_id="repo-a",
        accepted_revision=12,
        semantic_indexed_revision=12,
        exact_indexed_revision=12,
        snapshot_files=7000,
        exact_files=7000,
        exact_symbols=45000,
        exact_lines=1100000,
        semantic_index_coverage="exact_only_initial_sync",
        semantic_initial_exact_only=True,
    )

    assert contract["snapshot_ready"] is True
    assert contract["exact_index_ready"] is True
    assert contract["semantic_index_ready"] is False
    assert contract["search_degraded"] is True
    assert contract["recommended_query_mode"] == "exact_first"
    assert contract["query_mode_reason"] == "exact_only_initial_sync"
    assert contract["exact_query_safe"] is True
    assert contract["strict_semantic_safe"] is False
    assert "exact_symbol" in contract["supported_query_modes"]
    assert "semantic" not in contract["supported_query_modes"]


def test_readiness_contract_recommends_semantic_first_when_full_index_ready() -> None:
    contract = build_index_readiness_contract(
        workspace_id="repo-a",
        accepted_revision=12,
        semantic_indexed_revision=12,
        exact_indexed_revision=12,
        snapshot_files=20,
        exact_files=20,
        exact_symbols=100,
        exact_lines=500,
        semantic_index_coverage="semantic_full",
    )

    assert contract["semantic_index_ready"] is True
    assert contract["search_degraded"] is False
    assert contract["recommended_query_mode"] == "semantic_first"
    assert contract["query_mode_reason"] == "semantic_full"
    assert contract["strict_semantic_safe"] is True
    assert "semantic" in contract["supported_query_modes"]


def test_readiness_contract_recommends_local_only_for_empty_workspace() -> None:
    contract = build_index_readiness_contract(workspace_id="repo-a")

    assert contract["snapshot_ready"] is False
    assert contract["exact_index_ready"] is False
    assert contract["semantic_index_ready"] is False
    assert contract["search_degraded"] is False
    assert contract["recommended_query_mode"] == "local_only"
    assert contract["query_mode_reason"] == "empty_workspace"
    assert contract["supported_query_modes"] == ["local"]
