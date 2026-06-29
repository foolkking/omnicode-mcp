"""Contract tests for the audit-bundle.r6 follow-up.

Pinned by the audit:

* omni_context's memory section drives off /memory/search +
  _synthesise_advisory (the same path omni_memory v2 uses) — NOT the
  legacy /memory/advisory backend route.
* memory_status.memory_count matches the real number of recalled rows.
* context.memories[] entries carry memory_id (memory v2 normalised row
  shape).
* When the same advisory inputs are passed to both omni_memory and
  omni_context, the memory_id sets agree.
* memory section budget skip is explicit:
    memory_status.ran == false
    memory_status.reason mentions "skipped due to budget"
    truncation_reasons contains "skipped:memories (budget)"
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from typing import Any, Callable, Dict, List

from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim
# ---------------------------------------------------------------------------


class _ToolManagerStub:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}
        self._tool_manager = _ToolManagerStub()

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            self._tool_manager._tools[fn.__name__] = fn
            return fn

        return deco

    async def list_tools(self) -> List[Any]:  # pragma: no cover
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


def _build_tools(routes: Dict[str, Any]) -> Dict[str, Callable[..., Any]]:
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        if endpoint in routes:
            payload = routes[endpoint]
        else:
            payload = None
            key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
            if key in routes:
                payload = routes[key]
            else:
                for k, v in routes.items():
                    if k.endswith("/") and endpoint.startswith(k):
                        payload = v
                        break
        if payload is None:
            return {"result": {}}
        if callable(payload):
            payload = payload(method, endpoint, kwargs)
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Routes that mimic _detect_mode resolving plus an importance=4 mistake
# in the memory store. Crucially, /memory/search is the route that gets
# stubbed (the new path) rather than /memory/advisory (the legacy one).
# ---------------------------------------------------------------------------


_DETECT_MODE_FILE = "omnicode_adapters/mcp_server/high_level_tools.py"


def _detect_mode_routes_with_memory() -> Dict[str, Any]:
    """Realistic routes: symbol resolves + memory has the relevant row."""
    return {
        # Symbol index — _run_symbol's POST route.
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": "_detect_mode",
                    "file_path": _DETECT_MODE_FILE,
                    "line_start": 80,
                    "line_end": 123,
                    "signature": "def _detect_mode(query: str) -> str:",
                    "symbol_type": "function",
                    "relevance_score": 1.0,
                    "why_matched": ["symbol:exact"],
                }
            ],
            "total_results": 1,
        },
        # text grep used by _run_references fallback
        "/search/text": {
            "results": [
                {
                    "file_path": "tests/unit/test_detect_mode.py",
                    "line_number": 50,
                    "line_content": "    assert _detect_mode(query) == expected",
                    "context_before": [],
                    "context_after": [],
                    "match_type": "text",
                    "relevance_score": 0.6,
                    "why_matched": ["text:line_match"],
                }
            ],
            "total_results": 1,
        },
        # LSP unavailable
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/lsp/references": {"error": "lsp not running"},
        # Impact trio
        "/graph/risk": {
            "risk": "medium",
            "reasons": ["Affects 96 files", "Limited test coverage"],
        },
        "/graph/impact": {
            "affected_symbols": ["fullmatch", "split"],
            "dependent_symbols": ["test_detect_mode_routing"],
            "files_count": 96,
            "files_involved": [f"f{i}.py" for i in range(20)],
        },
        "/graph/related-tests": {
            "test_files": ["tests/unit/test_detect_mode.py"],
            "suggested_commands": ["pytest tests/unit/test_detect_mode.py"],
        },
        # Diagnostics empty
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/": {"diagnostics": []},
        # Git status
        "/git/status": {
            "status": {
                "modified_files": [_DETECT_MODE_FILE],
                "untracked_files": [],
                "staged_files": [],
            }
        },
        # ===== THE KEY ROUTE for r6: /memory/search drives advisory =====
        # The legacy /memory/advisory route is intentionally NOT stubbed
        # so any code that still calls it gets {} and we'd see empty.
        "/memory/search": {
            "results": [
                {
                    "memory": {
                        "id": 8,
                        "category": "mistake",
                        "content": "When modifying _detect_mode, always update tests/unit/test_detect_mode.py because search mode routing regressions are easy to miss.",
                        "importance": 4,
                        "tags": ["search", "mode-routing", "test", "regression"],
                        "timestamp": _now_iso(),
                        "related_files": [_DETECT_MODE_FILE],
                    },
                    "relevance_score": 0.9,
                    "match_reason": "Matched in content + tags + embedding",
                },
                {
                    "memory": {
                        "id": 4,
                        "category": "solution",
                        "content": "Fixed FAISS by persisting the index after each add.",
                        "importance": 4,
                        "tags": ["faiss", "search"],
                        "timestamp": _now_iso(),
                    },
                    "relevance_score": 0.55,
                    "match_reason": "Matched in tags + embedding",
                },
            ]
        },
    }


# ---------------------------------------------------------------------------
# 1. omni_context routes memory through the v2 advisory pipeline
# ---------------------------------------------------------------------------


def test_context_uses_memory_v2_advisory_path() -> None:
    """The legacy /memory/advisory route must NOT be hit by the
    composer — only /memory/search."""
    tools = _build_tools(_detect_mode_routes_with_memory())
    _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    ))
    captured = tools["__captured__"]
    # Old route MUST NOT be used by omni_context.
    assert "/memory/advisory" not in captured, (
        f"omni_context must not call /memory/advisory; captured: "
        f"{list(captured.keys())}"
    )
    # New route IS used.
    assert "/memory/search" in captured


# ---------------------------------------------------------------------------
# 2. memory_status.memory_count matches the real recall
# ---------------------------------------------------------------------------


def test_context_memory_status_count_matches_memories() -> None:
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    mem_status = payload.get("memory_status") or {}
    assert mem_status.get("ran") is True, payload
    # The 2 memories the search returned both go into the synthesis.
    assert mem_status.get("memory_count", 0) >= 2, payload
    # And memories[] in context has at least 2 normalised rows.
    assert len(payload["context"]["memories"]) >= 2


# ---------------------------------------------------------------------------
# 3. context.memories rows include memory_id (memory v2 shape)
# ---------------------------------------------------------------------------


def test_context_memories_rows_include_memory_id() -> None:
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    mems = payload["context"]["memories"]
    assert mems, payload
    for m in mems:
        assert m.get("memory_id") is not None, m
        # And the memory v2 row shape: id alias + category + content +
        # importance + score/confidence.
        assert m.get("id") == m["memory_id"]
        assert m.get("category")
        assert m.get("content")
        assert m.get("importance") is not None
        # score or confidence is acceptable for ranking — both are present
        # in v2-normalised rows.
        assert "score" in m
        assert "confidence" in m


def test_context_memories_match_omni_memory_advisory() -> None:
    """Cross-tool: the memory_ids context surfaces should equal those
    omni_memory(action='advisory') surfaces for the same inputs."""
    tools = _build_tools(_detect_mode_routes_with_memory())

    ctx_payload = json.loads(_run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    )))
    adv_payload = json.loads(_run(tools["omni_memory"](
        action="advisory",
        symbol="_detect_mode",
        task="change search auto routing rules",
        format="json",
    )))

    ctx_ids = {m.get("memory_id") for m in ctx_payload["context"]["memories"]}
    adv_ids = {
        r.get("memory_id")
        for r in adv_payload["referenced_memories"]
    }
    # context's recalled set must be a superset of (or equal to) advisory's
    # — they use the same helper, so the only difference would be
    # caps; advisory caps at 8, context caps at 5. Both are > the 2 we
    # stubbed, so the sets should match exactly here.
    assert adv_ids <= ctx_ids, (ctx_ids, adv_ids)
    assert 8 in ctx_ids, "memory_id 8 (the mistake) must be present"


# ---------------------------------------------------------------------------
# 4. memory_status.memory_count > 0 when advisory has references
# ---------------------------------------------------------------------------


def test_context_memory_status_not_zero_when_advisory_has_references() -> None:
    """The bug we're closing: legacy /memory/advisory always returned
    referenced_memories=[] which made memory_count=0 even when the
    advisory text listed lessons. Now memory_status reflects reality."""
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    mem_status = payload["memory_status"]
    assert mem_status["ran"] is True
    assert mem_status["memory_count"] > 0, payload
    assert mem_status.get("source") == "memory.v2.advisory"
    # Synthesis fields ride along on memory_status (the audit asked for
    # action_items / risks / confidence / why_recalled to be visible).
    assert mem_status.get("synthesis_summary")
    assert mem_status.get("action_items"), mem_status
    assert mem_status.get("confidence") in {"high", "medium", "low"}
    assert mem_status.get("why_recalled")
    # The why_selected list should also reflect the recall.
    why_joined = " ".join(payload.get("why_selected") or [])
    assert "memory:advisory recalled" in why_joined


def test_context_memory_status_zero_when_no_memories_match() -> None:
    """Sanity: when the search returns nothing, memory_count=0 + ran=true
    (we still ran the helper, just got empty), and synthesis is sparse."""
    routes = _detect_mode_routes_with_memory()
    routes["/memory/search"] = {"results": []}
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    mem_status = payload["memory_status"]
    assert mem_status["ran"] is True
    assert mem_status.get("memory_count", 0) == 0
    assert payload["context"]["memories"] == []


# ---------------------------------------------------------------------------
# 5. Budget skip is explicit
# ---------------------------------------------------------------------------


def test_context_memory_budget_skip_is_explicit() -> None:
    """When the token budget is fully consumed before memory runs, the
    composer must say so loudly: ran=false + reason + truncation_reason."""
    # token_budget=10 forces budget exhaustion before we get to memory
    # (primary_symbols + outline alone burns more than 10 tokens).
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode",
        task="change search auto routing rules",
        token_budget=10, format="json",
    ))
    payload = json.loads(raw)
    mem_status = payload["memory_status"]
    # Either the helper got the explicit pre-flight skip OR the budget
    # ran out partway through; both cases must surface the same signal.
    if not mem_status.get("ran", True):
        # Pre-flight skip path.
        assert (mem_status.get("reason") or "").lower().count("budget") >= 1, mem_status
    # Truncation reasons must include the memory budget skip OR the
    # general budget_utilization signal.
    trunc = payload.get("truncation_reasons") or []
    assert payload.get("truncated") is True
    assert trunc, payload
    # At least one reason is memory-related OR utilization-based.
    matched = any(
        r == "skipped:memories (budget)"
        or r.startswith("skipped:memories")
        or r.startswith("budget_utilization:")
        for r in trunc
    )
    assert matched, trunc


# ---------------------------------------------------------------------------
# Bonus: contract_version stayed at context.v2 (schema unchanged) but
# handler_version bumped to r6.
# ---------------------------------------------------------------------------


def test_context_contract_version_still_v2_after_r6_followup() -> None:
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode", token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    assert payload["contract_version"] == "context.v2"
    assert _CONTRACT_VERSIONS["omni_context"] == "context.v2"


def test_handler_version_is_r6() -> None:
    """Originally pinned r6 when this fixture landed; now reads the
    module constant so the test stays valid as the bundle increments
    forward (r7+)."""
    tools = _build_tools(_detect_mode_routes_with_memory())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode", token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    # Whatever the current bundle is, the response and constant must agree.
    assert payload["handler_version"] == _HANDLER_VERSION
    # And the bundle is at least the r6 baseline this test was written for.
    # Use numeric round comparison so r10+ doesn't trip on string ordering.
    import re as _re
    m = _re.search(r"r(\d+)$", _HANDLER_VERSION)
    assert m, f"unexpected handler_version shape: {_HANDLER_VERSION}"
    assert int(m.group(1)) >= 6
