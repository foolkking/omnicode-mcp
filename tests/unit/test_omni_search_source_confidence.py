"""Contract tests for omni_search source / confidence stamping.

The audit flagged that pre-fix, ``source`` and ``confidence`` came back
as empty strings for every mode except ``references``. This file pins
down the post-fix behaviour:

1. symbol mode → non-empty source + confidence per result row.
2. semantic mode → non-empty source + confidence per result row.
3. text mode → non-empty source + confidence per result row.
4. references mode never claims ``source=lsp`` when LSP is unavailable.
5. Empty results return a structured JSON envelope with
   ``ok=true, count=0, results=[]``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _infer_source_confidence,
    register_high_level_tools,
)
from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_core.capabilities.registry import build_runtime_capabilities


# ---------------------------------------------------------------------------
# Helpers — minimal MCP + make_request fakes.
# ---------------------------------------------------------------------------


class _MCPStub:
    """Tiny FastMCP shim that captures registered tools."""

    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_omni_search(
    responses: Dict[str, Any],
) -> Callable[..., Any]:
    """Wire up omni_search with a scripted ``make_request``.

    ``responses`` maps an endpoint string (last path segment) to either:
      * a dict — returned as-is wrapped in ``{"result": …}``
      * a callable — called with (method, endpoint, kwargs) for dynamic data
    """

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        # Strip /search/ prefix → "symbols" / "text" / plain "search".
        key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
        # Prefer endpoint-specific override, fall back to full path.
        for candidate in (endpoint, key):
            if candidate in responses:
                payload = responses[candidate]
                if callable(payload):
                    payload = payload(method, endpoint, kwargs)
                return {"result": payload}
        # Default: empty result with the standard shape.
        return {"result": {"results": [], "total_results": 0}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    return mcp.tools["omni_search"]


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _semantic_ready_caps() -> Dict[str, Any]:
    return build_runtime_capabilities(
        cloud_available=False,
        local_index_ready=True,
        line_fts_available=True,
        embedding_available=True,
        semantic_index_ready=True,
        graph_index_ready=False,
    )


# ---------------------------------------------------------------------------
# 1. Symbol mode — every row must carry source + confidence.
# ---------------------------------------------------------------------------


def test_symbol_mode_stamps_source_and_confidence() -> None:
    sym_payload = {
        "results": [
            {
                "symbol_name": "_detect_mode",
                "file_path": "x.py",
                "line_start": 80,
                "line_end": 123,
                "signature": "def _detect_mode(query: str) -> str:",
                "symbol_type": "function",
                "relevance_score": 1.0,
                "why_matched": ["symbol:exact"],
            },
            {
                "symbol_name": "_detect_mod_helper",
                "file_path": "y.py",
                "line_start": 10,
                "line_end": 20,
                "symbol_type": "function",
                "relevance_score": 0.85,
                "why_matched": ["symbol:prefix"],
            },
            {
                "symbol_name": "detect_anything",
                "file_path": "z.py",
                "line_start": 5,
                "line_end": 9,
                "symbol_type": "function",
                "relevance_score": 0.55,
                "why_matched": ["symbol:fuzzy", "rapidfuzz"],
            },
        ],
        "total_results": 3,
    }
    omni_search = _build_omni_search({"/search/symbols": sym_payload})

    raw = _run(omni_search(query="_detect_mode", mode="symbol", format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "symbol"
    assert payload["count"] == 3
    rows = payload["results"]

    # Every row gets non-empty source + confidence.
    for row in rows:
        assert row["source"], f"missing source: {row}"
        assert row["confidence"] in {"high", "medium", "low"}, row

    # Specific contract: exact → high, prefix → medium, fuzzy w/ low score → low.
    assert rows[0]["source"] == "symbol_index"
    assert rows[0]["confidence"] == "high"
    assert rows[1]["source"] == "symbol_index"
    assert rows[1]["confidence"] == "medium"
    assert rows[2]["source"] == "symbol_index_fuzzy"
    assert rows[2]["confidence"] == "low"


def test_search_json_blocks_unavailable_local_semantic_provider() -> None:
    omni_search = _build_omni_search({
        "/search": {"results": [], "total_results": 0},
    })

    raw = _run(omni_search(
        query="how middleware request handling works",
        mode="semantic",
        format="json",
    ))
    payload = json.loads(raw)

    preflight = payload["capability_preflight"]
    assert payload["ok"] is False
    assert payload["error_code"] == "SEMANTIC_INDEX_NOT_READY"
    assert payload["empty_reason"] == "provider_unavailable"
    assert preflight["required"] == ["search.semantic"]
    assert preflight["ready"] is False
    assert "search.semantic" in preflight["states"]
    assert preflight["states"]["search.semantic"]["state"] == "unavailable"
    assert preflight["execution_policy"]["mode"] == "block"
    assert preflight["execution_policy"]["can_execute"] is False


def test_search_json_backend_failure_is_structured_envelope() -> None:
    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        raise TimeoutError("urlopen error timed out")

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)

    raw = _run(mcp.tools["omni_search"](
        query='VALUE = "after"',
        mode="auto",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error_code"] == "CLOUD_UNAVAILABLE"
    assert payload["freshness"] == "unavailable"
    assert payload["empty_reason"] == "provider_unavailable"
    assert payload["provider_unavailable"] is True
    assert payload["query_plan"]["intent"] == "exact_text"
    assert "traceback" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# 2. Semantic mode — source reflects rerank toggle, confidence tracks score.
# ---------------------------------------------------------------------------


def test_semantic_mode_stamps_source_and_confidence_with_rerank(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _semantic_ready_caps(),
    )
    sem_payload = {
        "results": [
            {
                "symbol_name": "omni_search",
                "file_path": "high_level_tools.py",
                "line_start": 902,
                "line_end": 1006,
                "signature": "async def omni_search(",
                "symbol_type": "method",
                "relevance_score": 0.78,  # → high under rerank
                "why_matched": ["semantic"],
            },
            {
                "symbol_name": "_detect_mode",
                "file_path": "high_level_tools.py",
                "line_start": 80,
                "line_end": 123,
                "signature": "def _detect_mode(query: str) -> str:",
                "symbol_type": "function",
                "relevance_score": 0.50,  # → medium
                "why_matched": ["semantic"],
            },
            {
                "symbol_name": "noise",
                "file_path": "irrelevant.py",
                "line_start": 1,
                "line_end": 2,
                "symbol_type": "method",
                "relevance_score": 0.20,  # → low
                "why_matched": ["semantic"],
            },
        ],
        "total_results": 3,
    }
    omni_search = _build_omni_search({"/search": sem_payload})

    raw = _run(
        omni_search(
            query="how does the search system route between modes",
            mode="semantic",
            format="json",
            rerank=True,
        )
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "semantic"
    rows = payload["results"]
    assert all(r["source"] for r in rows)
    assert all(r["confidence"] for r in rows)

    # Rerank on → source carries the +reranker suffix.
    for r in rows:
        assert r["source"] == "vector_index+reranker"

    assert rows[0]["confidence"] == "high"
    assert rows[1]["confidence"] == "medium"
    assert rows[2]["confidence"] == "low"


def test_semantic_mode_without_rerank_uses_plain_source(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _semantic_ready_caps(),
    )
    sem_payload = {
        "results": [
            {
                "symbol_name": "omni_search",
                "file_path": "f.py",
                "line_start": 1,
                "line_end": 2,
                "signature": "async def omni_search(",
                "symbol_type": "method",
                "relevance_score": 0.95,
                "why_matched": ["semantic"],
            }
        ],
        "total_results": 1,
    }
    omni_search = _build_omni_search({"/search": sem_payload})

    raw = _run(
        omni_search(
            query="how does the search system route between modes",
            mode="semantic",
            format="json",
            rerank=False,
        )
    )
    payload = json.loads(raw)
    row = payload["results"][0]
    # No reranker → cannot escalate to "high" even at 0.95.
    assert row["source"] == "vector_index"
    assert row["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 3. Text mode — every row must carry source + confidence.
# ---------------------------------------------------------------------------


def test_text_mode_stamps_source_and_confidence() -> None:
    text_payload = {
        "results": [
            {
                "file_path": "README.md",
                "line_number": 89,
                "line_content": "OMNICODE_LLM_ROUTER=false",
                "context_before": [],
                "context_after": [],
                "match_type": "text",
                "relevance_score": 1.0,
                "why_matched": ["text:line_match"],
            },
            {
                "file_path": "core/lifespan.py",
                "line_number": 76,
                "line_content": "logger.info('LLM router disabled')",
                "context_before": [],
                "context_after": [],
                "match_type": "text",
                "relevance_score": 1.0,
                "why_matched": ["text:line_match"],
            },
        ],
        "total_results": 2,
    }
    omni_search = _build_omni_search({"/search/text": text_payload})

    raw = _run(
        omni_search(query="OMNICODE_LLM_ROUTER", mode="text", format="json")
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "text"
    rows = payload["results"]
    assert len(rows) == 2
    for r in rows:
        assert r["source"] == "text_index"
        assert r["confidence"] == "high"  # exact line_match → high


# ---------------------------------------------------------------------------
# 4. References mode — must not claim source=lsp when LSP fails.
# ---------------------------------------------------------------------------


def test_references_mode_falls_back_without_claiming_lsp() -> None:
    """Simulate an LSP-down environment and ensure honesty.

    /lsp/workspace-symbols returns ``{"error": ...}`` → fallback path must
    use ast_symbol + text_grep. No row may carry ``source=lsp``.
    """

    sym_payload = {
        "results": [
            {
                "symbol_name": "_detect_mode",
                "file_path": "high_level_tools.py",
                "line_start": 80,
                "line_end": 123,
                "signature": "def _detect_mode(query: str) -> str:",
                "symbol_type": "function",
                "relevance_score": 1.0,
                "why_matched": ["symbol:exact"],
            }
        ],
        "total_results": 1,
    }
    text_payload = {
        "results": [
            {
                "file_path": "high_level_tools.py",
                "line_number": 1650,
                "line_content": "resolved_mode = _detect_mode(query) if mode == 'auto' else mode",
                "context_before": ["try:"],
                "context_after": [""],
                "match_type": "text",
                "relevance_score": 0.6,
                "why_matched": ["text:line_match"],
            }
        ],
        "total_results": 1,
    }
    responses = {
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/lsp/references": {"error": "lsp not running"},
        "/search/symbols": sym_payload,
        "/search/text": text_payload,
    }

    omni_search = _build_omni_search(responses)

    raw = _run(
        omni_search(query="_detect_mode", mode="references", format="json")
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "references"

    # Top-level honesty: never call this an LSP run.
    assert payload["source"] != "lsp"
    assert payload["confidence"] != "high"

    # No individual row may claim LSP either.
    for r in payload["results"]:
        assert r["source"] in {"ast_symbol", "text_grep"}, r
        # Definitions are medium, callsites are low — never high in fallback.
        assert r["confidence"] in {"medium", "low"}, r

    # The definition is still surfaced so the AI can act on it.
    defs = [r for r in payload["results"] if r["kind"] == "definition"]
    assert len(defs) == 1
    assert defs[0]["source"] == "ast_symbol"
    assert defs[0]["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 5. Empty results → structured envelope.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,endpoint",
    [
        ("symbol", "/search/symbols"),
        ("semantic", "/search"),
        ("text", "/search/text"),
    ],
)
def test_empty_results_return_structured_envelope(
    mode: str,
    endpoint: str,
    monkeypatch,
) -> None:
    if mode == "semantic":
        monkeypatch.setattr(
            hlt,
            "_runtime_capability_registry_snapshot",
            lambda **_kwargs: _semantic_ready_caps(),
        )
    omni_search = _build_omni_search(
        {endpoint: {"results": [], "total_results": 0}}
    )
    raw = _run(omni_search(query="zzz_no_such_symbol_zzz", mode=mode, format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["resolved_mode"] == mode


def test_empty_references_returns_structured_envelope() -> None:
    """References uses two backends; an empty pair should also be structured."""
    responses = {
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/search/symbols": {"results": [], "total_results": 0},
        "/search/text": {"results": [], "total_results": 0},
    }
    omni_search = _build_omni_search(responses)

    raw = _run(
        omni_search(query="zzz_no_such_zzz", mode="references", format="json")
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["resolved_mode"] == "references"
    # Empty references must still not claim LSP.
    assert payload["source"] != "lsp"


# ---------------------------------------------------------------------------
# Bonus — exercise _infer_source_confidence directly to lock the table.
# ---------------------------------------------------------------------------


def test_infer_source_confidence_table() -> None:
    cases: List[tuple[Dict[str, Any], str, bool, str, str]] = [
        # symbol mode
        ({"why_matched": ["symbol:exact"], "relevance_score": 1.0},
         "symbol", True, "symbol_index", "high"),
        ({"why_matched": ["symbol:prefix"], "relevance_score": 0.9},
         "symbol", True, "symbol_index", "medium"),
        ({"why_matched": ["symbol:contains"], "relevance_score": 0.7},
         "symbol", True, "symbol_index", "medium"),
        ({"why_matched": ["symbol:fuzzy", "rapidfuzz"], "relevance_score": 0.78},
         "symbol", True, "symbol_index_fuzzy", "medium"),
        ({"why_matched": ["symbol:fuzzy"], "relevance_score": 0.40},
         "symbol", True, "symbol_index_fuzzy", "low"),
        # semantic mode
        ({"why_matched": ["semantic"], "relevance_score": 0.80},
         "semantic", True, "vector_index+reranker", "high"),
        ({"why_matched": ["semantic"], "relevance_score": 0.45},
         "semantic", True, "vector_index+reranker", "medium"),
        ({"why_matched": ["semantic"], "relevance_score": 0.10},
         "semantic", False, "vector_index", "low"),
        # text mode
        ({"why_matched": ["text:line_match"], "relevance_score": 1.0},
         "text", True, "text_index", "high"),
        # hybrid mode — both labels present
        ({"why_matched": ["hybrid:symbol", "hybrid:semantic"], "relevance_score": 0.05},
         "hybrid", True, "hybrid:semantic+symbol", "high"),
        ({"why_matched": ["hybrid:symbol"], "relevance_score": 0.025},
         "hybrid", True, "hybrid:symbol", "medium"),
        ({"why_matched": ["hybrid:semantic"], "relevance_score": 0.001},
         "hybrid", True, "hybrid:semantic", "low"),
    ]
    for row, mode, rerank, want_src, want_conf in cases:
        got_src, got_conf = _infer_source_confidence(row, mode, rerank=rerank)
        assert got_src == want_src, (mode, row, got_src)
        assert got_conf == want_conf, (mode, row, got_conf)


# ---------------------------------------------------------------------------
# audit-bundle.r9 — illegal mode JSON envelope (P1-1).
#
# Before r9 an illegal mode under format='json' fell through to a
# plain-text "❌ Unknown search mode" string. These tests pin the new
# structured, stamped ok=false envelope.
# ---------------------------------------------------------------------------


from omnicode_adapters.mcp_server.high_level_tools import (  # noqa: E402
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _SEARCH_VALID_MODES,
)


def test_omni_search_illegal_mode_json_error_envelope() -> None:
    omni_search = _build_omni_search({})
    raw = _run(omni_search(
        query="_detect_mode", mode="illegal_mode", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["requested_mode"] == "illegal_mode"
    assert payload["query"] == "_detect_mode"
    assert "illegal_mode" in payload["error"]


def test_omni_search_illegal_mode_includes_valid_modes() -> None:
    omni_search = _build_omni_search({})
    raw = _run(omni_search(
        query="x", mode="nope", format="json",
    ))
    payload = json.loads(raw)
    assert "valid_modes" in payload
    # Every real mode must be advertised.
    for m in ("auto", "semantic", "symbol", "text", "hybrid", "references"):
        assert m in payload["valid_modes"]
    assert list(payload["valid_modes"]) == list(_SEARCH_VALID_MODES)
    assert payload.get("next_actions")


def test_omni_search_illegal_mode_is_stamped() -> None:
    omni_search = _build_omni_search({})
    raw = _run(omni_search(
        query="x", mode="bogus", format="json",
    ))
    payload = json.loads(raw)
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_search"]
    # contract_version must NOT drift — still source_confidence.v1.
    assert payload["contract_version"] == "search.source_confidence.v1"


def test_omni_search_illegal_mode_does_not_return_plain_text_json() -> None:
    omni_search = _build_omni_search({})
    raw = _run(omni_search(
        query="x", mode="bogus", format="json",
    ))
    # Must be valid JSON, not a leading-emoji plain-text fallback.
    assert not raw.lstrip().startswith("\u274c")  # ❌
    parsed = json.loads(raw)  # would raise if it were plain text
    assert isinstance(parsed, dict)


def test_omni_search_illegal_mode_text_format_stays_human_readable() -> None:
    """The text path may still return the human-readable error — only the
    JSON path is contractually required to be structured."""
    omni_search = _build_omni_search({})
    raw = _run(omni_search(
        query="x", mode="bogus", format="text",
    ))
    assert "Unknown search mode" in raw
    # Not JSON in text mode.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_omni_search_valid_mode_still_works_after_guard() -> None:
    """Regression guard: the up-front mode check must not break a normal
    symbol search."""
    sym_payload = {
        "results": [
            {
                "file": "x.py", "line": 1, "end_line": 2,
                "name": "foo", "type": "function", "score": 1.0,
            }
        ],
        "total_results": 1,
    }
    omni_search = _build_omni_search({"symbols": sym_payload})
    raw = _run(omni_search(query="foo", mode="symbol", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["resolved_mode"] == "symbol"
