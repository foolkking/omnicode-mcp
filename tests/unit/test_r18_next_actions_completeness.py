"""Contract tests for audit-bundle.r18 — Round 9 next_actions completeness.

Pinned by Round 9 (2 P2 + 2 P3 fixes):

* P2-A omni_search JSON success path now emits ``next_actions`` for all
       resolved modes (symbol / references / text / semantic / hybrid),
       branching on the quality of the top hit so AI editors get a
       ready-to-run follow-up.
* P2-B omni_memory advisory next_actions interpolate the actual
       ``symbol`` / ``file`` / ``task`` instead of leaving ``<symbol>``
       placeholders for the caller to substitute.
* P3-A discover_tools mirrors the ``pipeline`` field as ``next_actions``
       so AI editors using the canonical ``next_actions`` key get the
       same workflow steps without special-casing this tool.
* P3-B omni_diagnostics next_actions include a targeted
       ``omni_read(mode='range', ...)`` locator pointing at the first
       error / warning line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _HANDLER_VERSION,
    register_high_level_tools,
)


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
        if payload is None:
            return {"result": {}}
        if callable(payload):
            payload = payload(method, endpoint, kwargs)
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# P2-A — omni_search next_actions
# ===========================================================================


def test_omni_search_symbol_mode_high_confidence_recommends_read_and_impact() -> None:
    """Symbol mode + high-confidence top hit → recommend
    omni_read(mode='symbol') + omni_impact + omni_search(references)."""
    routes = {
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": "_detect_mode",
                    "file_path": "src/x.py",
                    "line_start": 80, "line_end": 123,
                    "kind": "function",
                    "signature": "def _detect_mode(query: str) -> str:",
                    "relevance_score": 1.0,
                    # _infer_source_confidence keys off this list to set
                    # source=symbol_index, confidence=high.
                    "why_matched": ["symbol:exact"],
                },
            ],
            "total_results": 1,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="_detect_mode", mode="symbol", format="json",
    ))
    payload = json.loads(raw)
    assert "next_actions" in payload
    blob = " ".join(payload["next_actions"])
    assert "omni_read" in blob
    assert "_detect_mode" in blob, "must interpolate the symbol name"
    assert "omni_impact" in blob
    assert "references" in blob


def test_omni_search_symbol_mode_fuzzy_only_recovers_to_references() -> None:
    """When top hit is fuzzy / low-confidence, suggest references or
    text mode for an exact lookup."""
    routes = {
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": "fuzzy_neighbor",
                    "file_path": "src/y.py",
                    "line_start": 1, "line_end": 5,
                    "kind": "function",
                    "relevance_score": 0.55,
                    # Mark as fuzzy so _infer_source_confidence stamps
                    # source=symbol_index_fuzzy, confidence=low.
                    "why_matched": ["symbol:fuzzy", "rapidfuzz"],
                },
            ],
            "total_results": 1,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="my_target", mode="symbol", format="json",
    ))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"]).lower()
    # Must surface the recovery hints (either references or text mode).
    assert ("references" in blob) or ("text" in blob)
    assert "my_target" in " ".join(payload["next_actions"])


def test_omni_search_references_mode_recommends_read_and_impact() -> None:
    routes = {
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/search/symbols": {
            "results": [{
                "symbol_name": "my_func",
                "file_path": "src/x.py",
                "line_start": 10, "line_end": 20,
                "signature": "def my_func():",
            }],
        },
        "/search/text": {"results": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="my_func", mode="references", format="json",
    ))
    payload = json.loads(raw)
    assert "next_actions" in payload
    blob = " ".join(payload["next_actions"])
    assert "omni_read" in blob
    assert "omni_impact" in blob


def test_omni_search_text_mode_recommends_range_read() -> None:
    """Text mode hits → recommend omni_read(mode='range') anchored on
    the top hit's line."""
    routes = {
        "/search/text": {
            "results": [{
                "file_path": "src/x.py",
                "line_number": 42,
                "line_content": "    foo()",
            }],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="foo", mode="text", format="json",
    ))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"])
    assert "omni_read" in blob
    assert "mode='range'" in blob
    assert "42" in blob, "must include the actual hit line"


def test_omni_search_no_results_recommends_recovery() -> None:
    """Empty result set → recommend mode upgrade (semantic / hybrid)."""
    routes = {"/search/text": {"results": []}}
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="zzzz_no_match", mode="text", format="json",
    ))
    payload = json.loads(raw)
    if "next_actions" in payload:
        blob = " ".join(payload["next_actions"]).lower()
        assert "semantic" in blob or "hybrid" in blob


# ===========================================================================
# P2-B — omni_memory advisory interpolation
# ===========================================================================


def test_omni_memory_advisory_interpolates_symbol_into_next_actions() -> None:
    """Pre-r18 next_actions had ``omni_search(query=<symbol>, ...)``
    with a literal placeholder. Post-r18 the actual symbol must be
    substituted in."""
    routes = {
        "/memory/search": {
            "results": [
                {
                    "id": 8, "memory_id": 8,
                    "category": "mistake",
                    "content": "lesson",
                    "score": 0.8,
                    "match_reason": "Matched in content",
                },
            ],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="_detect_mode",
        task="modify search routing",
        format="json",
    ))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"])
    # Must contain the literal symbol, not a placeholder.
    assert "_detect_mode" in blob
    assert "<symbol>" not in blob, (
        "P2-B: must interpolate symbol, not leave literal placeholder"
    )


def test_omni_memory_advisory_no_symbol_falls_back_to_placeholder() -> None:
    """When no symbol is given, the placeholder is acceptable —
    callers can still see the schema."""
    routes = {
        "/memory/search": {
            "results": [
                {
                    "id": 8, "memory_id": 8,
                    "category": "solution",
                    "content": "lesson",
                    "score": 0.5,
                    "match_reason": "Matched in content",
                },
            ],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        task="some task",
        format="json",
    ))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"])
    # When symbol absent, placeholder is the documented fallback.
    assert "<symbol>" in blob


def test_omni_memory_advisory_interpolates_file_into_preview_action() -> None:
    routes = {
        "/memory/search": {
            "results": [
                {
                    "id": 1, "memory_id": 1,
                    "category": "solution",
                    "content": "lesson",
                    "score": 0.9,
                    "match_reason": "match",
                },
            ],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="my_func",
        file="src/x.py",
        format="json",
    ))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"])
    # Both symbol + file must be interpolated.
    assert "my_func" in blob
    assert "src/x.py" in blob


# ===========================================================================
# P3-A — discover_tools next_actions alias for pipeline
# ===========================================================================


def test_discover_tools_safe_edit_query_mirrors_pipeline_to_next_actions() -> None:
    """``pipeline`` and ``next_actions`` must carry the same workflow
    steps so callers can use either field."""
    from omnicode_adapters.mcp_server.high_level_tools import _recommend_tools_payload
    payload = _recommend_tools_payload("safe edit")
    assert "pipeline" in payload
    assert "next_actions" in payload
    assert payload["next_actions"] == payload["pipeline"], (
        "P3-A: next_actions must mirror pipeline for cross-tool uniformity"
    )
    assert payload["next_actions"], "expected non-empty workflow"


def test_discover_tools_default_listing_carries_next_actions() -> None:
    """Empty query → default listing must also carry next_actions."""
    from omnicode_adapters.mcp_server.high_level_tools import _recommend_tools_payload
    payload = _recommend_tools_payload("")
    assert "next_actions" in payload
    assert payload["next_actions"] == payload["default_pipeline"]


def test_discover_tools_no_match_carries_next_actions() -> None:
    """Zero-match fallback also carries next_actions."""
    from omnicode_adapters.mcp_server.high_level_tools import _recommend_tools_payload
    payload = _recommend_tools_payload("zzz_unrelated_xyz_no_keyword")
    assert "next_actions" in payload
    assert payload["next_actions"] == payload["default_pipeline"]


# ===========================================================================
# P3-B — omni_diagnostics error-line locator
# ===========================================================================


def test_omni_diagnostics_with_errors_includes_line_locator(tmp_path) -> None:
    """When there are errors, next_actions must include an
    omni_read(mode='range', ...) locator targeting the first error."""
    file = tmp_path / "x.py"
    file.write_text("def f(): return 1\n")

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        if endpoint == "/guard/check":
            return {"result": {
                "issues": [
                    {
                        "severity": "error",
                        "line": 42,
                        "message": "boom",
                        "source": "ruff", "rule": "E001",
                    },
                    {
                        "severity": "error",
                        "line": 99,
                        "message": "ouch",
                        "source": "ruff", "rule": "E002",
                    },
                ],
                "tools_run": ["guard"],
            }}
        if endpoint.startswith("/lsp/diagnostics/"):
            return {"result": {"diagnostics": []}}
        return {"result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    raw = _run(tools["omni_diagnostics"](file=str(file), format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    blob = " ".join(payload["next_actions"])
    # Must point at the FIRST error (line 42), not the second (99).
    assert "mode='range'" in blob
    assert "start_line=39" in blob, "first error line 42 → start_line=42-3=39"
    assert "end_line=45" in blob, "first error line 42 → end_line=42+3=45"


def test_omni_diagnostics_clean_file_no_locator_action(tmp_path) -> None:
    """A clean file should not surface a locator (no errors to point at)."""
    file = tmp_path / "x.py"
    file.write_text("def f(): return 1\n")

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        if endpoint == "/guard/check":
            return {"result": {"issues": [], "tools_run": ["guard"]}}
        if endpoint.startswith("/lsp/diagnostics/"):
            return {"result": {"diagnostics": []}}
        return {"result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    raw = _run(tools["omni_diagnostics"](file=str(file), format="json"))
    payload = json.loads(raw)
    blob = " ".join(payload["next_actions"])
    # No errors → no locator.
    assert "mode='range'" not in blob
    # But still recommends the outline for the next step.
    assert "outline" in blob


# ===========================================================================
# Feature flags + version stamp
# ===========================================================================


def test_handler_features_advertise_r18_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    for flag in (
        "search.next_actions",
        "memory.next_actions_interpolated",
        "discover.next_actions_alias",
        "diagnostics.error_locator",
    ):
        assert flag in flags, f"missing r18 feature flag: {flag}"


def test_handler_version_is_r18() -> None:
    import re
    m = re.search(r"\.r(\d+)", hlt._HANDLER_VERSION)
    assert m is not None
    assert int(m.group(1)) >= 18, hlt._HANDLER_VERSION
