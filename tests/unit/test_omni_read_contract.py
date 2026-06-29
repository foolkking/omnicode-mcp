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

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _READ_VALID_MODES,
    _build_read_payload,
    _emit_read_error,
    _guess_language_from_path,
    _next_actions_for_mode,
    _sanitize_error_text,
)
from tests.unit.mcp_harness import (
    build_tools_with_route_keys as _build_tools,
)
from tests.unit.mcp_harness import (
    run as _run,
)

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


def test_omni_diagnostics_hybrid_uses_local_workspace_first(
    tmp_path, monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "tests" / "tmp_cloudsim_routing.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'def cloudsim_route():\n    return "local-v2"\n',
        encoding="utf-8",
    )

    class _Guard:
        async def check(self, file_path: str):
            assert str(target) == file_path
            return SimpleNamespace(
                issues=[],
                tools_run=["ruff"],
                tools_skipped=["mypy", "bandit"],
            )

    import omnicode.guard.analyzer as guard_analyzer

    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(guard_analyzer, "ProactiveGuard", _Guard)

    tools = _build_tools({})
    raw = _run(tools["omni_diagnostics"](
        file="tests/tmp_cloudsim_routing.py",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["source"] == "local_guard"
    assert payload["local_first"] is True
    assert payload["local_authority"] is True
    assert payload["tools_run"] == ["ruff"]
    assert "lsp:local_mcp_lsp_unavailable" in payload["tools_skipped"]


def test_omni_diagnostics_scala_unsupported_preflights_without_backend(
    tmp_path, monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "core" / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App { def broken( = 1 }\n", encoding="utf-8")

    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))

    tools = _build_tools({
        "/diagnostics": {
            "ok": True,
            "diagnostics": [{"message": "backend should not run"}],
            "counts": {"total": 1},
        }
    })
    raw = _run(tools["omni_diagnostics"](
        file="core/src/main/scala/App.scala",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["diagnostics_status"] == "unsupported"
    assert payload["language"] == "scala"
    assert payload["tools_run"] == []
    assert payload["tools_skipped"] == ["metals_unavailable"]
    assert payload["capability_preflight"]["execution_policy"]["mode"] == "block"
    assert "/diagnostics" not in tools["__captured__"]


def test_omni_diagnostics_java_tree_sitter_syntax_without_backend(
    tmp_path, monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "src" / "main" / "java" / "App.java"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class App { void broken() { int value = ; } }\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_JDTLS_DISABLED", "true")

    tools = _build_tools({
        "/guard/check": {
            "issues": [{"message": "backend should not run"}],
        },
        "/lsp/diagnostics/src/main/java/App.java": {
            "diagnostics": [{"message": "backend should not run"}],
        },
    })
    raw = _run(tools["omni_diagnostics"](
        file="src/main/java/App.java",
        severity="all",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["language"] == "java"
    assert payload["diagnostics_status"] == "target_errors"
    assert payload["source"] == "tree_sitter_java"
    assert payload["tools_run"] == ["tree_sitter_java"]
    assert "java_semantic_diagnostics_not_performed" in payload["tools_skipped"]
    assert payload["counts"]["error"] >= 1
    assert payload["diagnostics"][0]["rule"] == "java-syntax"
    assert "/guard/check" not in tools["__captured__"]
    assert "/lsp/diagnostics/src/main/java/App.java" not in tools["__captured__"]


def test_omni_diagnostics_java_javac_environment_incomplete(
    tmp_path, monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "src" / "main" / "java" / "App.java"
    target.parent.mkdir(parents=True)
    target.write_text(
        "import missing.Dependency;\nclass App { Dependency dep; }\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_JDTLS_DISABLED", "true")

    tools = _build_tools({
        "/guard/check": {
            "issues": [{"message": "backend should not run"}],
        },
        "/lsp/diagnostics/src/main/java/App.java": {
            "diagnostics": [{"message": "backend should not run"}],
        },
    })
    raw = _run(tools["omni_diagnostics"](
        file="src/main/java/App.java",
        severity="all",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["language"] == "java"
    assert payload["diagnostics_status"] == "environment_incomplete"
    assert payload["source"] == "tree_sitter_java+javac"
    assert payload["tools_run"] == ["tree_sitter_java", "javac"]
    assert "java_environment_incomplete" in payload["warnings"]
    assert payload["counts"]["error"] >= 1
    assert "/guard/check" not in tools["__captured__"]
    assert "/lsp/diagnostics/src/main/java/App.java" not in tools["__captured__"]


def test_omni_read_symbol_hybrid_uses_local_ast(
    tmp_path, monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "django" / "core" / "handlers" / "base.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class BaseHandler:\n"
        "    def load_middleware(self):\n"
        "        return 'local'\n\n"
        "class Other:\n"
        "    pass\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))

    tools = _build_tools({})
    raw = _run(tools["omni_read"](
        file="django/core/handlers/base.py",
        mode="symbol",
        symbol="BaseHandler",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["mode"] == "symbol"
    assert payload["symbol"] == "BaseHandler"
    assert payload["source"] == "local_ast"
    assert payload["local_authority"] is True
    assert "class BaseHandler" in payload["content"]
    assert "class Other" not in payload["content"]


@pytest.mark.parametrize(
    "mode,kwargs",
    [
        ("full", {}),
        ("range", {"start_line": 1, "end_line": 2}),
        ("outline", {}),
    ],
)
def test_omni_read_hybrid_cloud_down_stays_local(
    tmp_path,
    monkeypatch,
    mode: str,
    kwargs: Dict[str, Any],
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "tests" / "tmp_cloudsim_cloud_down.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def local_only():\n"
        "    return 'cloud-down-read'\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_REMOTE", "http://127.0.0.1:6799")

    tools = _build_tools({
        "/sync/status": {
            "error": "Cannot connect to FastAPI server - server may be down",
            "error_type": "ConnectionError",
        },
        "/read": {
            "success": False,
            "error": "cloud read should not be called",
        },
    })
    raw = _run(tools["omni_read"](
        file="tests/tmp_cloudsim_cloud_down.py",
        mode=mode,
        format="json",
        **kwargs,
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["source"] in {"local_file", "local_ast"}
    assert payload["local_first"] is True
    assert payload["local_authority"] is True
    assert "local_only" in json.dumps(payload, ensure_ascii=False)
    assert "/read" not in tools["__captured__"]


def test_omni_read_hybrid_missing_local_file_does_not_fallback_to_cloud(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    (workspace / "tests").mkdir(parents=True)

    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_REMOTE", "http://127.0.0.1:6799")

    tools = _build_tools({
        "/sync/status": {
            "error": "Cannot connect to FastAPI server - server may be down",
            "error_type": "ConnectionError",
        },
        "/read": {
            "success": False,
            "error": "cloud read should not be called",
        },
    })
    raw = _run(tools["omni_read"](
        file="tests/tmp_cloudsim_missing_after_rollback.py",
        mode="full",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "File not found" in payload["error"]
    assert "/read" not in tools["__captured__"]


def test_omni_context_hybrid_diagnostics_uses_local_workspace(
    tmp_path, monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    target = workspace / "tests" / "tmp_cloudsim_routing.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'def cloudsim_route():\n    return "local-v2"\n',
        encoding="utf-8",
    )

    class _Guard:
        async def check(self, file_path: str):
            assert str(target) == file_path
            return SimpleNamespace(
                issues=[],
                tools_run=["ruff"],
                tools_skipped=["mypy", "bandit"],
            )

    import omnicode.guard.analyzer as guard_analyzer
    from omnicode_core.workspace.local import LocalWorkspace
    from omnicode_core.workspace.manifest import LocalManifest

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setattr(guard_analyzer, "ProactiveGuard", _Guard)

    local_ws = LocalWorkspace(root=workspace, workspace_id="repo-a")
    manifest = LocalManifest.load(workspace=local_ws)
    manifest.mark_changed(target)
    manifest.data["last_accepted_revision"] = manifest.local_revision
    manifest.data["last_indexed_revision"] = manifest.local_revision
    manifest.data["pending"] = []
    manifest.save()

    tools = _build_tools({
        "/sync/status": {
            "ok": True,
            "accepted_revision": manifest.local_revision,
            "indexed_revision": manifest.local_revision,
        },
    })
    raw = _run(tools["omni_context"](
        file="tests/tmp_cloudsim_routing.py",
        format="json",
        token_budget=2000,
    ))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["context"]["primary_symbols"][0]["name"] == "cloudsim_route"
    status = payload["diagnostics_status"]
    assert status["ran"] is True
    assert status["source"] == "local_guard"
    assert status["local_first"] is True
    assert status["local_authority"] is True
    assert status["tools_run"] == ["ruff"]
    assert "lsp:local_mcp_lsp_unavailable" in status["tools_skipped"]


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


def test_diagnostics_separates_project_findings_from_target() -> None:
    tools = _build_tools(_diag_routes([
        {
            "tool": "ruff",
            "severity": "error",
            "line": 4,
            "code": "F821",
            "message": "Undefined name `missing`",
            "file_path": "x.py",
        },
        {
            "tool": "mypy",
            "severity": "error",
            "line": 9,
            "code": "attr-defined",
            "message": "Unrelated project error",
            "file_path": "package/other.py",
        },
    ]))

    payload = json.loads(_run(tools["omni_diagnostics"](
        file="x.py", format="json",
    )))

    assert payload["diagnostics_status"] == "target_errors"
    assert payload["counts"] == {
        "error": 1, "warning": 0, "info": 0, "total": 1,
    }
    assert payload["diagnostics"][0]["file"] == "x.py"
    assert payload["project_diagnostics_count"] == 1
    assert payload["project_counts"]["error"] == 1
    assert payload["project_diagnostics_sample"][0]["file"] == "package/other.py"


def test_diagnostics_marks_project_mypy_environment_incomplete() -> None:
    tools = _build_tools(_diag_routes([
        {
            "tool": "mypy",
            "severity": "error",
            "line": 1,
            "code": "import-not-found",
            "message": "Cannot find implementation or library stub",
            "file_path": "package/other.py",
        },
    ]))

    payload = json.loads(_run(tools["omni_diagnostics"](
        file="x.py", format="json",
    )))

    assert payload["diagnostics"] == []
    assert payload["counts"]["total"] == 0
    assert payload["diagnostics_status"] == "environment_incomplete"
    assert payload["project_diagnostics_count"] == 1
    assert payload["environment"]["reason"] == "mypy_environment_incomplete"
    assert "diagnostics_environment_incomplete" in payload["warnings"]


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
    assert "C:\\" not in payload["error"]
    assert "<absolute-path>" in payload["error"]
    _assert_path_guard_next_actions_are_safe(payload)


def test_error_text_redacts_known_absolute_paths() -> None:
    error = (
        "backend failed at C:\\omnicode-sim\\state-cloud\\cloud-sync\\repo-a "
        "while reading C:/Users/86182/project/tests/tmp_escape.py"
    )

    redacted = _sanitize_error_text(error)

    assert "C:\\" not in redacted
    assert "C:/" not in redacted
    assert "<absolute-path>" in redacted


def test_error_text_keeps_relative_tmp_paths_readable() -> None:
    text = (
        "File not found: tests/tmp_cloudsim_file.py; "
        "rejected ../tmp_cloudsim_escape.py"
    )

    redacted = _sanitize_error_text(text)

    assert "tests/tmp_cloudsim_file.py" in redacted
    assert "../tmp_cloudsim_escape.py" in redacted
    assert "<absolute-path>" not in redacted


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
