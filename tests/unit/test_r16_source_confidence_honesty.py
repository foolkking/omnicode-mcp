"""Contract tests for audit-bundle.r16 — Round 6 source/confidence honesty.

Pinned by the Round 6 audit (3 P3 fixes):

* P3-A  omni_impact ``confidence`` is downgraded from ``high`` to
        ``medium`` when the graph is wide (>=50 files) OR when callees
        are dominated by Python builtins / method-style names. The
        ``confidence_caveats`` field explains why. Tight + clean graphs
        still report ``high``.
* P3-B  omni_search(mode='references') response surfaces structured
        LSP probe metadata: ``lsp_attempted`` / ``lsp_available`` /
        ``lsp_returned_refs`` / ``fallback_used`` / ``fallback_reason``
        so callers can tell "we tried LSP and it returned nothing"
        from "we never tried LSP".
* P3-C  omni_read responses now carry a per-mode ``source`` field
        (``ast`` / ``raw_file`` / ``vector`` / ``guard+lsp`` /
        ``graph``) and ``confidence`` (``high`` / ``medium`` /
        ``low``) for cross-tool uniformity.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _HANDLER_VERSION,
    _PYTHON_BUILTIN_CALLEE_NAMES,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# Reusable shim
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
# P3-A — omni_impact confidence honesty
# ===========================================================================


def test_omni_impact_tight_clean_graph_keeps_high_confidence() -> None:
    """A tight graph (<25 files) with no builtin callee noise must
    still report confidence=high — the fix only downgrades dishonest
    cases, not honest ones."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            "affected_symbols": ["my_helper_a", "my_helper_b"],
            "dependent_symbols": ["my_caller"],
            "files_count": 8,  # well under 25
            "files_involved": [f"f{i}.py" for i in range(8)],
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
    assert payload["confidence"] == "high"
    # No caveats on a clean graph.
    assert "confidence_caveats" not in payload or not payload["confidence_caveats"]


def test_omni_impact_wide_graph_downgrades_to_medium() -> None:
    """files_count >= 50 is too wide to honestly call ``high``."""
    routes = {
        "/graph/risk": {"risk": "medium", "reasons": ["Affects 80 files"]},
        "/graph/impact": {
            "affected_symbols": ["one_helper"],
            "dependent_symbols": ["one_caller"],
            "files_count": 80,
            "files_involved": [f"f{i}.py" for i in range(20)],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="my_func", format="json"))
    payload = json.loads(raw)
    assert payload["confidence"] == "medium"
    assert payload.get("confidence_caveats"), "wide graph must surface caveats"
    blob = " ".join(payload["confidence_caveats"]).lower()
    assert "transitive blast radius" in blob


def test_omni_impact_builtin_noise_downgrades_to_medium() -> None:
    """Even a small graph downgrades to medium when callees are
    dominated by Python builtins / method-style names."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            # 5 of 6 callees are builtin/method-style — that's noise.
            "affected_symbols": [
                "len", "lower", "split", "strip", "fullmatch",
                "actually_a_real_helper",
            ],
            "dependent_symbols": ["one_caller"],
            "files_count": 12,  # tight on file count
            "files_involved": [f"f{i}.py" for i in range(12)],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="my_func", format="json"))
    payload = json.loads(raw)
    assert payload["confidence"] == "medium"
    assert payload.get("confidence_caveats"), "builtin noise must surface caveats"
    blob = " ".join(payload["confidence_caveats"]).lower()
    assert "builtin" in blob or "method-style" in blob


def test_omni_impact_medium_band_for_25_to_50_files() -> None:
    """Mid-size graphs sit in the medium band even without noise."""
    routes = {
        "/graph/risk": {"risk": "low", "reasons": []},
        "/graph/impact": {
            "affected_symbols": ["a", "b", "c"],
            "dependent_symbols": ["d"],
            "files_count": 30,
            "files_involved": [f"f{i}.py" for i in range(20)],
        },
        "/graph/related-tests": {"test_files": [], "suggested_commands": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_impact"](symbol="my_func", format="json"))
    payload = json.loads(raw)
    assert payload["confidence"] == "medium"
    # 25-49 files is the "noticeable scope but not noisy" band — no
    # caveats to declare.
    assert "confidence_caveats" not in payload or not payload["confidence_caveats"]


def test_python_builtin_callee_set_includes_common_noise() -> None:
    """The constant must include the obvious offenders the audit cited."""
    must_have = {"len", "lower", "split", "strip", "fullmatch", "debug",
                 "append", "get", "items", "keys", "values"}
    missing = must_have - set(_PYTHON_BUILTIN_CALLEE_NAMES)
    assert not missing, f"missing builtin noise names: {missing}"


# ===========================================================================
# P3-B — omni_search(mode='references') LSP probe transparency
# ===========================================================================


def test_omni_search_references_surfaces_lsp_probe_metadata() -> None:
    """Even when LSP returns nothing, the response must surface
    lsp_attempted / lsp_available / fallback_used so callers can
    tell "tried + empty" from "never tried"."""
    routes = {
        # LSP errors out → fall back to AST + text_grep.
        "/lsp/workspace-symbols": {"error": "lsp not running"},
        # AST symbol exact match.
        "/search/symbols": {
            "results": [{
                "symbol_name": "my_func",
                "file_path": "src/x.py",
                "line_start": 10,
                "line_end": 20,
                "signature": "def my_func():",
            }],
        },
        # Text grep for callsites.
        "/search/text": {
            "results": [
                {"file_path": "src/y.py", "line_number": 5,
                 "line_content": "    my_func()"},
            ],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="my_func", mode="references", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["lsp_attempted"] is True
    assert payload["lsp_available"] is False
    assert payload["lsp_returned_refs"] is False
    assert payload["fallback_used"] == "ast+text_grep"
    assert payload.get("fallback_reason"), "must explain why we fell back"
    assert "lsp" in payload["fallback_reason"].lower()


def test_omni_search_references_lsp_success_marks_fallback_lsp() -> None:
    """When LSP returns refs, fallback_used='lsp' and confidence is high.

    Note: in the real call flow ``workspace/symbol`` first comes back
    empty (pyright lazy-loads the file), AST resolves the anchor, then
    ``textDocument/references`` is called. This test exercises that
    same code path."""
    routes = {
        # workspace-symbols returns empty → triggers AST-anchor + LSP
        # references call.
        "/lsp/workspace-symbols": {"symbols": []},
        # AST symbol exact match for the anchor.
        "/search/symbols": {
            "results": [{
                "symbol_name": "my_func",
                "file_path": "src/x.py",
                "line_start": 10,
                "line_end": 20,
                "signature": "def my_func():",
            }],
        },
        # textDocument/references returns LSP-grade refs.
        "/lsp/references": {
            "locations": [
                {"file_path": "src/x.py",
                 "range": {"start": {"line": 9, "character": 4}}},
                {"file_path": "src/y.py",
                 "range": {"start": {"line": 4, "character": 0}}},
            ],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="my_func", mode="references", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["lsp_attempted"] is True
    assert payload["lsp_available"] is True
    assert payload["lsp_returned_refs"] is True
    assert payload["fallback_used"] == "lsp"
    # All result rows should carry source=lsp + confidence=high.
    for row in payload["results"]:
        assert row["source"] == "lsp"
        assert row["confidence"] == "high"


def test_omni_search_non_references_modes_unaffected() -> None:
    """The new fields are scoped to references mode — auto/symbol
    must not leak them."""
    routes = {
        "/search/symbols": {
            "results": [{
                "symbol_name": "my_func", "file_path": "src/x.py",
                "line_start": 1, "line_end": 5, "kind": "function",
            }],
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_search"](
        query="my_func", mode="symbol", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    # Should NOT carry the references-only probe fields.
    assert "lsp_attempted" not in payload
    assert "fallback_used" not in payload


# ===========================================================================
# P3-C — omni_read source/confidence stamping per mode
# ===========================================================================


@pytest.mark.parametrize("mode,expected_source,fixture_payload", [
    (
        "outline",
        "ast",
        {
            "language": "python",
            "total_lines": 50,
            "symbols": [
                {"name": "f", "kind": "function", "lines": [1, 5],
                 "signature": "def f():"},
            ],
        },
    ),
    (
        "symbols",
        "ast",
        {
            "language": "python", "total_lines": 50,
            "symbols": [
                {"name": "f", "kind": "function", "lines": [1, 5]},
            ],
        },
    ),
    (
        "imports",
        "ast",
        {
            "language": "python", "total_lines": 50,
            "imports": ["import json", "from typing import Any"],
            "ast_used": True,
        },
    ),
    (
        "full",
        "raw_file",
        {
            "language": "python", "total_lines": 3,
            "content": "def f():\n    return 1\n",
        },
    ),
])
def test_omni_read_modes_carry_source_and_confidence(
    mode: str, expected_source: str, fixture_payload: Dict[str, Any],
) -> None:
    routes = {"/read": fixture_payload}
    tools = _build_tools(routes)
    raw = _run(tools["omni_read"](
        file="src/x.py", mode=mode, format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["source"] == expected_source, (
        f"mode={mode}: expected source={expected_source!r}, "
        f"got {payload.get('source')!r}"
    )
    assert payload["confidence"] in ("high", "medium", "low")


def test_omni_read_relevant_chunks_uses_vector_source() -> None:
    routes = {
        "/read": {
            "language": "python", "total_lines": 50,
            "chunks": [
                {"text": "...", "score": 0.9, "lines": [10, 20]},
            ],
            "result_count": 1,
            "query": "find x",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_read"](
        file="src/x.py", mode="relevant_chunks",
        query="find x", format="json",
    ))
    payload = json.loads(raw)
    assert payload["source"] == "vector"
    # Vector retrieval is approximate by construction.
    assert payload["confidence"] == "medium"


def test_omni_read_diagnostics_uses_guard_lsp_source() -> None:
    """omni_read[diagnostics] dispatches to ``_collect_diagnostics_payload``
    which calls /guard/check and /lsp/diagnostics. The contract is
    same-as omni_diagnostics for source/confidence parity."""
    routes = {
        "/guard/check": {
            "issues": [
                {"severity": "error", "line": 5, "message": "x",
                 "source": "ruff", "rule": "E001"},
            ],
            "tools_run": ["guard"],
        },
        "/lsp/diagnostics/src/x.py": {"diagnostics": []},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_read"](
        file="src/x.py", mode="diagnostics", format="json",
    ))
    payload = json.loads(raw)
    # source must be authoritative (linter rules).
    assert payload["source"] == "guard+lsp"
    # diagnostic linter output is rule-driven, so confidence is high
    # whenever any diagnostics came back.
    assert payload["confidence"] == "high"


def test_omni_read_empty_content_marks_medium_confidence() -> None:
    """When no content / symbols / diagnostics came back, confidence
    must NOT claim ``high`` even if the source is authoritative."""
    routes = {
        "/read": {
            "language": "python", "total_lines": 0,
            "content": "",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_read"](
        file="src/empty.py", mode="full", format="json",
    ))
    payload = json.loads(raw)
    assert payload["source"] == "raw_file"
    assert payload["confidence"] == "medium"


# ===========================================================================
# Feature flags + version stamp
# ===========================================================================


def test_handler_features_advertise_r16_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    for flag in (
        "impact.confidence_caveats",
        "search.references_lsp_probe",
        "read.source_confidence",
    ):
        assert flag in flags, f"missing r16 feature flag: {flag}"


def test_handler_version_is_r16() -> None:
    import re
    m = re.search(r"\.r(\d+)", hlt._HANDLER_VERSION)
    assert m is not None
    assert int(m.group(1)) >= 16, hlt._HANDLER_VERSION
