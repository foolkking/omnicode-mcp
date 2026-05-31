"""Contract tests for audit-bundle.r15 — Round 5 error-input conformance.

Pinned by the Round 5 audit (3 fixes):

* P1   omni_patch rollback failures (e.g. "Session not found") now
       lift the backend ``message`` to the canonical top-level
       ``error`` key. Mirrors the r14 omni_edit fix.
* P2   omni_context returns ``ok=false`` + top-level ``error`` +
       ``file_status="not_found"`` when ``file=`` was supplied but the
       file cannot be resolved. Drops the unrelated memory advisory
       so the response stops presenting phantom lessons under a
       failed call.
* P3   omni_impact JSON envelope ships the canonical
       ``symbol_resolution`` field ('found' / 'not_found' / 'n/a')
       for cross-tool parity with omni_intelligence and omni_context.
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


# ---------------------------------------------------------------------------
# FastMCP shim + scripted backend (matches the pattern of prior rounds).
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
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# P1 — omni_patch rollback error-field alignment
# ===========================================================================


def test_omni_patch_rollback_session_not_found_lifts_error() -> None:
    """Backend says "Session not found" → top-level ``error`` field
    present; ``message`` preserved for back-compat."""
    routes = {
        "/patch/sessions": {"sessions": []},  # no matching session in lookup
        "/patch/rollback": {
            "success": False,
            "message": "Session not found: bogus-id",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="rollback",
        session_id="bogus-id",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "error" in payload, "P1: top-level 'error' missing on rollback failure"
    assert "not found" in payload["error"].lower()
    # Back-compat: message still present.
    assert payload["message"] == "Session not found: bogus-id"
    assert payload["rolled_back"] is False
    # Recovery next_actions
    joined = " ".join(payload["next_actions"]).lower()
    assert "sessions" in joined  # nudge towards omni_patch(action='sessions')


def test_omni_patch_rollback_already_rolled_back_lifts_error() -> None:
    routes = {
        "/patch/sessions": {"sessions": [
            {"session_id": "x", "file_path": "tests/x.py"},
        ]},
        "/patch/rollback": {
            "success": False,
            "message": "Session already rolled back",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="rollback",
        session_id="x",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "error" in payload
    assert "already" in payload["error"].lower()


def test_omni_patch_rollback_success_does_not_inject_error() -> None:
    """Don't pollute success responses with a stray error field."""
    routes = {
        "/patch/sessions": {"sessions": [
            {"session_id": "ok", "file_path": "tests/x.py"},
        ]},
        "/patch/rollback": {
            "success": True,
            "message": "Rolled back tests/x.py",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="rollback",
        session_id="ok",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "error" not in payload
    assert payload["rolled_back"] is True


# ===========================================================================
# P2 — omni_context file-existence guard
# ===========================================================================


def _omni_context_tools_for_missing_file() -> Dict[str, Callable[..., Any]]:
    """Backend payloads that simulate a non-existent file:
       /read returns empty (no symbols), /guard/check + /lsp/diagnostics
       report file-not-found via an issues row, /git/status returns
       nothing useful, memory has weak hits unrelated to the requested
       file."""
    file_not_found_blob = {
        "issues": [
            {
                "source": "guard",
                "severity": "error",
                "message": "File not found: not_exist_file_r15.py",
            },
        ],
        "tools_run": ["guard"],
    }
    routes = {
        "/read": {"language": "python", "symbols": [], "total_lines": 0},
        "/guard/check": file_not_found_blob,
        # /lsp/diagnostics/<file> — register both the prefix and the
        # full path so the dispatcher's exact-match falls through.
        "/lsp/diagnostics/not_exist_file_r15.py": {
            "diagnostics": [],
        },
        "/lsp/diagnostics/ghost.py": {"diagnostics": []},
        "/git/status": {"status": {"modified_files": [], "untracked_files": []}},
        "/memory/search": {
            "results": [
                {
                    "id": 8,
                    "memory_id": 8,
                    "category": "mistake",
                    "content": "unrelated lesson",
                    "tags": ["search"],
                    "score": 0.5,
                    "match_reason": "Matched in content",
                    "match_fields": [],
                },
            ],
        },
        "/memory/advisory": {"advisory": "...", "memories_used": [8]},
        "/symbols/find": {"results": []},
        "/lsp/definitions": {"locations": [], "available": True},
    }
    return _build_tools(routes)


def test_omni_context_missing_file_returns_ok_false() -> None:
    """Caller passed file= but it doesn't exist → ok=false + top-level
    error + file_status='not_found'. No phantom advisory."""
    tools = _omni_context_tools_for_missing_file()
    raw = _run(tools["omni_context"](
        file="not_exist_file_r15.py",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False, "P2: missing file must yield ok=false"
    assert "error" in payload
    assert "not found" in payload["error"].lower()
    assert payload["file_status"] == "not_found"
    # next_actions must steer towards omni_read / omni_search recovery.
    joined = " ".join(payload["next_actions"]).lower()
    assert "omni_read" in joined or "omni_search" in joined
    # Memory rows must be empty (no phantom lessons).
    assert payload["context"]["memories"] == []
    assert payload["memory_status"]["memory_count"] == 0
    assert payload["memory_status"].get("ran") is False
    # Stamps still present.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == "context.v2"


def test_omni_context_missing_file_drops_phantom_memories() -> None:
    """Specifically guard against the Round 5 bug where
    omni_context returned 5 unrelated memory rows under ok=true."""
    tools = _omni_context_tools_for_missing_file()
    raw = _run(tools["omni_context"](
        file="ghost.py",
        format="json",
    ))
    payload = json.loads(raw)
    # No memory rows snuck in.
    assert payload["context"]["memories"] == []
    # And memory_status reflects the skip with a clear reason.
    assert "skipped" in payload["memory_status"].get("reason", "").lower()


def test_omni_context_with_real_file_still_returns_ok_true(tmp_path) -> None:
    """A real file must still produce ok=true with a populated
    response. Regression guard for the new file-existence check."""
    real_file = tmp_path / "real.py"
    real_file.write_text("def f():\n    return 1\n")

    routes = {
        "/read": {
            "language": "python",
            "total_lines": 2,
            "symbols": [
                {"name": "f", "kind": "function", "lines": [1, 2],
                 "signature": "def f():"},
            ],
        },
        "/diagnostics/file": {
            "diagnostics": [],
            "tools_run": ["guard"],
        },
        "/git/status": {"status": {"modified_files": []}},
        "/memory/search": {"results": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        file=str(real_file),
        format="json",
    ))
    payload = json.loads(raw)
    # Should NOT be flagged as not_found.
    assert payload.get("file_status") != "not_found"
    assert payload["ok"] is True


def test_omni_context_no_file_param_is_unaffected() -> None:
    """When the caller doesn't pass file=, the new guard must not
    fire — that mode of omni_context is task-only and legitimately
    has no anchor file."""
    routes = {
        "/symbols/find": {"results": []},
        "/memory/search": {"results": []},
        "/git/status": {"status": {"modified_files": []}},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        task="explain something",
        format="json",
    ))
    payload = json.loads(raw)
    # No file passed → no file_status field should be added.
    assert payload.get("file_status") != "not_found"
    assert payload["ok"] is True


# ===========================================================================
# P3 — omni_impact symbol_resolution parity
# ===========================================================================


def test_omni_impact_resolved_symbol_marks_found() -> None:
    routes = {
        "/graph/risk": {"risk": "medium", "reasons": ["Affects 5 files"]},
        "/graph/impact": {
            "affected_symbols": ["a"], "dependent_symbols": ["b"],
            "files_count": 5, "files_involved": ["src/x.py"],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="my_func", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"


def test_omni_impact_missing_symbol_marks_not_found() -> None:
    """Missing symbol must surface symbol_resolution='not_found' for
    parity with omni_intelligence and omni_context."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            "affected_symbols": [], "dependent_symbols": [],
            "files_count": 0, "files_involved": [],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="GhostSymbol", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["risk"] == "unknown"
    assert payload["symbol_resolution"] == "not_found"
    assert payload["confidence"] == "low"


def test_omni_impact_empty_symbol_marks_na() -> None:
    """Empty / whitespace symbol → ok=false + symbol_resolution='n/a'."""
    tools = _build_tools({})
    raw = _run(tools["omni_impact"](symbol="", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["symbol_resolution"] == "n/a"


# ===========================================================================
# Feature flags advertised + version stamp
# ===========================================================================


def test_handler_features_advertise_r15_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    for flag in (
        "patch.rollback_error_alignment",
        "context.file_existence_guard",
        "impact.symbol_resolution_field",
    ):
        assert flag in flags, f"missing r15 feature flag: {flag}"


def test_handler_version_is_r15() -> None:
    import re
    m = re.search(r"\.r(\d+)", hlt._HANDLER_VERSION)
    assert m is not None
    assert int(m.group(1)) >= 15, hlt._HANDLER_VERSION
