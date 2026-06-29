from __future__ import annotations

from omnicode_core.search.planner import (
    build_search_plan,
    detect_search_mode,
    empty_reason_for_unavailable,
)
from omnicode_adapters.mcp_server.high_level_tools import (
    _symbol_candidate_from_text_query,
)


def test_detect_search_mode_routes_code_declarations_to_text() -> None:
    assert detect_search_mode("class BaseHandler:") == "text"
    assert detect_search_mode("class ReplicaManager") == "text"
    assert detect_search_mode("def _detect_mode") == "text"


def test_detect_search_mode_routes_identifiers_to_symbol() -> None:
    assert detect_search_mode("BaseHandler") == "symbol"
    assert detect_search_mode("_detect_mode") == "symbol"


def test_text_plan_lists_deterministic_provider_chain() -> None:
    plan = build_search_plan(
        query="class BaseHandler:",
        requested_mode="auto",
        resolved_mode="text",
    )

    assert plan.intent == "exact_text"
    assert plan.required_capabilities == ["search.text_exact"]
    assert plan.providers == [
        "exact_line_fts",
        "ripgrep_fallback",
        "python_grep_fallback",
        "cloud_snapshot_grep",
    ]
    assert plan.to_dict()["fallback_capabilities"]


def test_declaration_text_queries_extract_symbol_fast_path_candidate() -> None:
    assert _symbol_candidate_from_text_query("class BaseHandler:") == (
        "BaseHandler",
        "class",
    )
    assert _symbol_candidate_from_text_query("class ReplicaManager") == (
        "ReplicaManager",
        "class",
    )
    assert _symbol_candidate_from_text_query("def _detect_mode") == (
        "_detect_mode",
        "function",
    )
    assert _symbol_candidate_from_text_query("arbitrary middleware text") == (
        None,
        None,
    )


def test_symbol_plan_keeps_exact_symbol_contract() -> None:
    plan = build_search_plan(
        query="ReplicaManager",
        requested_mode="symbol",
        resolved_mode="symbol_exact",
        freshness_required=True,
    )

    payload = plan.to_dict(providers=["local_exact_index"])
    assert payload["intent"] == "exact_symbol"
    assert payload["resolved_mode"] == "symbol_exact"
    assert payload["providers"] == ["local_exact_index"]
    assert payload["required_capabilities"] == ["search.symbol_exact"]
    assert payload["freshness_required"] is True


def test_semantic_plan_is_not_default_safe_dependency() -> None:
    plan = build_search_plan(
        query="how request middleware works",
        requested_mode="semantic",
    )

    assert plan.intent == "semantic"
    assert plan.providers == ["semantic_vector"]
    assert plan.required_capabilities == ["search.semantic"]
    assert "search.symbol_exact" in plan.fallback_capabilities


def test_empty_reason_distinguishes_infrastructure_from_true_empty() -> None:
    assert empty_reason_for_unavailable(index_ready=False) == "index_not_ready"
    assert empty_reason_for_unavailable(provider_available=False) == "provider_unavailable"
    assert empty_reason_for_unavailable(filtered=True) == "filtered_out"
    assert empty_reason_for_unavailable() == "true_empty"
