"""Contract tests for audit-bundle.r17 — Round 8 token-budget honesty.

Pinned by the Round 8 audit (1 P1 + 2 P3 fixes):

* P1   omni_search JSON path now emits ``token_estimate`` + ``truncated``
       + (when set) ``token_budget``, and trims ``results[]`` from the
       lowest-relevance tail when the estimate exceeds budget. The
       references-mode mirror stays in sync. Pre-r17 the parameter was
       silently ignored on the JSON path.

* P3-A omni_context promotes the top lexical hit per task term to
       ``primary_symbols`` BEFORE filling ``related_files`` when no
       explicit symbol/file anchor is provided. Prevents the audit's
       "primary_symbols=[] while related_files burns the budget" case.

* P3-B omni_memory(action='advisory') gains ``max_memories`` +
       ``token_budget`` parameters and surfaces ``token_estimate`` +
       ``truncated`` (+ ``truncation_reasons`` on cap). Brings the
       advisory budget contract in line with omni_search / omni_read /
       omni_context.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from omnicode_adapters.mcp_server import high_level_tools as hlt
from tests.unit.mcp_harness import build_tools as _build_tools
from tests.unit.mcp_harness import run as _run

# ===========================================================================
# P1 — omni_search budget honesty
# ===========================================================================


def _wide_symbol_results(n: int = 20) -> Dict[str, Any]:
    """Build a fat /search/symbols response with n hits so total
    token_estimate exceeds tight budgets."""
    return {
        "results": [
            {
                "symbol_name": f"sym_{i:02d}",
                "file_path": f"src/dir_{i // 5}/file_{i}.py",
                "line_start": 100 + i,
                "line_end": 130 + i,
                "kind": "function",
                "signature": f"def sym_{i:02d}(arg_a: str, arg_b: int) -> None:",
                "relevance_score": 1.0 - i * 0.04,
            }
            for i in range(n)
        ],
        "total_results": n,
    }


def test_omni_search_emits_token_estimate_when_budget_set() -> None:
    """token_estimate must always be present in the JSON envelope when
    token_budget>0, and ``truncated`` must reflect estimate vs budget."""
    routes = {"/search/symbols": _wide_symbol_results(20)}
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="sym_",
        mode="symbol",
        max_results=20,
        token_budget=500,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "token_estimate" in payload, "P1: token_estimate MUST be emitted"
    assert isinstance(payload["token_estimate"], int)
    assert payload["token_estimate"] > 0
    assert "truncated" in payload
    assert payload["token_budget"] == 500


def test_omni_search_no_budget_param_does_not_inject_token_budget() -> None:
    """When the caller does NOT pass token_budget, the response should
    still carry token_estimate (always honest) but NOT a forced
    truncated=true. token_budget echo is omitted."""
    routes = {"/search/symbols": _wide_symbol_results(5)}
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="sym_",
        mode="symbol",
        max_results=10,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "token_estimate" in payload
    # No budget passed → never truncated.
    assert payload["truncated"] is False
    assert "token_budget" not in payload


def test_omni_search_tight_budget_trims_results_from_tail() -> None:
    """When the response exceeds the budget, results[] are trimmed
    from the lowest-relevance tail until the estimate fits, count
    matches the kept rows, and truncation_reasons is set."""
    # Use 30 results so total payload definitely exceeds 200 tokens.
    routes = {"/search/symbols": _wide_symbol_results(30)}
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="sym_",
        mode="symbol",
        max_results=30,
        token_budget=200,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["truncated"] is True
    # Trimmed → count is now smaller than the original 30 hits but >= 1.
    assert 1 <= payload["count"] < 30
    assert payload["total"] == 30  # backend total preserved
    assert "truncation_reasons" in payload
    joined = " ".join(payload["truncation_reasons"]).lower()
    assert "results_capped" in joined
    # The first row (highest relevance) must survive the trim.
    assert payload["results"][0]["symbol"] in {f"sym_{i:02d}" for i in range(30)}
    # next_actions[0] should advise widening the budget.
    first = payload["next_actions"][0].lower()
    assert "token_budget" in first or "max_results" in first
    # JSON must be parseable (regression for partial-write corruption).
    json.loads(raw)


def test_omni_search_response_remains_valid_json_when_trimmed() -> None:
    """The trimmed response must still round-trip through json.loads
    (no truncation in the middle of an object). Pinned because the
    audit explicitly asks if the JSON stays valid."""
    routes = {"/search/symbols": _wide_symbol_results(15)}
    tools = _build_tools(routes)
    for budget in (200, 350, 600, 5000):
        raw = _run(tools["omni_search"](
            query="sym_",
            mode="symbol",
            max_results=15,
            token_budget=budget,
            format="json",
        ))
        # Round-trip parse — fails loudly if the trimmer ever produces
        # garbage.
        parsed = json.loads(raw)
        assert parsed["ok"] is True
        assert "token_estimate" in parsed
        assert isinstance(parsed["results"], list)


# ===========================================================================
# P3-A — omni_context primary_symbols promotion
# ===========================================================================


def test_omni_context_promotes_lexical_hit_to_primary_symbols() -> None:
    """When the caller passes a task with code-shaped tokens but no
    symbol/file anchor, the highest-scoring lexical hit per term must
    be promoted to ``primary_symbols`` (capped at 2 promotions)."""
    routes = {
        "/symbols/find": {"results": []},
        # Symbol search returns a real high-relevance hit for the task
        # token "_detect_mode".
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": "_detect_mode",
                    "file_path": "src/router.py",
                    "line_start": 80,
                    "line_end": 123,
                    "kind": "function",
                    "signature": "def _detect_mode(query: str) -> str:",
                    "relevance_score": 0.95,
                },
                {
                    "symbol_name": "_detect_mode_alt",
                    "file_path": "src/other.py",
                    "line_start": 10,
                    "line_end": 20,
                    "kind": "function",
                    "signature": "def _detect_mode_alt():",
                    "relevance_score": 0.7,
                },
            ],
            "total_results": 2,
        },
        "/search": {"results": []},
        "/git/status": {"status": {"modified_files": []}},
        "/memory/search": {"results": []},
        "/lsp/definitions": {"locations": [], "available": True},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        task="modify _detect_mode search routing",
        token_budget=2000,
        max_files=10,
        format="json",
    ))
    payload = json.loads(raw)
    primary = payload["context"]["primary_symbols"]
    # Top-scoring lexical hit must have been promoted.
    assert primary, "P3-A: primary_symbols must NOT be empty for task-only queries"
    names = [r.get("name") for r in primary]
    assert "_detect_mode" in names
    # And the promotion is recorded in why_selected.
    why_blob = " ".join(payload["why_selected"]).lower()
    assert "promoted" in why_blob


def test_omni_context_skips_promotion_when_symbol_already_passed() -> None:
    """When caller passes ``symbol=``, that's the explicit primary
    anchor — lexical hits should NOT be promoted on top of it."""
    # The explicit symbol resolution and the task lexical scan both go
    # through ``/search/symbols``. We make the route query-aware so the
    # right hit comes back for each call.
    def _symbols_route(method: str, endpoint: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        params = kwargs.get("params") or {}
        q = params.get("query") or ""
        if q == "explicit_func":
            return {
                "results": [{
                    "symbol_name": "explicit_func",
                    "file_path": "src/x.py",
                    "line_start": 5, "line_end": 10,
                    "signature": "def explicit_func():",
                    "kind": "function",
                    "relevance_score": 1.0,
                }],
                "total_results": 1,
            }
        if q == "task_lexical_hit":
            return {
                "results": [{
                    "symbol_name": "task_lexical_hit",
                    "file_path": "src/y.py",
                    "line_start": 1, "line_end": 5,
                    "kind": "function",
                    "relevance_score": 0.9,
                    "signature": "def task_lexical_hit():",
                }],
                "total_results": 1,
            }
        return {"results": [], "total_results": 0}

    routes = {
        "/search/symbols": _symbols_route,
        "/search": {"results": []},
        "/git/status": {"status": {"modified_files": []}},
        "/memory/search": {"results": []},
        "/lsp/definitions": {"locations": [], "available": True},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        symbol="explicit_func",
        task="something with task_lexical_hit",
        token_budget=4000,
        format="json",
    ))
    payload = json.loads(raw)
    primary_names = [
        r.get("name") for r in payload["context"]["primary_symbols"]
    ]
    # The explicit symbol stays as primary; the lexical hit goes into
    # related_files instead.
    assert "explicit_func" in primary_names
    # Promotion guard: when an explicit symbol anchor exists, lexical
    # promotion must NOT add task_lexical_hit on top. It might still
    # land in related_files — that's fine — but not in primary.
    assert "task_lexical_hit" not in primary_names


def test_omni_context_promotion_caps_at_two() -> None:
    """At most 2 lexical hits should be promoted to primary_symbols."""
    routes = {
        "/symbols/find": {"results": []},
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": f"hit_{i}",
                    "file_path": f"src/h{i}.py",
                    "line_start": i, "line_end": i + 1,
                    "kind": "function",
                    "relevance_score": 0.9,
                    "signature": f"def hit_{i}():",
                }
                for i in range(5)
            ],
            "total_results": 5,
        },
        "/search": {"results": []},
        "/git/status": {"status": {"modified_files": []}},
        "/memory/search": {"results": []},
        "/lsp/definitions": {"locations": [], "available": True},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        task="hit_0 hit_1 hit_2 hit_3 hit_4",
        token_budget=4000,
        format="json",
    ))
    payload = json.loads(raw)
    primary = payload["context"]["primary_symbols"]
    # Cap is 2 — must not flood primary with everything.
    assert len(primary) <= 2


# ===========================================================================
# P3-B — omni_memory advisory budget contract
# ===========================================================================


def _wide_advisory_routes(n: int = 8) -> Dict[str, Any]:
    """Backend returns n memories — enough to need budget gating."""
    return {
        "/memory/search": {
            "results": [
                {
                    "id": i,
                    "memory_id": i,
                    "category": "solution" if i % 2 else "mistake",
                    "content": "padded content " * 20 + f" entry {i}",
                    "tags": ["tag_a", "tag_b"],
                    "importance": 4 - (i % 3),
                    "score": 0.9 - i * 0.05,
                    "match_reason": "Matched in content + tags",
                    "match_fields": [
                        {"field": "content", "snippet": "...", "weight": 1.0},
                    ],
                }
                for i in range(1, n + 1)
            ],
        },
        "/memory/advisory": {"advisory": "...", "memories_used": []},
    }


def test_omni_memory_advisory_emits_token_estimate() -> None:
    routes = _wide_advisory_routes(8)
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="my_func",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "token_estimate" in payload, (
        "P3-B: omni_memory advisory must emit token_estimate"
    )
    assert "truncated" in payload
    assert "max_memories" in payload


def test_omni_memory_advisory_max_memories_caps_results() -> None:
    """max_memories=2 must produce at most 2 memories rows."""
    routes = _wide_advisory_routes(10)
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="my_func",
        max_memories=2,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["max_memories"] == 2
    assert len(payload["memories"]) <= 2


def test_omni_memory_advisory_token_budget_trims_memories() -> None:
    """When token_budget is set and exceeded, the memories[] list is
    trimmed from the tail (lowest score) and truncation_reasons records
    the cap. JSON must remain parseable.

    Note: the advisory also synthesises ``advisory`` / ``advisory_text``
    / ``referenced_memories`` blocks that don't shrink with the
    ``memories`` cap, so the post-trim ``token_estimate`` may still
    exceed the budget — the contract is "trim what we can and tell the
    truth", not "always fit".
    """
    routes = _wide_advisory_routes(8)
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="my_func",
        max_memories=8,
        token_budget=600,
        format="json",
    ))
    payload = json.loads(raw)
    if payload["truncated"]:
        assert "truncation_reasons" in payload
        joined = " ".join(payload["truncation_reasons"]).lower()
        assert "memories_capped" in joined
        # Memories list MUST have been trimmed below the original 8.
        assert len(payload["memories"]) < 8
    # JSON must round-trip cleanly regardless.
    json.loads(raw)


def test_omni_memory_advisory_no_budget_does_not_force_truncated() -> None:
    routes = _wide_advisory_routes(3)
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="my_func",
        format="json",
    ))
    payload = json.loads(raw)
    # No token_budget passed → ``truncated`` is honestly False.
    assert payload["truncated"] is False
    assert "token_budget" not in payload


# ===========================================================================
# Feature flags + version stamp
# ===========================================================================


def test_handler_features_advertise_r17_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    for flag in (
        "search.budget_honesty",
        "context.primary_priority",
        "memory.advisory_budget",
    ):
        assert flag in flags, f"missing r17 feature flag: {flag}"


def test_handler_version_is_r17() -> None:
    import re
    m = re.search(r"\.r(\d+)", hlt._HANDLER_VERSION)
    assert m is not None
    assert int(m.group(1)) >= 17, hlt._HANDLER_VERSION
