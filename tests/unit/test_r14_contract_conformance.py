"""Contract conformance tests for audit-bundle.r14 — 1 P1 + 5 P2 fixes.

Pinned by Round 4 contract evaluation:

* P1   omni_edit preview/rollback now lift backend ``message`` to the
       canonical top-level ``error`` field when ok=false.
* P2-1 omni_impact JSON envelope ships top-level ``next_actions``
       on success / missing-symbol / error / empty-symbol branches.
* P2-2 omni_diagnostics ships the singular ``source`` (alongside the
       legacy ``sources``) and a top-level ``next_actions`` list.
* P2-3 omni_patch sessions response carries ``truncated`` +
       ``total_count`` + ``limit`` so long-running workspaces don't
       silently drop rows.
* P2-4 omni_analyze callers / callees / graph branches stamp ``source``
       (the impact branch already had it; the others didn't).
* P2-5 omni_intelligence numeric memory confidence is normalised to
       the canonical {high, medium, low} band at the top level, with
       the raw float preserved as ``memory.confidence_score``. Adds a
       ``truncated`` flag.
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


# ---------------------------------------------------------------------------
# Reusable shim — same pattern the r13 tests use.
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
        # /intelligence/context expects success at the top level.
        if endpoint == "/intelligence/context":
            return {"success": True, "result": payload}
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# P1 — omni_edit error field alignment
# ===========================================================================


def test_omni_edit_preview_lifts_backend_message_to_error() -> None:
    """When the backend reports a preview failure (e.g. File not found),
    omni_edit must surface the message under the canonical top-level
    ``error`` key, not only under ``message``."""
    routes = {
        "/patch/preview": {
            "success": False,
            "message": "File not found: tests/no_such_file.py",
            "diff": None,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_edit"](
        action="preview",
        file="tests/no_such_file.py",
        content="print(1)\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "error" in payload, "P1: top-level 'error' missing on preview failure"
    assert "not found" in payload["error"].lower()
    # Back-compat: message preserved.
    assert payload["message"] == "File not found: tests/no_such_file.py"
    # Recovery next_actions
    joined = " ".join(payload["next_actions"]).lower()
    assert "omni_read" in joined or "omni_search" in joined
    # Alias envelope intact
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"


def test_omni_edit_rollback_lifts_backend_message_to_error() -> None:
    """Same contract for rollback: backend failure message → top-level error."""
    routes = {
        "/patch/rollback": {
            "success": False,
            "message": "Session not found: bad-id",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_edit"](
        action="rollback",
        session_id="bad-id",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "error" in payload
    assert "not found" in payload["error"].lower()
    assert payload["rolled_back"] is False
    assert payload["deprecated"] is True


def test_omni_edit_preview_success_does_not_inject_error() -> None:
    """Don't pollute success responses with a stray error field."""
    routes = {
        "/patch/preview": {
            "success": True,
            "message": "Preview ready: +1/-0 lines",
            "diff": "+print(1)\n",
            "lines_added": 1,
            "lines_removed": 0,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_edit"](
        action="preview",
        file="tests/x.py",
        content="print(1)\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "error" not in payload
    assert payload["lines_added"] == 1
    assert payload["deprecated"] is True


# ===========================================================================
# P2-1 — omni_impact next_actions
# ===========================================================================


def test_omni_impact_success_has_top_level_next_actions() -> None:
    """A successful impact analysis must surface next_actions at the
    top level so AI editors get a ready-to-run follow-up."""
    routes = {
        "/graph/risk": {"risk": "medium", "reasons": ["Affects 12 files"]},
        "/graph/impact": {
            "affected_symbols": ["a", "b"],
            "dependent_symbols": ["c"],
            "files_count": 12,
            "files_involved": ["src/a.py", "src/b.py"],
        },
        "/graph/related-tests": {
            "test_files": ["tests/test_x.py"],
            "suggested_commands": ["pytest tests/test_x.py"],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="my_func", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert isinstance(payload.get("next_actions"), list)
    assert len(payload["next_actions"]) > 0
    joined = " ".join(payload["next_actions"]).lower()
    # Should reference the suggested command for test runs.
    assert "pytest" in joined or "test_x" in joined or "advisory" in joined


def test_omni_impact_missing_symbol_has_recovery_next_actions() -> None:
    """When the call graph yields nothing, next_actions must steer the
    caller to omni_search to confirm the symbol exists."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 0,
            "files_involved": [],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="GhostSymbol", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["risk"] == "unknown"
    assert payload["confidence"] == "low"
    joined = " ".join(payload["next_actions"]).lower()
    assert "omni_search" in joined


def test_omni_impact_empty_symbol_has_next_actions() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_impact"](symbol="", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload.get("next_actions"), "empty-symbol guard must include next_actions"


# ===========================================================================
# P2-2 — omni_diagnostics source alignment
# ===========================================================================


def test_omni_diagnostics_exposes_singular_source(tmp_path) -> None:
    """omni_diagnostics must emit ``source`` (singular) alongside the
    legacy ``sources`` plural for contract parity with the rest of the
    surface."""
    file = tmp_path / "x.py"
    file.write_text("def f():\n    return 1\n")

    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        if endpoint == "/diagnostics/file":
            return {"result": {"diagnostics": [], "tools_run": ["guard"]}}
        return {"result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    raw = _run(tools["omni_diagnostics"](file=str(file), format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    # P2-2: BOTH keys present.
    assert "source" in payload, "missing canonical singular 'source'"
    assert "sources" in payload, "lost legacy 'sources' (back-compat)"
    # And next_actions is non-empty.
    assert isinstance(payload.get("next_actions"), list)
    assert len(payload["next_actions"]) > 0


# ===========================================================================
# P2-3 — omni_patch sessions truncation transparency
# ===========================================================================


def test_omni_patch_sessions_carries_truncation_metadata() -> None:
    """sessions response must carry ``truncated`` + ``limit`` +
    ``total_count`` so the caller knows the page size."""
    # Backend returns a page of 20 + total_count > 20 → truncated.
    sample_session = {
        "session_id": "x", "file_path": "tests/x.py",
        "timestamp": "2026-05-30T10:00:00",
        "lines_added": 1, "lines_removed": 0,
        "applied": True, "rolled_back": False,
    }
    routes = {
        "/patch/sessions": {
            "sessions": [sample_session for _ in range(20)],
            "total_count": 73,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["count"] == 20
    assert payload["limit"] == 20
    assert payload["total_count"] == 73
    assert payload["truncated"] is True
    # First next_action should announce the truncation.
    first = (payload["next_actions"] or [""])[0]
    assert "truncated" in first.lower() or "73" in first


def test_omni_patch_sessions_not_truncated_when_under_limit() -> None:
    sample_session = {
        "session_id": "x", "file_path": "tests/x.py",
        "timestamp": "2026-05-30T10:00:00",
        "lines_added": 1, "lines_removed": 0,
        "applied": True, "rolled_back": False,
    }
    routes = {
        "/patch/sessions": {
            "sessions": [sample_session for _ in range(3)],
            "total_count": 3,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    assert payload["truncated"] is False
    assert payload["total_count"] == 3
    assert payload["count"] == 3


# ===========================================================================
# P2-4 — omni_analyze source alignment
# ===========================================================================


def test_omni_analyze_callers_branch_stamps_source() -> None:
    """The callers / callees / impact branches of the alias must all
    stamp ``source`` so the field is consistent across analyses."""
    routes = {
        "/search/symbols/relations": {
            "callers": {"count": 3, "names": ["a", "b", "c"]},
            "callees": {"count": 1, "names": ["d"]},
            "total_edges": 4,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_analyze"](
        symbol="my_func",
        analysis="callers",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["analysis"] == "callers"
    assert payload["source"] == "graph", "P2-4: callers branch missing 'source'"
    assert payload["confidence"] in ("high", "medium")
    assert payload["deprecated"] is True


def test_omni_analyze_graph_branch_stamps_source_and_confidence() -> None:
    routes = {
        "/search/symbols/graph": {
            "summary": {"total_edges": 42, "total_callers": 10, "total_callees": 8},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_analyze"](
        symbol="my_func",
        analysis="graph",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["analysis"] == "graph"
    assert payload["source"] == "graph"
    assert payload["confidence"] in ("high", "low")


# ===========================================================================
# P2-5 — omni_intelligence confidence normalisation
# ===========================================================================


def test_omni_intelligence_normalises_numeric_confidence_to_band() -> None:
    """The intelligence backend ships ``memory.confidence`` as a raw
    float. The alias layer must convert it to the canonical
    {high, medium, low} band at the top level, while preserving the
    raw float as memory.confidence_score."""
    routes = {
        "/intelligence/context": {
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
            "impact": {"affected_count": 1, "dependent_count": 0,
                       "callers": [{"name": "g"}], "callees": []},
            "memory": {
                "advisory": "some lesson",
                "memories_used": [8, 4],
                "confidence": 0.767,  # raw float
                "signals_matched": ["task"],
            },
            "git_history": {},
            "errors": {},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_intelligence"](symbol="f", task="audit"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    # Memory blob: original score preserved, band overrides confidence
    mem = payload["memory"]
    assert mem["confidence_score"] == pytest.approx(0.767)
    assert mem["confidence"] == "high"  # 0.767 >= 0.75 → high
    # Symbol resolution still wins at top level
    assert payload["symbol_resolution"] == "found"
    assert payload["confidence"] == "high"


def test_omni_intelligence_low_band_for_weak_memory() -> None:
    routes = {
        "/intelligence/context": {
            "elapsed_ms": 5,
            "token_estimate": 50,
            "token_budget": 4096,
            "advisories": [],
            "capability_status": [],
            "code_understanding": {
                "symbols": [{"name": "f", "kind": "function"}],
                "file_path": "src/x.py",
            },
            "search": {"results": [{"file": "src/x.py", "line": 1}]},
            "impact": {"affected_count": 1, "dependent_count": 0,
                       "callers": [{"name": "g"}], "callees": []},
            "memory": {"confidence": 0.2, "memories_used": []},
            "git_history": {}, "errors": {},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_intelligence"](symbol="f", task="x"))
    payload = json.loads(raw)
    assert payload["memory"]["confidence"] == "low"
    assert payload["memory"]["confidence_score"] == pytest.approx(0.2)


def test_omni_intelligence_medium_band() -> None:
    routes = {
        "/intelligence/context": {
            "elapsed_ms": 5, "token_estimate": 50, "token_budget": 4096,
            "advisories": [], "capability_status": [],
            "code_understanding": {"symbols": [{"name": "f"}], "file_path": "x.py"},
            "search": {"results": [{"file": "x.py", "line": 1}]},
            "impact": {"affected_count": 1, "dependent_count": 0,
                       "callers": [{"name": "g"}], "callees": []},
            "memory": {"confidence": 0.5, "memories_used": []},
            "git_history": {}, "errors": {},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_intelligence"](symbol="f", task="x"))
    payload = json.loads(raw)
    assert payload["memory"]["confidence"] == "medium"


def test_omni_intelligence_carries_truncated_flag() -> None:
    """omni_intelligence response must carry a top-level ``truncated``
    flag for contract parity with token-bearing tools."""
    routes = {
        "/intelligence/context": {
            "elapsed_ms": 5,
            "token_estimate": 5000,  # exceeds budget
            "token_budget": 4096,
            "advisories": [], "capability_status": [],
            "code_understanding": {}, "search": {}, "impact": {},
            "memory": {}, "git_history": {}, "errors": {},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_intelligence"](task="anything"))
    payload = json.loads(raw)
    assert "truncated" in payload
    assert payload["truncated"] is True


def test_omni_intelligence_truncated_false_when_under_budget() -> None:
    routes = {
        "/intelligence/context": {
            "elapsed_ms": 5,
            "token_estimate": 100,
            "token_budget": 4096,
            "advisories": [], "capability_status": [],
            "code_understanding": {}, "search": {}, "impact": {},
            "memory": {}, "git_history": {}, "errors": {},
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_intelligence"](task="anything"))
    payload = json.loads(raw)
    assert payload["truncated"] is False


# ===========================================================================
# Feature flags advertised
# ===========================================================================


def test_handler_features_advertise_r14_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    for flag in (
        "edit.error_field_alignment",
        "impact.next_actions",
        "diagnostics.source_alignment",
        "patch.sessions_truncation",
        "alias.analyze_source_alignment",
        "alias.intelligence_confidence_normalised",
    ):
        assert flag in flags, f"missing r14 feature flag: {flag}"


def test_handler_version_is_r14() -> None:
    import re
    m = re.search(r"\.r(\d+)", hlt._HANDLER_VERSION)
    assert m is not None
    assert int(m.group(1)) >= 14, hlt._HANDLER_VERSION
