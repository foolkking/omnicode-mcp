"""Boundary-contract tests for omni_impact.

Pinned by the audit:

* empty / whitespace symbol → structured ok=false (not a fake test list)
* symbol not in graph → risk='unknown' (not 'low')
* max_files smaller than backend minimum → MCP-side truncation
  (not an HTTP 422 leaked into ``note``)
* main happy path on _detect_mode (or any healthy graph symbol) still
  recommends the targeted test file
* every error path carries handler_version + contract_version

These tests stub the HTTP backend so they don't need a live FastAPI app.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# Helpers
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
    """Wire omni_impact + friends with a scripted ``make_request``.

    ``routes`` keys may be the full endpoint or its trailing path
    segment; values can be a dict, or a callable for inspecting the
    actual call args.
    """
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
        for candidate in (endpoint, key):
            if candidate in routes:
                payload = routes[candidate]
                if callable(payload):
                    payload = payload(method, endpoint, kwargs)
                return {"result": payload}
        return {"result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    # Expose the captured-args dict for assertions.
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Empty symbol → structured ok=false (no fake test list).
# ---------------------------------------------------------------------------


def test_omni_impact_empty_symbol_returns_structured_error() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_impact"](symbol="", format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "non-empty" in payload["error"].lower()
    assert payload["risk"] == "unknown"
    assert payload["confidence"] == "low"
    assert payload["callers"] == []
    assert payload["callees"] == []
    assert payload["files_involved"] == []
    assert payload["suggested_tests"] == []
    assert payload["suggested_commands"] == []
    assert payload.get("suggested_next_action"), payload
    # Audit rule: error path keeps the version stamps.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_impact"]


def test_omni_impact_whitespace_symbol_is_treated_as_empty() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_impact"](symbol="   \t\n", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["suggested_tests"] == []


def test_omni_impact_empty_symbol_does_not_call_backend() -> None:
    tools = _build_tools({})
    _run(tools["omni_impact"](symbol="", format="json"))
    captured = tools["__captured__"]
    # We must reject the empty-symbol call locally — never let it reach
    # /graph/* (which is what would surface the 44 unrelated tests).
    for endpoint in ("/graph/risk", "/graph/impact", "/graph/related-tests"):
        assert endpoint not in captured, (
            f"omni_impact should not have called {endpoint} for empty symbol; "
            f"captured: {captured}"
        )


# ---------------------------------------------------------------------------
# 2. Missing symbol in graph → risk='unknown'.
# ---------------------------------------------------------------------------


def test_omni_impact_missing_symbol_risk_unknown() -> None:
    """Even when /graph/risk reports risk='low' with 'No test coverage',
    omni_impact MUST override to risk='unknown' when the call graph
    yielded zero callers/callees AND zero files. 'No test coverage' for
    a non-existent symbol is not low risk; it's unknown."""
    routes = {
        "/graph/risk": {
            "risk": "low",
            "reasons": ["No test coverage found"],
        },
        "/graph/impact": {
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 0,
            "files_involved": [],
        },
        "/graph/related-tests": {
            "test_files": [],
            "suggested_commands": [],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="DefinitelyNotExist", format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["risk"] == "unknown", payload
    assert payload["risk_reasons"] == [], (
        "risk_reasons must be cleared when risk is overridden to unknown; "
        f"got {payload['risk_reasons']}"
    )
    assert payload["confidence"] == "low"
    assert payload["callers"] == []
    assert payload["callees"] == []
    note = (payload.get("note") or "").lower()
    assert "unknown" in note or "not found" in note, payload
    # Stamp present.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_impact"]


# ---------------------------------------------------------------------------
# 3. max_files smaller than backend minimum → truncated, no HTTP 422 leak.
# ---------------------------------------------------------------------------


def test_omni_impact_max_files_truncates_or_structured_error() -> None:
    """User passes max_files=5 but the backend has 122 files. We must
    NOT return ok=true with note='HTTP 422'. Instead:
      - truncate files_involved to 5
      - set truncated=true
      - put a clear note explaining the truncation
      - the backend was queried with max_files >= its floor, never the
        user's tiny value."""
    big_files = [f"path/file_{i:03d}.py" for i in range(122)]
    routes = {
        "/graph/risk": {"risk": "medium", "reasons": ["Affects 122 files"]},
        "/graph/impact": {
            "affected_symbols": ["a", "b"],
            "dependent_symbols": ["c"],
            "files_count": 122,
            "files_involved": big_files,
        },
        "/graph/related-tests": {
            "test_files": ["tests/unit/test_x.py"],
            "suggested_commands": ["pytest tests/unit/test_x.py"],
        },
    }
    tools = _build_tools(routes)
    raw = _run(
        tools["omni_impact"](symbol="big_symbol", max_files=5, format="json")
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["files_count"] == 122
    assert len(payload["files_involved"]) == 5
    assert payload["truncated"] is True
    note = (payload.get("note") or "").lower()
    assert "5" in note and "122" in note and "trunc" in note
    # Crucial: no leaked backend 422.
    assert "http 422" not in note
    assert "422" not in note

    # And the backend was queried with the clamped floor (>= 200), not
    # the user's tiny value. The floor matches the omni_impact default
    # max_files so high-fan-out graphs return useful data.
    captured = tools["__captured__"]
    for endpoint in ("/graph/risk", "/graph/impact", "/graph/related-tests"):
        assert endpoint in captured, captured
        sent = captured[endpoint][0].get("params", {})
        assert sent.get("max_files", 0) >= 200, (endpoint, sent)


def test_omni_impact_max_files_within_floor_not_truncated() -> None:
    """When max_files >= the actual file count, no truncation flag."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 3,
            "files_involved": ["a.py", "b.py", "c.py"],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="small", max_files=200, format="json"))
    payload = json.loads(raw)
    assert payload["truncated"] is False
    assert len(payload["files_involved"]) == 3


# ---------------------------------------------------------------------------
# 4b. Live-like truncation: backend returns 96 files (the production
# blast radius for _detect_mode), user asks for max_files=5.
# Pins the audit-bundle.r3 floor bump (_DEFAULT_IMPACT_MAX_FILES = 200)
# end-to-end:
#   * backend is asked for the floor (200) so the graph fully expands
#   * MCP layer truncates display to the user's max_files (5)
#   * truncated=true, files_count keeps the real total, note explains
# ---------------------------------------------------------------------------


def test_omni_impact_live_like_truncation_against_detect_mode_shape() -> None:
    """Mirror the production shape of _detect_mode (96 files, 3 callers,
    13 callees, suggested test = test_detect_mode.py). Pass max_files=5
    and verify the MCP-layer truncation contract."""
    files_involved = [f"path/file_{i:03d}.py" for i in range(96)]
    captured: Dict[str, List[Dict[str, Any]]] = {}

    def _impact_handler(method: str, endpoint: str, kwargs: Dict[str, Any]):
        # Mimic the real backend: only return data when max_files is
        # large enough for the graph to expand. At max_files<200 the
        # production /graph/impact returns zeros for high-fan-out
        # symbols. Asserting ``backend_max_files >= 200`` validates the
        # floor bump is in effect.
        captured.setdefault(endpoint, []).append(kwargs)
        sent = kwargs.get("params", {}) or {}
        sent_max = sent.get("max_files", 0)
        if sent_max < 200:
            return {
                "affected_symbols": [],
                "dependent_symbols": [],
                "files_count": 0,
                "files_involved": [],
            }
        return {
            "affected_symbols": [
                "split", "fullmatch", "len", "_strip_quotes",
                "_strip_python", "_strip_html", "_strip_sql",
                "_strip_c_like", "_strip_hash_family", "_normalize_language",
                "lower", "strip", "debug",
            ],
            "dependent_symbols": [
                "test_detect_mode_routing",
                "test_detect_mode_strips_quotes_before_routing",
                "omni_search",
            ],
            "files_count": 96,
            "files_involved": files_involved,
        }

    routes = {
        "/graph/risk": {
            "risk": "medium",
            "reasons": ["Affects 96 files", "Limited test coverage"],
        },
        "/graph/impact": _impact_handler,
        "/graph/related-tests": {
            "test_files": ["tests/unit/test_detect_mode.py"],
            "suggested_commands": ["pytest tests/unit/test_detect_mode.py"],
        },
    }
    tools = _build_tools(routes)
    raw = _run(
        tools["omni_impact"](
            symbol="_detect_mode", depth=3, max_files=5, format="json",
        )
    )
    payload = json.loads(raw)

    # Audit-bundle.r3 acceptance criteria.
    assert payload["ok"] is True
    assert payload["risk"] == "medium", (
        "risk MUST stay 'medium' for a real graph hit; the floor bump "
        "must let the backend return non-empty data so the missing-symbol "
        f"override does NOT fire. Got: {payload}"
    )
    assert payload["truncated"] is True
    assert len(payload["files_involved"]) <= 5
    assert payload["files_count"] == 96, "files_count must keep the real total"
    note = (payload.get("note") or "").lower()
    assert "5" in note and "96" in note and "trunc" in note, payload
    assert "http 422" not in note
    assert "tests/unit/test_detect_mode.py" in payload["suggested_tests"]
    assert any(
        cmd.startswith("pytest tests/unit/test_detect_mode.py")
        for cmd in payload["suggested_commands"]
    )

    # Backend was queried with the new floor (200), not the user's 5.
    impact_calls = tools["__captured__"].get("/graph/impact", [])
    assert impact_calls, tools["__captured__"]
    sent_max = impact_calls[0].get("params", {}).get("max_files", 0)
    assert sent_max >= 200, (
        f"backend max_files MUST be at the new floor (200), got {sent_max}"
    )


# ---------------------------------------------------------------------------
# 4. Main path: _detect_mode-style symbol still recommends its test file.
# ---------------------------------------------------------------------------


def test_omni_impact_main_path_detect_mode_still_recommends_test_detect_mode() -> None:
    """Regression guard for the audit's happy-path verdict. With a
    healthy call graph + tests endpoint, suggested_tests must include
    the targeted test file and suggested_commands must be runnable."""
    routes = {
        "/graph/risk": {
            "risk": "medium",
            "reasons": ["Affects 96 files", "Limited test coverage"],
        },
        "/graph/impact": {
            "affected_symbols": ["fullmatch", "split", "lower"],
            "dependent_symbols": [
                "test_detect_mode_routing",
                "omni_search",
            ],
            "files_count": 96,
            "files_involved": [f"f{i}.py" for i in range(20)],
        },
        "/graph/related-tests": {
            "test_files": ["tests/unit/test_detect_mode.py"],
            "suggested_commands": ["pytest tests/unit/test_detect_mode.py"],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="_detect_mode", format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["risk"] == "medium"
    assert "test_detect_mode_routing" in payload["callers"]
    assert "tests/unit/test_detect_mode.py" in payload["suggested_tests"]
    assert any(
        cmd.startswith("pytest tests/unit/test_detect_mode.py")
        for cmd in payload["suggested_commands"]
    )
    # audit-bundle.r16 (P3-A): wide graph (96 files) + builtin-style
    # callees (lower / split / fullmatch) → confidence is honestly
    # downgraded to ``medium`` with caveats explaining why. ``high`` is
    # now reserved for tight, clean graphs an AI editor can act on
    # without double-checking via omni_search/omni_read.
    assert payload["confidence"] == "medium"
    assert payload.get("confidence_caveats"), (
        "confidence_caveats must explain why a wide+noisy graph isn't 'high'"
    )
    caveats_blob = " ".join(payload["confidence_caveats"]).lower()
    assert (
        "transitive blast radius" in caveats_blob
        or "builtin" in caveats_blob
    )
    assert payload["truncated"] is False
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_impact"]


# ---------------------------------------------------------------------------
# 5. Every error / low-confidence path carries the version stamp.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_routes,call_kwargs,description",
    [
        (
            {},  # empty routes → empty backend responses
            {"symbol": "", "format": "json"},
            "empty symbol",
        ),
        (
            {
                "/graph/risk": {"risk": "low", "reasons": []},
                "/graph/impact": {
                    "affected_symbols": [],
                    "dependent_symbols": [],
                    "files_count": 0,
                    "files_involved": [],
                },
                "/graph/related-tests": {
                    "test_files": [], "suggested_commands": [],
                },
            },
            {"symbol": "missing_symbol_xyz", "format": "json"},
            "missing symbol",
        ),
        (
            {
                "/graph/risk": {"risk": "low", "reasons": []},
                "/graph/impact": {
                    "affected_symbols": [],
                    "dependent_symbols": [],
                    "files_count": 200,
                    "files_involved": [f"x{i}.py" for i in range(200)],
                },
                "/graph/related-tests": {
                    "test_files": [], "suggested_commands": [],
                },
            },
            {"symbol": "lots_of_files", "max_files": 3, "format": "json"},
            "max_files truncation",
        ),
    ],
)
def test_omni_impact_error_paths_include_contract_version(
    scenario_routes, call_kwargs, description,
):
    tools = _build_tools(scenario_routes)
    raw = _run(tools["omni_impact"](**call_kwargs))
    payload = json.loads(raw)
    assert payload.get("handler_version") == _HANDLER_VERSION, description
    assert payload.get("contract_version") == _CONTRACT_VERSIONS["omni_impact"], description
