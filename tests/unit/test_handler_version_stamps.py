"""Contract tests for handler_version + contract_version stamping.

Pinned by the audit-bundle update:

* every flagship tool's JSON envelope MUST include both fields
* omni_read's contract_version distinguishes the diagnostics-aligned build
* omni_search's contract_version distinguishes the source/confidence build
* omni_status reports the per-tool expected_contract_versions table

Stale-binding detection in practice: a caller compares
``response.contract_version`` against ``omni_status().expected_contract_versions[tool]``.
A mismatch means the FastMCP host is serving an old handler closure even
though the module-level helpers reloaded. That's exactly the failure
mode that bit the late-May 2026 audit.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim + scripted make_request
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
    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
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
    return mcp.tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Every core tool that returns JSON includes handler_version + contract_version.
# ---------------------------------------------------------------------------


def _diag_routes() -> Dict[str, Any]:
    return {
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/x.py": {"diagnostics": []},
    }


def _build_full_tools_with_routes() -> Dict[str, Callable[..., Any]]:
    """Wire enough routes for every flagship tool to return its happy-path JSON."""
    routes: Dict[str, Any] = {
        # search backends
        "/search/symbols": {
            "results": [
                {
                    "symbol_name": "_detect_mode",
                    "file_path": "x.py",
                    "line_start": 1,
                    "line_end": 2,
                    "signature": "def _detect_mode(): ...",
                    "symbol_type": "function",
                    "relevance_score": 1.0,
                    "why_matched": ["symbol:exact"],
                }
            ],
            "total_results": 1,
        },
        "/search": {"results": [], "total_results": 0},
        "/search/text": {"results": [], "total_results": 0},
        # read backend (outline)
        "/read": {
            "language": "python",
            "total_lines": 10,
            "symbols": [{"name": "foo", "kind": "function", "lines": [1, 5]}],
            "symbol_count": 1,
        },
        # diagnostics backends
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/x.py": {"diagnostics": []},
        # impact backends
        "/graph/impact": {
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 0,
            "files_involved": [],
        },
        "/graph/risk": {"level": "low", "reasons": []},
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
        # patch backends — keep them empty so we hit happy-path validation.
        "/patch/preview": {"success": True, "diff": "", "lines_added": 0, "lines_removed": 0},
        "/patch/validate": {"success": True, "checks": []},
        "/patch/apply": {"success": True, "session_id": "s1", "lines_added": 0, "lines_removed": 0},
        "/patch/rollback": {"success": True, "message": "rolled back"},
        "/patch/sessions": {"sessions": []},
        # memory backends
        "/memory/search": {"results": []},
        "/memory/store": {"memory_id": "m1"},
        "/memory/context": {},
        "/memory/advisory": {"advisory": "no advisory"},
    }
    return _build_tools(routes)


@pytest.mark.parametrize(
    "tool_name,call",
    [
        ("omni_search", lambda t: t["omni_search"](query="_detect_mode", mode="symbol", format="json")),
        ("omni_read", lambda t: t["omni_read"](file="x.py", mode="outline", format="json")),
        ("omni_diagnostics", lambda t: t["omni_diagnostics"](file="x.py", format="json")),
        ("omni_patch", lambda t: t["omni_patch"](action="sessions", format="json")),
        ("omni_memory", lambda t: t["omni_memory"](action="context", format="json")),
        ("omni_skill", lambda t: t["omni_skill"](action="list", format="json")),
        ("omni_status", lambda t: t["omni_status"]()),
    ],
)
def test_core_tools_include_handler_version_in_json(tool_name, call):
    tools = _build_full_tools_with_routes()
    raw = _run(call(tools))
    payload = json.loads(raw)
    assert payload.get("handler_version") == _HANDLER_VERSION, (
        tool_name, payload.get("handler_version")
    )
    assert payload.get("contract_version") == _CONTRACT_VERSIONS[tool_name], (
        tool_name, payload.get("contract_version")
    )


def test_core_tools_include_contract_version_on_error_paths():
    """Stamping must also reach error envelopes — that's where stale
    handlers most often diverge silently."""
    tools = _build_full_tools_with_routes()
    # omni_read[range] without start_line → structured error.
    raw = _run(tools["omni_read"](file="x.py", mode="range", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_read"]


# ---------------------------------------------------------------------------
# 2. omni_read contract_version specifically distinguishes diagnostics-aligned.
# ---------------------------------------------------------------------------


def test_omni_read_contract_version_is_diagnostics_aligned():
    tools = _build_tools(_diag_routes())
    raw = _run(tools["omni_read"](file="x.py", mode="diagnostics", format="json"))
    payload = json.loads(raw)
    assert payload["contract_version"] == "read.diagnostics_aligned.v1"
    # The diagnostics-aligned envelope MUST carry the fields that gave
    # this contract version its name.
    for required in (
        "counts", "total_count", "severity_filter",
        "sources", "tools_run", "tools_skipped", "truncated",
    ):
        assert required in payload, f"missing {required} on read.diagnostics_aligned.v1"


def test_omni_read_legacy_contract_version_string_value():
    """Smoke: the string used in the contract table is the canonical one
    so a future read-tool refactor doesn't accidentally rebrand it."""
    assert _CONTRACT_VERSIONS["omni_read"] == "read.diagnostics_aligned.v1"


# ---------------------------------------------------------------------------
# 3. omni_search contract_version specifically distinguishes source_confidence.
# ---------------------------------------------------------------------------


def test_omni_search_contract_version_is_source_confidence():
    tools = _build_full_tools_with_routes()
    raw = _run(
        tools["omni_search"](query="_detect_mode", mode="symbol", format="json")
    )
    payload = json.loads(raw)
    assert payload["contract_version"] == "search.source_confidence.v1"
    # Every result row must carry the source/confidence fields the
    # contract version is named after.
    for row in payload["results"]:
        assert row.get("source"), row
        assert row.get("confidence"), row


def test_omni_search_legacy_contract_version_string_value():
    assert _CONTRACT_VERSIONS["omni_search"] == "search.source_confidence.v1"


# ---------------------------------------------------------------------------
# 4. omni_status reports expected_contract_versions for every flagship tool.
# ---------------------------------------------------------------------------


def test_omni_status_expected_contract_versions_present():
    tools = _build_tools({})
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    expected = payload.get("expected_contract_versions")
    assert isinstance(expected, dict), payload
    # Required tools must be in the table with the published versions.
    assert expected["omni_search"] == "search.source_confidence.v1"
    assert expected["omni_read"] == "read.diagnostics_aligned.v1"
    assert expected["omni_diagnostics"] == "diagnostics.shared_envelope.v1"
    # And a sanity check: every flagship tool has a contract row.
    for tool in (
        "omni_search", "omni_read", "omni_impact",
        "omni_diagnostics", "omni_patch", "omni_memory",
        "omni_context", "omni_skill", "omni_status", "discover_tools",
    ):
        assert tool in expected, tool
        assert expected[tool], f"empty contract_version for {tool}"
    # And the omni_status response is itself stamped.
    assert payload.get("contract_version") == "status.v1"
    assert payload.get("handler_version") == _HANDLER_VERSION


def test_handler_version_in_status_matches_module_constant():
    tools = _build_tools({})
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    assert payload["handler_version"] == hlt._HANDLER_VERSION
