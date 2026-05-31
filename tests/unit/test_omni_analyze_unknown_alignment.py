"""Contract tests for omni_analyze missing-symbol alignment with
omni_impact (audit-bundle.r13, P1-A and P1-B close).

Round 4 found omni_analyze:
  * P1-A: missing symbol → risk='low'  (should be 'unknown')
  * P1-B: empty symbol   → risk='low'  (should be ok=false)

Post-fix contract:
  * empty/whitespace symbol → ok=false, error explicit, alias envelope
  * missing symbol (no callers, no callees, no edges) → ok=true,
    risk='unknown', confidence='low', note pointing at omni_search
  * valid symbol           → previous risk-banded behaviour preserved
  * alias envelope (deprecated/replacement/use_instead) preserved
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


# ---------------------------------------------------------------------------
# P1-A: missing symbol → risk='unknown'
# ---------------------------------------------------------------------------


def test_omni_analyze_missing_symbol_returns_unknown() -> None:
    """When the call graph has neither callers nor callees AND
    total_edges is 0, risk MUST be 'unknown' — not 'low'."""
    tools = _build_tools({
        "/search/symbols/relations": {
            "callers": {"count": 0, "names": []},
            "callees": {"count": 0, "names": []},
            "total_edges": 0,
        },
    })
    raw = _run(tools["omni_analyze"](
        symbol="DefinitelyNotExistSymbol123",
        analysis="impact",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["risk"] == "unknown", payload
    assert payload["confidence"] == "low"
    assert payload.get("note")
    assert "not found" in payload["note"].lower() or "unknown" in payload["note"].lower()
    # Must recommend omni_search to confirm.
    joined = " ".join(payload["next_actions"]).lower()
    assert "omni_search" in joined
    assert "omni_impact" in joined  # also recommend the modern entry point
    # alias envelope
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_impact"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT
    assert payload["handler_version"] == _HANDLER_VERSION


# ---------------------------------------------------------------------------
# P1-B: empty / whitespace symbol → ok=false
# ---------------------------------------------------------------------------


def test_omni_analyze_empty_symbol_returns_error() -> None:
    """Empty symbol must be rejected up front, before any backend call,
    with a structured ok=false envelope."""
    tools = _build_tools({})  # no routes — backend must NOT be called
    for empty in ("", "   ", "\t\n"):
        raw = _run(tools["omni_analyze"](
            symbol=empty,
            analysis="impact",
            format="json",
        ))
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "non-empty" in payload["error"].lower() or \
            "empty" in payload["error"].lower()
        # alias envelope
        assert payload["deprecated"] is True
        assert payload["replacement"] == "omni_impact"
        # next_actions must point at omni_impact + omni_search
        joined = " ".join(payload["next_actions"]).lower()
        assert "omni_impact" in joined
        assert "omni_search" in joined
    # NO backend hit
    assert "/search/symbols/relations" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# Sanity: a valid symbol still returns a real risk band
# ---------------------------------------------------------------------------


def test_omni_analyze_valid_symbol_preserves_risk_banding() -> None:
    """Existing behaviour for a real symbol must not regress."""
    tools = _build_tools({
        "/search/symbols/relations": {
            "callers": {"count": 5, "names": ["a", "b", "c", "d", "e"]},
            "callees": {"count": 2, "names": ["x", "y"]},
            "total_edges": 7,
        },
    })
    raw = _run(tools["omni_analyze"](
        symbol="my_real_function",
        analysis="impact",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    # 5 callers → "medium" risk band per the existing thresholds
    assert payload["risk"] == "medium"
    assert payload["confidence"] in ("high", "medium")
    assert payload.get("note") is None or payload.get("note") == ""
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_impact"


# ---------------------------------------------------------------------------
# handler_features stamps the new flag
# ---------------------------------------------------------------------------


def test_handler_features_advertise_analyze_unknown_alignment() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "alias.analyze_unknown_alignment" in flags
