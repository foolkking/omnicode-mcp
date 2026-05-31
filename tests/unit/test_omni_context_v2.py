"""Contract tests for omni_context v2 (audit-bundle.r4).

Pinned by the audit:

* symbol mode actually composes — calls symbol search, references,
  impact, memory, and (when a file is resolved) diagnostics.
* missing symbol returns ``symbol_resolution="not_found"`` and looks
  visibly different from a found symbol.
* error paths carry handler_version + contract_version.
* every successful response ships ``next_actions``.
* low budget surfaces ``truncated`` + ``truncation_reasons`` +
  ``budget_utilization``.
* file mode either runs diagnostics or reports diagnostics_status with a
  reason — never silently emits an empty list.
* task mode does lexical boost: a task containing ``_detect_mode``
  returns the actual symbol's file via lexical hit, not just semantic.
* contract_version is exactly ``context.v2``.

The tests stub the HTTP backend so they don't need a live FastAPI app.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _extract_lexical_terms,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim + scripted backend
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
        # Route by exact endpoint, then by trailing path segment.
        # Allow a per-test catch-all by mapping prefixes via callable.
        if endpoint in routes:
            payload = routes[endpoint]
        else:
            key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
            payload = routes.get(key)
            if payload is None:
                # Try prefix-match keys (e.g. "/lsp/diagnostics/").
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


# ---------------------------------------------------------------------------
# Common fixtures: scripted routes for the _detect_mode shape.
# ---------------------------------------------------------------------------


_DETECT_MODE_FILE = "omnicode_adapters/mcp_server/high_level_tools.py"


def _routes_for_detect_mode_found() -> Dict[str, Any]:
    """Routes that mimic _detect_mode resolving in production."""
    return {
        # /search/symbols (used by _run_symbol)
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
        # /search/text (used by _run_references for the grep step)
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
                },
                {
                    "file_path": _DETECT_MODE_FILE,
                    "line_number": 2306,
                    "line_content": "resolved_mode = _detect_mode(query)",
                    "context_before": [],
                    "context_after": [],
                    "match_type": "text",
                    "relevance_score": 0.6,
                    "why_matched": ["text:line_match"],
                },
            ],
            "total_results": 2,
        },
        # LSP unavailable so references falls back to AST + grep.
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/lsp/references": {"error": "lsp not running"},
        # /graph/* trio
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
        # diagnostics: clean file
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/": {"diagnostics": []},
        # memory advisory
        "/memory/advisory": {
            "advisory": "📝 Past lesson: be careful when changing routing rules.",
            "referenced_memories": ["m1", "m2"],
        },
        # git status
        "/git/status": {
            "status": {
                "modified_files": [_DETECT_MODE_FILE],
                "untracked_files": [],
                "staged_files": [],
            }
        },
    }


def _routes_for_missing_symbol() -> Dict[str, Any]:
    """Routes where the symbol simply doesn't exist anywhere."""
    return {
        "/search/symbols": {"results": [], "total_results": 0},
        "/search/text": {"results": [], "total_results": 0},
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        "/lsp/references": {"error": "lsp not running"},
        "/graph/risk": {"risk": "low", "reasons": ["No test coverage found"]},
        "/graph/impact": {
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 0,
            "files_involved": [],
        },
        "/graph/related-tests": {
            "test_files": [], "suggested_commands": [],
        },
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/": {"diagnostics": []},
        "/memory/advisory": {"advisory": "", "referenced_memories": []},
        "/git/status": {"status": {"modified_files": [], "untracked_files": [], "staged_files": []}},
    }


# ---------------------------------------------------------------------------
# 1. symbol mode resolves _detect_mode
# ---------------------------------------------------------------------------


def test_context_symbol_mode_resolves_detect_mode() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(
        tools["omni_context"](symbol="_detect_mode", token_budget=4000, format="json")
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"
    assert payload["confidence"] == "high"
    # primary_symbols must include the definition row.
    psy = payload["context"]["primary_symbols"]
    assert psy, "primary_symbols must be non-empty for a found symbol"
    assert any(p.get("name") == "_detect_mode" for p in psy)


# ---------------------------------------------------------------------------
# 2. symbol mode includes references
# ---------------------------------------------------------------------------


def test_context_symbol_mode_includes_references() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(
        tools["omni_context"](symbol="_detect_mode", token_budget=4000, format="json")
    )
    payload = json.loads(raw)
    refs = payload["context"]["references"]
    assert refs, "symbol mode must surface references; got empty list"
    # Each ref carries source/confidence honest tagging.
    for r in refs:
        assert r.get("source"), r
        assert r.get("confidence"), r


# ---------------------------------------------------------------------------
# 3. symbol mode includes impact + suggested tests
# ---------------------------------------------------------------------------


def test_context_symbol_mode_includes_impact_suggested_tests() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(
        tools["omni_context"](symbol="_detect_mode", token_budget=4000, format="json")
    )
    payload = json.loads(raw)
    impact = payload["context"].get("impact") or {}
    assert impact, "impact block must be present for found symbol"
    assert impact.get("risk") == "medium"
    assert "tests/unit/test_detect_mode.py" in (impact.get("suggested_tests") or [])
    cmds = impact.get("suggested_commands") or []
    assert any(
        c.startswith("pytest tests/unit/test_detect_mode.py")
        for c in cmds
    )


# ---------------------------------------------------------------------------
# 4. symbol mode includes memory or memory_status
# ---------------------------------------------------------------------------


def test_context_symbol_mode_includes_memory_or_memory_status() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(
        tools["omni_context"](symbol="_detect_mode", token_budget=4000, format="json")
    )
    payload = json.loads(raw)
    mems = payload["context"].get("memories") or []
    mem_status = payload.get("memory_status") or {}
    # Either we surfaced an advisory row, or memory_status reports it ran.
    assert mems or mem_status.get("ran") is True, payload


# ---------------------------------------------------------------------------
# 5. missing symbol → symbol_resolution=not_found
# ---------------------------------------------------------------------------


def test_context_missing_symbol_sets_symbol_resolution_not_found() -> None:
    tools = _build_tools(_routes_for_missing_symbol())
    raw = _run(
        tools["omni_context"](
            symbol="DefinitelyNotExistSymbol123",
            token_budget=3000, format="json",
        )
    )
    payload = json.loads(raw)
    assert payload["symbol_resolution"] == "not_found"
    assert payload["confidence"] == "low"
    assert payload["context"]["primary_symbols"] == []
    assert payload["context"]["references"] == []
    assert (payload["context"].get("impact") or {}) == {} or not payload["context"].get("impact")
    note = (payload.get("note") or "").lower()
    assert "not found" in note
    actions = " ".join(payload.get("next_actions") or []).lower()
    assert "omni_search" in actions and "symbol" in actions


# ---------------------------------------------------------------------------
# 6. existing vs missing symbol must look visibly different
# ---------------------------------------------------------------------------


def test_context_missing_symbol_not_same_as_existing_symbol() -> None:
    found_tools = _build_tools(_routes_for_detect_mode_found())
    miss_tools = _build_tools(_routes_for_missing_symbol())
    found = json.loads(_run(found_tools["omni_context"](
        symbol="_detect_mode", token_budget=4000, format="json",
    )))
    miss = json.loads(_run(miss_tools["omni_context"](
        symbol="DefinitelyNotExistSymbol123", token_budget=4000, format="json",
    )))

    assert found["symbol_resolution"] != miss["symbol_resolution"]
    assert found["confidence"] != miss["confidence"]
    # The two responses must NOT share the same shape signature: the
    # "found" one has primary_symbols + references; "missing" doesn't.
    assert bool(found["context"]["primary_symbols"]) != bool(miss["context"]["primary_symbols"])
    assert bool(found["context"]["references"]) != bool(miss["context"]["references"])


# ---------------------------------------------------------------------------
# 7. error paths include contract_version
# ---------------------------------------------------------------------------


def test_context_error_paths_include_contract_version() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_context"](format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_context"]
    # Also: the error envelope should still tell the caller what to do.
    assert payload.get("next_actions"), payload


def test_context_empty_strings_are_treated_as_missing() -> None:
    tools = _build_tools({})
    raw = _run(
        tools["omni_context"](task="", file="", symbol="", format="json")
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_context"]


# ---------------------------------------------------------------------------
# 8. success paths include next_actions
# ---------------------------------------------------------------------------


def test_context_success_paths_include_next_actions() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    # symbol mode
    sym_payload = json.loads(_run(
        tools["omni_context"](symbol="_detect_mode", token_budget=4000, format="json")
    ))
    assert sym_payload["next_actions"]
    # task mode
    task_payload = json.loads(_run(
        tools["omni_context"](
            task="modify _detect_mode routing for hybrid mode",
            token_budget=4000, format="json",
        )
    ))
    assert task_payload["next_actions"]
    # file mode
    file_payload = json.loads(_run(
        tools["omni_context"](file=_DETECT_MODE_FILE, token_budget=4000, format="json")
    ))
    assert file_payload["next_actions"]


# ---------------------------------------------------------------------------
# 9. low budget surfaces truncated / truncation_reasons / budget_utilization
# ---------------------------------------------------------------------------


def test_context_low_budget_sets_truncated_or_budget_utilization() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode", token_budget=200, format="json",
    ))
    payload = json.loads(raw)
    # Either explicit truncation OR budget_utilization >= 0.8 must hold.
    assert payload["truncated"] is True, payload
    assert payload["truncation_reasons"], payload
    assert "budget_utilization" in payload
    # Each truncation reason is a "skipped:<section>" or "budget_utilization:..."
    # or "*_capped:N" string.
    for r in payload["truncation_reasons"]:
        assert any(r.startswith(prefix) for prefix in (
            "skipped:", "budget_utilization:", "references_capped:",
            "related_files_capped:",
        )), r


# ---------------------------------------------------------------------------
# 10. file mode runs diagnostics or reports diagnostics_status
# ---------------------------------------------------------------------------


def test_context_file_mode_runs_diagnostics_or_reports_status() -> None:
    routes = _routes_for_detect_mode_found()
    # Inject a real diagnostic so the diagnostics list is non-empty.
    routes["/guard/check"] = {
        "issues": [
            {
                "tool": "ruff", "severity": "error", "line": 42,
                "column": 0, "code": "F821", "message": "Undefined name `x`",
            }
        ]
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        file=_DETECT_MODE_FILE, token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    diag_status = payload.get("diagnostics_status") or {}
    assert diag_status.get("ran") is True, payload
    assert diag_status.get("source"), payload
    # And the diagnostic itself made it into the context.
    diags = payload["context"]["diagnostics"]
    assert any(d.get("severity") == "error" for d in diags), payload


def test_context_file_mode_diagnostics_status_explains_silent_zero() -> None:
    """When backend has 0 issues, diagnostics_status.ran should still be
    true so the AI knows we ran it; diagnostics list may be empty."""
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(tools["omni_context"](
        file=_DETECT_MODE_FILE, token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    diag_status = payload.get("diagnostics_status") or {}
    assert diag_status.get("ran") is True, payload


# ---------------------------------------------------------------------------
# 11. task mode lexical boost finds _detect_mode
# ---------------------------------------------------------------------------


def test_context_task_mode_lexical_boost_finds_detect_mode() -> None:
    """A task referencing the symbol by name should trigger lexical
    boost via _run_symbol so the actual file shows up — even if the
    semantic backend is silent on the topic."""
    routes = {
        # symbol search WILL fire for the lexical token "_detect_mode".
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
        # Semantic returns NOTHING relevant — only fuzz, simulating the
        # production audit observation.
        "/search": {
            "results": [
                {
                    "file_path": "templates/static/js/core/logger.js",
                    "symbol_name": "getHistory",
                    "relevance_score": 0.43,
                    "why_matched": ["semantic"],
                }
            ],
            "total_results": 1,
        },
        "/git/status": {"status": {}},
        "/memory/search": {"results": []},
        "/guard/check": {"issues": []},
        "/lsp/diagnostics/": {"diagnostics": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_context"](
        task="fix how _detect_mode routes between text and semantic",
        token_budget=4000, format="json",
    ))
    payload = json.loads(raw)

    related = payload["context"]["related_files"]
    files = [r.get("file") for r in related]
    assert _DETECT_MODE_FILE in files, (
        f"lexical boost must surface {_DETECT_MODE_FILE} for a task "
        f"mentioning '_detect_mode'; got {files}"
    )
    # And at least one row is tagged as a lexical hit, not pure semantic.
    assert any(
        (r.get("reason") or "").startswith("task→lexical:")
        for r in related
    ), related
    # Confidence escalates to 'high' when lexical hit lands.
    assert payload["confidence"] == "high"


def test_extract_lexical_terms_finds_snake_case() -> None:
    terms = _extract_lexical_terms(
        "fix how _detect_mode and ProviderRegistry.test_provider work"
    )
    assert "_detect_mode" in terms
    assert any(t.startswith("ProviderRegistry") or "test_provider" in t for t in terms)


# ---------------------------------------------------------------------------
# 12. contract_version is exactly context.v2
# ---------------------------------------------------------------------------


def test_context_contract_version_is_context_v2() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode", token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    assert payload["contract_version"] == "context.v2"
    assert _CONTRACT_VERSIONS["omni_context"] == "context.v2"


def test_context_handler_version_matches_module_constant() -> None:
    tools = _build_tools(_routes_for_detect_mode_found())
    raw = _run(tools["omni_context"](
        symbol="_detect_mode", token_budget=4000, format="json",
    ))
    payload = json.loads(raw)
    assert payload["handler_version"] == _HANDLER_VERSION
