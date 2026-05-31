"""Contract tests for the omni_read schema fixes.

Pinned by the audit:

1. test_read_diagnostics_schema_matches_omni_diagnostics
   omni_read(mode="diagnostics") returns the same envelope shape as
   omni_diagnostics — counts / total_count / severity_filter / sources /
   tools_run / tools_skipped / truncated all present, and the legacy
   diagnostic_count is kept as an alias.

2. test_read_language_present_in_all_modes
   ``language`` is non-empty for every mode (outline / symbols / symbol /
   imports / diagnostics / range / full).

3. test_read_next_actions_present_for_all_modes
   ``next_actions`` is a non-empty list for every mode.

4. test_read_full_respects_max_tokens
   mode=full with a tight budget still truncates and ships
   truncation_hint + a useful next_actions list.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _build_read_payload,
    _emit_read_error,
    _guess_language_from_path,
    _next_actions_for_mode,
    register_high_level_tools,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_tools(
    routes: Dict[str, Any],
) -> Dict[str, Callable[..., Any]]:
    """Wire the MCP tools up with a scripted ``make_request``.

    ``routes`` keys can be the full endpoint or the trailing path segment;
    values can be a dict (returned wrapped in ``{"result": …}``) or a
    callable invoked with (method, endpoint, kwargs).
    """

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
# 1. omni_read(mode="diagnostics") schema must match omni_diagnostics.
# ---------------------------------------------------------------------------


def _diag_routes(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a routes dict where /guard/check returns the given issues."""
    return {
        "/guard/check": {"issues": issues},
        # LSP diagnostics endpoint pattern is /lsp/diagnostics/<file>; the
        # route table matches on trailing path segment, which after the
        # filename can be anything — return an empty diagnostic set so we
        # cleanly hit the "lsp ran with zero hits" branch.
        "/lsp/diagnostics/x.py": {"diagnostics": []},
    }


def test_read_diagnostics_schema_matches_omni_diagnostics() -> None:
    issues = [
        {
            "tool": "ruff",
            "severity": "error",
            "line": 12,
            "column": 4,
            "code": "E501",
            "message": "line too long",
        },
        {
            "tool": "mypy",
            "severity": "warning",
            "line": 30,
            "column": 0,
            "code": "type-arg",
            "message": "missing type argument",
        },
    ]

    tools = _build_tools(_diag_routes(issues))

    diag_raw = _run(tools["omni_diagnostics"](file="x.py", format="json"))
    read_raw = _run(tools["omni_read"](file="x.py", mode="diagnostics", format="json"))
    diag = json.loads(diag_raw)
    read = json.loads(read_raw)

    # Both must succeed.
    assert diag["ok"] is True
    assert read["ok"] is True

    # The canonical fields must agree on shape AND value where applicable.
    canonical = (
        "diagnostics",
        "counts",
        "total_count",
        "severity_filter",
        "sources",
        "tools_run",
        "tools_skipped",
        "truncated",
    )
    for field in canonical:
        assert field in diag, f"omni_diagnostics missing field {field}"
        assert field in read, f"omni_read[diagnostics] missing field {field}"

    # Diagnostics list and counts must match exactly.
    assert read["diagnostics"] == diag["diagnostics"]
    assert read["counts"] == diag["counts"]
    assert read["total_count"] == diag["total_count"]
    assert read["counts"]["total"] == read["total_count"]

    # The legacy diagnostic_count alias is still honoured for back-compat.
    assert read["diagnostic_count"] == read["total_count"]

    # omni_read keeps its own envelope keys on top.
    assert read["mode"] == "diagnostics"
    assert read["file"] == "x.py"
    assert "next_actions" in read


def test_read_diagnostics_empty_returns_canonical_envelope() -> None:
    """Zero-issue runs still need the full canonical envelope."""
    tools = _build_tools(_diag_routes([]))
    raw = _run(tools["omni_read"](file="x.py", mode="diagnostics", format="json"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["diagnostics"] == []
    assert payload["total_count"] == 0
    assert payload["counts"] == {"error": 0, "warning": 0, "info": 0, "total": 0}
    assert payload["severity_filter"] == "all"
    # Sources is the requested set; should include "guard" and/or "lsp".
    assert set(payload["sources"]) <= {"guard", "lsp"}
    assert payload["truncated"] is False


# ---------------------------------------------------------------------------
# 2. language must be non-empty for every mode.
# ---------------------------------------------------------------------------


def _outline_payload() -> Dict[str, Any]:
    return {
        "language": "python",
        "total_lines": 100,
        "symbols": [
            {"name": "foo", "kind": "function", "lines": [10, 20]},
        ],
        "symbol_count": 1,
    }


def _imports_payload() -> Dict[str, Any]:
    return {
        "language": "python",
        "total_lines": 100,
        "imports": [{"line": 1, "line_end": 1, "text": "import os"}],
        "import_count": 1,
        "ast_used": True,
        "content": "import os",
    }


def _full_payload() -> Dict[str, Any]:
    # Backend deliberately leaves language empty for full/range/symbol —
    # this is the bug the contract test pins down.
    return {
        "language": "",
        "total_lines": 5,
        "content": "line1\nline2\nline3\nline4\nline5",
    }


def _range_payload() -> Dict[str, Any]:
    return {
        "language": "",
        "total_lines": 5,
        "start_line": 1,
        "end_line": 3,
        "content": "1 | line1\n2 | line2\n3 | line3",
    }


def _symbol_payload() -> Dict[str, Any]:
    return {
        "language": "",
        "total_lines": 100,
        "start_line": 10,
        "end_line": 20,
        "symbol_name": "foo",
        "content": "10 | def foo():\n11 |     pass",
    }


@pytest.mark.parametrize(
    "mode,builder_kwargs,backend",
    [
        ("outline", {}, _outline_payload()),
        ("symbols", {}, _outline_payload()),
        ("imports", {}, _imports_payload()),
        ("full", {}, _full_payload()),
        ("range", {"start_line": 1, "end_line": 3}, _range_payload()),
        ("symbol", {"symbol": "foo"}, _symbol_payload()),
    ],
)
def test_read_language_present_in_all_modes(mode, builder_kwargs, backend):
    payload = _build_read_payload(
        file="x.py",
        requested_mode=mode,
        data=backend,
        start_line=builder_kwargs.get("start_line"),
        end_line=builder_kwargs.get("end_line"),
        symbol=builder_kwargs.get("symbol"),
        query=None,
        max_tokens=8000,
    )
    assert payload["language"] == "python", (mode, payload["language"])


def test_read_language_present_for_diagnostics() -> None:
    """diagnostics is wired through the live tool because it bypasses /read."""
    tools = _build_tools(_diag_routes([]))
    raw = _run(tools["omni_read"](file="x.py", mode="diagnostics", format="json"))
    payload = json.loads(raw)
    assert payload["language"] == "python"


def test_guess_language_from_path_table() -> None:
    cases = [
        ("foo/bar/baz.py", "python"),
        ("a.ts", "typescript"),
        ("b.tsx", "typescript"),
        ("c.go", "go"),
        ("d.rs", "rust"),
        ("e.md", "markdown"),
        ("README", ""),  # no extension → empty (caller may keep blank)
        ("noext.unknown", ""),
    ]
    for path, want in cases:
        assert _guess_language_from_path(path) == want, path


# ---------------------------------------------------------------------------
# 3. next_actions must be present for every mode.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,builder_kwargs,backend",
    [
        ("outline", {}, _outline_payload()),
        ("symbols", {}, _outline_payload()),
        ("imports", {}, _imports_payload()),
        ("full", {}, _full_payload()),
        ("range", {"start_line": 1, "end_line": 3}, _range_payload()),
        ("symbol", {"symbol": "foo"}, _symbol_payload()),
    ],
)
def test_read_next_actions_present_for_all_modes(mode, builder_kwargs, backend):
    payload = _build_read_payload(
        file="x.py",
        requested_mode=mode,
        data=backend,
        start_line=builder_kwargs.get("start_line"),
        end_line=builder_kwargs.get("end_line"),
        symbol=builder_kwargs.get("symbol"),
        query=None,
        max_tokens=8000,
    )
    actions = payload.get("next_actions")
    assert actions, f"{mode} has no next_actions"
    assert isinstance(actions, list)
    assert all(isinstance(a, str) and a for a in actions)


def test_read_next_actions_present_for_diagnostics() -> None:
    tools = _build_tools(_diag_routes([]))
    raw = _run(tools["omni_read"](file="x.py", mode="diagnostics", format="json"))
    payload = json.loads(raw)
    assert payload.get("next_actions"), payload


def test_symbol_mode_next_actions_mention_followups() -> None:
    """Symbol mode should specifically push the user toward
    references / impact / diagnostics / patch preview."""
    actions = _next_actions_for_mode(
        mode="symbol", symbol="foo", file="x.py", truncated=False,
    )
    joined = " ".join(actions).lower()
    assert "omni_search" in joined and "references" in joined
    assert "omni_impact" in joined
    assert "omni_diagnostics" in joined
    assert "omni_patch" in joined and "preview" in joined


def test_range_mode_next_actions_point_to_outline_or_symbol() -> None:
    actions = _next_actions_for_mode(
        mode="range", symbol=None, file="x.py", truncated=False,
    )
    joined = " ".join(actions).lower()
    assert "outline" in joined or "symbol" in joined


def test_diagnostics_mode_next_actions_recommend_canonical_tool() -> None:
    actions = _next_actions_for_mode(
        mode="diagnostics", symbol=None, file="x.py", truncated=False,
    )
    joined = " ".join(actions).lower()
    assert "omni_diagnostics" in joined
    # And a way to read the offending lines.
    assert "range" in joined


# ---------------------------------------------------------------------------
# 4. mode=full + tight max_tokens must still truncate cleanly.
# ---------------------------------------------------------------------------


def test_read_full_respects_max_tokens() -> None:
    big_content = "\n".join(f"line {i:04d}: {'x' * 80}" for i in range(2000))
    backend = {
        "language": "",
        "total_lines": 2000,
        "content": big_content,
    }
    payload = _build_read_payload(
        file="x.py",
        requested_mode="full",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query=None,
        max_tokens=500,
    )
    assert payload["truncated"] is True
    assert payload["token_estimate"] <= 600  # ~500 with a small safety margin
    assert payload["lines_returned"] < 2000
    # truncation_hint must be present and actionable.
    assert "truncation_hint" in payload
    hint = payload["truncation_hint"].lower()
    assert "outline" in hint or "range" in hint
    # next_actions should also tell the AI where to go.
    actions = payload.get("next_actions") or []
    joined = " ".join(actions).lower()
    assert "range" in joined or "outline" in joined
    # Language fallback should still kick in even for full mode.
    assert payload["language"] == "python"


def test_read_full_under_budget_is_not_truncated() -> None:
    backend = {
        "language": "",
        "total_lines": 5,
        "content": "a\nb\nc\nd\ne",
    }
    payload = _build_read_payload(
        file="x.py",
        requested_mode="full",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query=None,
        max_tokens=8000,
    )
    assert payload["truncated"] is False
    assert "truncation_hint" not in payload
    # Still has next_actions even when no truncation happened.
    assert payload["next_actions"]
    assert payload["language"] == "python"


# ---------------------------------------------------------------------------
# audit-bundle.r10 — read.error_next_actions + read.valid_modes_envelope
# ---------------------------------------------------------------------------
#
# r10 polishes the omni_read error envelope so AI editors get a recovery
# next_actions list on file-not-found, and a structured valid_modes array
# (mirroring omni_search) on illegal mode. Same goes for omni_diagnostics
# file-not-found.

from omnicode_adapters.mcp_server.high_level_tools import (  # noqa: E402
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _READ_VALID_MODES,
)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def test_read_file_not_found_includes_next_actions() -> None:
    # Backend echoes success=false → omni_read should surface a
    # structured error envelope with a recovery next_actions list.
    tools = _build_tools({
        "/read": {"success": False, "error": "File not found: not_exist_file_123.py"},
    })
    raw = _run(tools["omni_read"](
        file="not_exist_file_123.py", mode="outline", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()
    assert payload.get("next_actions"), "next_actions must be present on file-not-found"
    # Recovery hints should mention search and path-check at minimum.
    nxt = " ".join(payload["next_actions"]).lower()
    assert "omni_search" in nxt
    assert "path" in nxt or "retry" in nxt
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_read"]


def test_diagnostics_file_not_found_includes_next_actions() -> None:
    # _collect_diagnostics_payload short-circuits with ok=false when the
    # guard returns an issue whose message contains "file not found"
    # AND no other issues exist. omni_diagnostics must inject
    # next_actions before stamping.
    tools = _build_tools({
        "/guard/check": {"issues": [
            {"tool": "guard", "severity": "error",
             "message": "file not found: not_exist_file_123.py"},
        ]},
        # LSP returns nothing — leaves all_issues empty after the
        # file_missing filter strips the guard "file not found" issue.
    })
    raw = _run(tools["omni_diagnostics"](
        file="not_exist_file_123.py", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()
    assert payload.get("next_actions"), "next_actions must be present on file-not-found"
    nxt = " ".join(payload["next_actions"]).lower()
    assert "omni_read" in nxt or "omni_search" in nxt
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_diagnostics"]


def _assert_path_guard_next_actions_are_safe(payload: Dict[str, Any]) -> None:
    actions = payload.get("next_actions")
    assert actions, "path-guard errors must include next_actions"
    joined = " ".join(actions)
    lowered = joined.lower()
    assert "omni_status()" in joined
    assert "tmp_escape.py" in joined
    assert "../tmp_escape.py" not in joined
    assert "omni_read(file='../tmp_escape.py'" not in joined
    assert "query='../tmp_escape.py'" not in joined
    assert "do not retry" in lowered


def test_read_path_guard_error_does_not_repeat_unsafe_path() -> None:
    raw = _emit_read_error(
        file="../tmp_escape.py",
        mode="full",
        error=(
            "Access denied: Path escapes workspace: ../tmp_escape.py -> "
            "C:\\tmp_escape.py"
        ),
        fmt="json",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "access denied" in payload["error"].lower()
    _assert_path_guard_next_actions_are_safe(payload)


def test_diagnostics_path_guard_error_does_not_repeat_unsafe_path() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_diagnostics"](
        file="../tmp_escape.py", format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "path" in payload["error"].lower()
    _assert_path_guard_next_actions_are_safe(payload)
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_diagnostics"]


def test_read_illegal_mode_includes_valid_modes() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_read"](
        file="omnicode_adapters/mcp_server/high_level_tools.py",
        mode="illegal_mode",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["requested_mode"] == "illegal_mode"
    assert "illegal_mode" in payload["error"]
    assert "valid_modes" in payload
    assert list(payload["valid_modes"]) == list(_READ_VALID_MODES)
    # All canonical modes advertised.
    for m in ("outline", "symbols", "full", "range", "symbol",
              "imports", "diagnostics", "relevant_chunks", "tests"):
        assert m in payload["valid_modes"]
    assert payload.get("next_actions")


def test_read_illegal_mode_is_stamped() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_read"](
        file="x.py", mode="bogus", format="json",
    ))
    payload = json.loads(raw)
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_read"]
    # Contract must NOT drift — still read.diagnostics_aligned.v1.
    assert payload["contract_version"] == "read.diagnostics_aligned.v1"


def test_read_illegal_mode_text_format_stays_human_readable() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_read"](
        file="x.py", mode="bogus", format="text",
    ))
    assert "Unknown read mode" in raw
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_read_valid_mode_still_works_after_guard() -> None:
    """Regression guard: the new up-front mode check must not break a
    normal outline call."""
    tools = _build_tools({
        "/read": {
            "success": True,
            "content": "def f(): pass\n",
            "language": "python",
            "total_lines": 1,
            "symbols": [],
        },
    })
    raw = _run(tools["omni_read"](
        file="x.py", mode="outline", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["mode"] == "outline"
