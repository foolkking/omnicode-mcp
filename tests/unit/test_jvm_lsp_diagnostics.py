from __future__ import annotations

import json
from pathlib import Path

from omnicode_adapters.mcp_server import high_level_tools as hlt
from tests.unit.mcp_harness import (
    build_tools_with_route_keys as _build_tools,
)
from tests.unit.mcp_harness import run as _run


class _FakeBridge:
    def __init__(
        self,
        result: dict,
        *,
        references: dict | None = None,
        hierarchy: dict | None = None,
    ) -> None:
        self.result = result
        self.references = references or {"locations": []}
        self.hierarchy = hierarchy or {
            "incoming": [],
            "outgoing": [],
            "prepared": False,
        }
        self.calls: list[dict] = []

    async def get_diagnostics(
        self,
        file: str,
        *,
        content: str | None = None,
        restore_after: bool = False,
    ) -> dict:
        self.calls.append({
            "file": file,
            "content": content,
            "restore_after": restore_after,
        })
        return dict(self.result)

    async def find_references(
        self,
        file: str,
        line: int,
        col: int,
        include_declaration: bool = True,
    ) -> dict:
        self.calls.append({
            "kind": "references",
            "file": file,
            "line": line,
            "col": col,
            "include_declaration": include_declaration,
        })
        return dict(self.references)

    async def call_hierarchy(
        self,
        file: str,
        line: int,
        col: int,
    ) -> dict:
        self.calls.append({
            "kind": "hierarchy",
            "file": file,
            "line": line,
            "col": col,
        })
        return dict(self.hierarchy)


def _ready_capability(language: str) -> dict:
    return {
        "language": language,
        "capability": f"diagnostics.{language}.workspace",
        "state": "ready",
        "provider": "jdtls" if language == "java" else "metals",
        "build_ready": True,
        "toolchain_ready": True,
        "runtime_ready": True,
        "start_allowed": True,
        "reason": "",
    }


def test_java_diagnostics_uses_local_jdtls_when_available(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "src" / "main" / "java" / "App.java"
    target.parent.mkdir(parents=True)
    target.write_text("class App {}\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        hlt,
        "_workspace_diagnostics_capability",
        _ready_capability,
    )
    bridge = _FakeBridge({
        "diagnostics": [{
            "message": "cannot resolve symbol",
            "severity": "error",
            "line": 2,
            "col": 4,
            "source": "jdt",
            "code": "java.problem",
        }],
        "overlay": False,
        "restored": False,
    })
    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: bridge,
    )

    tools = _build_tools({})
    payload = json.loads(_run(tools["omni_diagnostics"](
        file="src/main/java/App.java",
        severity="all",
        sources="guard,lsp",
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["source"] == "jdtls"
    assert payload["diagnostics_status"] == "target_errors"
    assert payload["tools_run"] == ["jdtls"]
    assert payload["diagnostics"][0]["line"] == 3
    assert payload["diagnostics"][0]["column"] == 5
    assert bridge.calls[0]["content"] is None


def test_scala_diagnostics_uses_metals_instead_of_unsupported(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "core" / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App {}\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        hlt,
        "_workspace_diagnostics_capability",
        _ready_capability,
    )
    bridge = _FakeBridge({
        "diagnostics": [],
        "overlay": False,
        "restored": False,
    })
    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: bridge,
    )

    tools = _build_tools({})
    payload = json.loads(_run(tools["omni_diagnostics"](
        file="core/src/main/scala/App.scala",
        severity="all",
        sources="lsp",
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["source"] == "metals"
    assert payload["diagnostics_status"] == "clean"
    assert payload["tools_run"] == ["metals"]
    assert payload["counts"]["total"] == 0


def test_scala_patch_validate_uses_lsp_overlay_and_restores(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "core" / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App {}\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        hlt,
        "_workspace_diagnostics_capability",
        _ready_capability,
    )
    bridge = _FakeBridge({
        "diagnostics": [],
        "overlay": True,
        "restored": True,
    })
    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: bridge,
    )

    tools = _build_tools({})
    candidate = "object App { def value: Int = 1 }\n"
    payload = json.loads(_run(tools["omni_patch"](
        action="validate",
        file="core/src/main/scala/App.scala",
        content=candidate,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["validation_passed"] is True
    assert payload["validation"]["reason"] == "scala_workspace_lsp_passed"
    assert payload["source"] == "metals"
    assert payload["overlay"] is True
    assert payload["restored"] is True
    assert bridge.calls[0]["content"] == candidate
    assert bridge.calls[0]["restore_after"] is True
    assert target.read_text(encoding="utf-8") == "object App {}\n"


def test_scala_patch_validate_lsp_failure_is_not_performed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "core" / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App {}\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        hlt,
        "_workspace_diagnostics_capability",
        _ready_capability,
    )
    bridge = _FakeBridge({
        "error": "Metals import failed",
        "error_code": "build_import_failed",
    })
    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: bridge,
    )

    tools = _build_tools({})
    payload = json.loads(_run(tools["omni_patch"](
        action="validate",
        file="core/src/main/scala/App.scala",
        content="object App { def broken( = 1 }\n",
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["validation_passed"] is None
    assert payload["validation"]["status"] == "not_performed"
    assert payload["validation"]["reason"] == "build_import_failed"
    assert "build_import_failed" in payload["warnings"]


def test_java_impact_merges_jdtls_references_and_call_hierarchy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "src" / "main" / "java" / "BaseHandler.java"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class BaseHandler {\n"
        "  void dispatch() {}\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-java")
    monkeypatch.setattr(
        hlt,
        "_workspace_diagnostics_capability",
        _ready_capability,
    )
    bridge = _FakeBridge(
        {"diagnostics": []},
        references={
            "locations": [{
                "file": "src/test/java/BaseHandlerTest.java",
                "line": 7,
                "col": 3,
            }]
        },
        hierarchy={
            "incoming": [{
                "name": "handle",
                "file": "src/main/java/Dispatcher.java",
                "line": 11,
                "col": 2,
            }],
            "outgoing": [{
                "name": "middleware",
                "file": "src/main/java/Middleware.java",
                "line": 4,
                "col": 1,
            }],
            "prepared": True,
        },
    )
    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: bridge,
    )
    tools = _build_tools({
        "/graph/risk": {"risk": "unknown", "reasons": []},
        "/graph/impact": {
            "symbol_found": True,
            "found": True,
            "graph_available": False,
            "graph_status": "partial",
            "impact_status": "unknown",
            "affected_symbols": [],
            "dependent_symbols": [],
            "files_count": 1,
            "files_involved": ["src/main/java/BaseHandler.java"],
            "snapshot_symbol": {
                "file_path": "src/main/java/BaseHandler.java",
                "symbol_name": "BaseHandler",
                "line_start": 1,
                "line_end": 3,
                "revision": 9,
            },
            "references": [],
            "test_candidates": [],
            "evidence_providers": ["tree_sitter_ast"],
        },
        "/graph/related-tests": {
            "test_files": [],
            "suggested_commands": [],
        },
    })

    payload = json.loads(_run(tools["omni_impact"](
        symbol="BaseHandler",
        depth=2,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"
    assert payload["graph_status"] == "ready"
    assert payload["impact_status"] == "available"
    assert "handle" in payload["callers"]
    assert "middleware" in payload["callees"]
    assert payload["references"][0]["source"] == "jdtls"
    assert payload["lsp_evidence"]["provider"] == "jdtls"
    assert "jdtls" in payload["evidence_providers"]
