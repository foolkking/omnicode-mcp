from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from tests.unit.mcp_harness import build_tools, run


def _empty_graph_routes() -> Dict[str, Any]:
    return {
        "/graph/risk": {"risk": "low", "reasons": ["no graph edges"]},
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
        "/search/symbols": {"results": [], "total_results": 0},
        "/search/text": {"results": [], "total_results": 0},
        "/guard/check": {"ok": True, "diagnostics": []},
        "/lsp/diagnostics": {"diagnostics": []},
    }


def _bootstrap_local_exact_index(tmp_path: Path, monkeypatch) -> Path:
    workspace = tmp_path / "repo"
    source = workspace / "pkg"
    source.mkdir(parents=True)
    (source / "known.py").write_text(
        "class KnownSymbol:\n"
        "    def method(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-test")

    from omnicode_core.workspace.exact_index import SnapshotExactIndex

    result = SnapshotExactIndex().index_workspace_root(
        workspace_id="repo-test",
        root=workspace,
        force=True,
    )
    assert result["status"]["symbols"] >= 1
    return workspace


def test_impact_uses_local_exact_symbol_when_graph_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _bootstrap_local_exact_index(tmp_path, monkeypatch)
    tools = build_tools(_empty_graph_routes())

    payload = json.loads(run(tools["omni_impact"](
        symbol="KnownSymbol",
        depth=2,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"
    assert payload["risk"] == "unknown"
    assert payload["confidence"] == "low"
    assert payload["source"] == "graph+symbol_fallback"
    assert payload["symbol_fallback"]["file"] == "pkg/known.py"
    assert "impact.graph" in payload["capabilities_missing"]
    assert payload["fallback"]["references"]
    assert payload["fallback"]["references"][0]["file"] == "pkg/known.py"
    assert payload["fallback"]["test_candidates"]
    assert payload["fallback"]["test_candidate_source"] == "path_heuristic"


def test_context_reports_deterministic_degraded_sections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _bootstrap_local_exact_index(tmp_path, monkeypatch)
    tools = build_tools(_empty_graph_routes())

    payload = json.loads(run(tools["omni_context"](
        symbol="KnownSymbol",
        token_budget=3000,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["symbol_resolution"] == "found"
    assert payload["context_builder"] == "deterministic"
    assert payload["degraded"] is True
    assert payload["context"]["definition"]["available"] is True
    assert payload["context"]["definition"]["file"] == "pkg/known.py"
    assert payload["context"]["graph"]["available"] is False
    assert "impact.graph" in payload["capabilities_missing"]


def test_symbol_definition_dedupe_ignores_provider_duplicates() -> None:
    from omnicode_adapters.mcp_server.high_level_tools import (
        _dedupe_symbol_definition_rows,
    )

    rows = [
        {
            "file_path": "pkg/known.py",
            "symbol_name": "KnownSymbol",
            "line_start": 1,
            "line_end": 3,
            "signature": "class KnownSymbol:",
            "source": "cloud_exact_index",
        },
        {
            "file": "pkg\\known.py",
            "name": "KnownSymbol",
            "line": 1,
            "end_line": 3,
            "signature": "class KnownSymbol:",
            "source": "local_exact_index",
        },
        {
            "file_path": "pkg/other.py",
            "symbol_name": "KnownSymbol",
            "line_start": 9,
            "line_end": 11,
            "signature": "class KnownSymbol:",
            "source": "cloud_exact_index",
        },
    ]

    deduped = _dedupe_symbol_definition_rows(rows)

    assert len(deduped) == 2


def test_context_file_symbol_uses_fast_local_path_without_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _bootstrap_local_exact_index(tmp_path, monkeypatch)
    tools = build_tools({})

    payload = json.loads(run(tools["omni_context"](
        file="pkg/known.py",
        symbol="KnownSymbol",
        token_budget=3000,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["context_builder"] == "deterministic_fast"
    assert payload["symbol_resolution"] == "found"
    assert payload["context"]["definition"]["file"] == "pkg/known.py"
    assert payload["context"]["local_neighborhood"]["available"] is True
    assert "impact.graph" in payload["capabilities_missing"]
    assert "search.semantic" in payload["capabilities_missing"]
    assert payload["diagnostics_status"]["ran"] is False
    assert tools["__captured__"] == {}


def test_local_symbol_search_reports_index_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-empty")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "local")
    tools = build_tools({
        "/search/symbols": {"results": [], "total_results": 0},
    })

    payload = json.loads(run(tools["omni_search"](
        query="KnownSymbol",
        mode="symbol",
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["error_code"] == "INDEX_NOT_READY"
    assert payload["empty_reason"] == "index_not_ready"
    assert payload["local_index"]["ready"] is False
    assert "omni_index" in " ".join(payload["next_actions"])


def test_auto_code_declaration_routes_to_text() -> None:
    from omnicode_adapters.mcp_server.high_level_tools import _detect_mode

    assert _detect_mode("def _detect_mode") == "text"
    assert _detect_mode("class BaseHandler:") == "text"
    assert _detect_mode("class ReplicaManager") == "text"


def test_text_search_uses_local_exact_index_when_backend_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _bootstrap_local_exact_index(tmp_path, monkeypatch)
    tools = build_tools({
        "/search/text": {
            "results": [],
            "total_results": 0,
            "provider": "cloud_snapshot_grep",
            "provider_chain": ["cloud_snapshot_grep"],
            "empty_reason": "true_empty",
        },
    })

    payload = json.loads(run(tools["omni_search"](
        query="class KnownSymbol:",
        mode="auto",
        format="json",
        max_results=5,
    )))

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "text"
    assert payload["provider"] == "local_exact_index"
    assert payload["results"][0]["file"] == "pkg/known.py"
    assert payload["results"][0]["source"] == "local_exact_index"


def test_text_search_default_patterns_include_scala(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "core" / "src" / "main" / "scala" / "kafka" / "server"
    source.mkdir(parents=True)
    (source / "ReplicaManager.scala").write_text(
        "object ReplicaManager {\n"
        "  val MetricNames = Seq.empty[String]\n"
        "}\n"
        "class ReplicaManager(val brokerId: Int)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "scala-text")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "local")

    from omnicode_core.workspace.exact_index import SnapshotExactIndex

    SnapshotExactIndex().index_workspace_root(
        workspace_id="scala-text",
        root=workspace,
        force=True,
    )
    tools = build_tools({})

    payload = json.loads(run(tools["omni_search"](
        query="class ReplicaManager",
        mode="auto",
        format="json",
        max_results=5,
    )))

    assert payload["ok"] is True
    assert payload["resolved_mode"] == "text"
    assert payload["results"][0]["file"].endswith("ReplicaManager.scala")
    assert payload["results"][0]["line"] == 4


def test_omni_index_workspace_bootstrap_is_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "pkg"
    source.mkdir(parents=True)
    (source / "bootstrap.py").write_text(
        "class BootstrapSymbol:\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-bootstrap")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "local")
    tools = build_tools({})

    payload = json.loads(run(tools["omni_index"](
        action="bootstrap",
        scope="workspace",
        background=False,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["source"] == "local_exact_index"
    assert payload["local_index_ready"] is True
    assert payload["result"]["status"]["symbols"] >= 1
    assert tools["__captured__"] == {}

    search = json.loads(run(tools["omni_search"](
        query="BootstrapSymbol",
        mode="symbol",
        format="json",
    )))
    assert search["ok"] is True
    assert search["results"][0]["file"] == "pkg/bootstrap.py"


def test_omni_index_graph_bootstrap_is_local_and_persistent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "pkg"
    source.mkdir(parents=True)
    (source / "graph_bootstrap.py").write_text(
        "def target():\n"
        "    helper()\n"
        "\n"
        "def caller():\n"
        "    target()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-graph-bootstrap")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "local")
    tools = build_tools({})

    payload = json.loads(run(tools["omni_index"](
        action="bootstrap",
        scope="graph",
        background=False,
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["source"] == "local_graph_index"
    assert payload["graph_index_ready"] is True
    assert payload["result"]["status"]["edges"] >= 2
    assert tools["__captured__"] == {}

    status = json.loads(run(tools["omni_index"](
        action="status",
        scope="graph",
        format="json",
    )))
    assert status["ok"] is True
    assert status["graph_index_ready"] is True
    assert status["status"]["graph_indexed_revision"] >= 1
