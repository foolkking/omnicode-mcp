"""Contract tests for omni_intelligence resolution semantics
(audit-bundle.r13, P1-C and P1-D close).

Round 4 found omni_intelligence:
  * P1-C: missing symbol → ok=true, no symbol_resolution=not_found signal
  * P1-D: empty input    → ok=true with all-empty fields (should be ok=false)

Post-fix contract:
  * empty input (no file/symbol/task/query) → ok=false + error +
    suggested_next_action + alias envelope
  * missing symbol (impact/code_understanding/search all empty) →
    ok=true BUT symbol_resolution='not_found', confidence='low', note
    pointing at omni_context / omni_search
  * resolved symbol → symbol_resolution='found', confidence='high',
    no note
  * existing rich responses for real inputs are preserved
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _ALIAS_COMPAT_CONTRACT,
    _HANDLER_VERSION,
    register_high_level_tools,
)


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_tools(intelligence_payload: Dict[str, Any]) -> Dict[str, Callable[..., Any]]:
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        if endpoint == "/intelligence/context":
            # Backend usually returns success=true even when symbol is
            # missing — it just produces all-empty sub-blocks.
            return {"success": True, "result": intelligence_payload}
        return {"success": True, "result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# P1-D: empty input → ok=false
# ---------------------------------------------------------------------------


def test_omni_intelligence_empty_input_returns_error() -> None:
    """No file, no symbol, no task, no query → ok=false."""
    tools = _build_tools({})  # backend should not even be called
    raw = _run(tools["omni_intelligence"](
        task=None, file=None, symbol=None, query=None,
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    err = payload["error"].lower()
    assert "at least one" in err or "needs" in err
    # contract: must reference the modern alternative
    assert "omni_context" in payload["suggested_next_action"]
    # alias envelope
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_context"
    assert payload["use_instead"]
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT
    # Must guide the editor.
    assert payload.get("next_actions")
    # Backend NOT called.
    assert "/intelligence/context" not in tools["__captured__"]


def test_omni_intelligence_whitespace_only_inputs_count_as_empty() -> None:
    """All whitespace inputs are treated as empty too."""
    tools = _build_tools({})
    raw = _run(tools["omni_intelligence"](
        task="   ", file="\t", symbol="", query="\n",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "/intelligence/context" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# P1-C: missing symbol → symbol_resolution='not_found'
# ---------------------------------------------------------------------------


def test_omni_intelligence_missing_symbol_sets_symbol_resolution_not_found() -> None:
    """When the backend produces an effectively-empty payload for a
    symbol query, the alias must annotate symbol_resolution='not_found'
    + confidence='low' + a note pointing at omni_context/omni_search."""
    tools = _build_tools({
        "elapsed_ms": 5,
        "token_estimate": 0,
        "token_budget": 4096,
        "advisories": [],
        "capability_status": [],
        "code_understanding": {},
        "search": {"results": []},
        "impact": {"affected_count": 0, "dependent_count": 0,
                   "callers": [], "callees": []},
        "memory": {},
        "git_history": {},
        "errors": {},
    })
    raw = _run(tools["omni_intelligence"](
        symbol="DefinitelyNotExistSymbol123",
        task="unknown",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True  # backend ran, just empty
    assert payload["symbol_resolution"] == "not_found"
    assert payload["confidence"] == "low"
    note = payload.get("note") or ""
    assert "could not be resolved" in note.lower() \
        or "not be resolved" in note.lower()
    joined = " ".join(payload["next_actions"]).lower()
    assert "omni_search" in joined or "omni_context" in joined
    # alias envelope
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_context"


def test_omni_intelligence_resolved_symbol_marks_found() -> None:
    """A backend response with real impact / code_understanding / search
    rows means the symbol was resolved."""
    tools = _build_tools({
        "elapsed_ms": 12,
        "token_estimate": 280,
        "token_budget": 4096,
        "advisories": [],
        "capability_status": [],
        "code_understanding": {
            "symbols": [{"name": "f", "kind": "function"}],
            "file_path": "src/x.py",
        },
        "search": {"results": [{"file": "src/x.py", "line": 10}]},
        "impact": {
            "affected_count": 3, "dependent_count": 1,
            "callers": [{"name": "g"}], "callees": [],
        },
        "memory": {},
        "git_history": {},
        "errors": {},
    })
    raw = _run(tools["omni_intelligence"](
        symbol="f",
        task="audit",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"
    assert payload["confidence"] == "high"
    # No misleading note.
    assert payload.get("note") in (None, "")
    # alias envelope still present
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_context"


# ---------------------------------------------------------------------------
# Non-symbol input (file or task) does NOT get a symbol_resolution stamp
# ---------------------------------------------------------------------------


def test_omni_intelligence_file_only_input_skips_symbol_resolution() -> None:
    """When the caller doesn't pass a symbol, omni_intelligence should
    not invent a symbol_resolution field — that signal is symbol-scoped."""
    tools = _build_tools({
        "elapsed_ms": 7,
        "token_budget": 4096,
        "advisories": [],
        "capability_status": [],
        "code_understanding": {"file_path": "src/y.py", "symbols": []},
        "search": {"results": []},
        "impact": {},
        "memory": {},
        "git_history": {},
        "errors": {},
    })
    raw = _run(tools["omni_intelligence"](
        file="src/y.py",
        task="explain this file",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "symbol_resolution" not in payload
    assert payload["deprecated"] is True


# ---------------------------------------------------------------------------
# handler_features stamps the new flag
# ---------------------------------------------------------------------------


def test_handler_features_advertise_intelligence_resolution() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "alias.intelligence_resolution" in flags
