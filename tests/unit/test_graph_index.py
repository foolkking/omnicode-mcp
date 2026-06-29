from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from omnicode_core.workspace.graph_index import WorkspaceGraphIndex
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def _index(tmp_path: Path) -> WorkspaceGraphIndex:
    store = CloudSnapshotStore(root=tmp_path / "cloud-sync")
    return WorkspaceGraphIndex(store=store)


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_graph_index_persists_relations_and_revision(tmp_path: Path) -> None:
    index = _index(tmp_path)
    content = (
        "def target():\n"
        "    helper()\n"
        "\n"
        "def caller():\n"
        "    target()\n"
    )

    revision = index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "pkg/service.py",
                "hash": "sha256:test",
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=7,
    )

    status = index.status(workspace_id="repo-a", accepted_revision=7)
    assert revision == 7
    assert status["ready"] is True
    assert status["graph_indexed_revision"] == 7
    assert status["files"] == 1
    assert status["supported_files"] == 1
    assert status["edges"] >= 2
    assert "python" in status["languages"]

    impact = index.impact(
        workspace_id="repo-a",
        symbol="target",
        depth=2,
    )
    assert impact["found"] is True
    assert "caller" in impact["dependent_symbols"]
    assert "helper" in impact["affected_symbols"]
    assert impact["files_involved"] == ["pkg/service.py"]

    reopened = WorkspaceGraphIndex(store=index.store)
    persisted = reopened.impact(
        workspace_id="repo-a",
        symbol="target",
        depth=1,
    )
    assert persisted["found"] is True
    assert "caller" in persisted["direct_callers"]


def test_graph_index_try_status_reports_busy_without_waiting(tmp_path: Path) -> None:
    index = _index(tmp_path)
    lock = index._workspace_lock("repo-a")
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with lock:
            acquired.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=2)
    try:
        status = index.try_status(
            workspace_id="repo-a",
            accepted_revision=10,
            lock_timeout_ms=1,
        )
    finally:
        release.set()
        thread.join(timeout=2)

    assert status["ready"] is False
    assert status["busy"] is True
    assert status["last_error"] == "graph_index_busy"


def test_graph_index_readiness_avoids_detailed_aggregate_counts(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[{
            "path": "pkg/service.py",
            "hash": "sha256:test",
            "content": "def target():\n    helper()\n",
        }],
        deleted_paths=[],
        revision=7,
    )

    readiness = index.try_readiness(
        workspace_id="repo-a",
        accepted_revision=7,
    )

    assert readiness["ready"] is True
    assert readiness["current"] is True
    assert readiness["graph_indexed_revision"] == 7
    assert readiness["coverage_complete"] is True
    assert readiness["supported_files_present"] is True
    assert readiness["status_detail"] == "readiness"
    assert "edges" not in readiness


def test_graph_index_incremental_replace_and_delete(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "pkg/service.py",
                "hash": "sha256:v1",
                "content": "def caller():\n    old_target()\n",
            }
        ],
        deleted_paths=[],
        revision=1,
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "pkg/service.py",
                "hash": "sha256:v2",
                "content": "def caller():\n    new_target()\n",
            }
        ],
        deleted_paths=[],
        revision=2,
    )

    old_impact = index.impact(
        workspace_id="repo-a",
        symbol="old_target",
    )
    new_impact = index.impact(
        workspace_id="repo-a",
        symbol="new_target",
    )
    assert old_impact["found"] is False
    assert new_impact["found"] is True

    index.update_batch(
        workspace_id="repo-a",
        changed_files=[],
        deleted_paths=["pkg/service.py"],
        revision=3,
    )
    status = index.status(workspace_id="repo-a", accepted_revision=3)
    assert status["ready"] is False
    assert status["files"] == 0
    assert status["edges"] == 0


def test_graph_index_class_path_aggregates_method_edges(tmp_path: Path) -> None:
    index = _index(tmp_path)
    class_content = (
        "class BaseHandler:\n"
        "    def get_response(self):\n"
        "        middleware()\n"
    )
    caller_content = (
        "def dispatch(handler):\n"
        "    get_response()\n"
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": "sha256:base",
                "content": class_content,
            },
            {
                "path": "django/core/handlers/wsgi.py",
                "hash": "sha256:wsgi",
                "content": caller_content,
            },
        ],
        deleted_paths=[],
        revision=9,
    )

    impact = index.impact(
        workspace_id="repo-a",
        symbol="BaseHandler",
        symbol_path="django/core/handlers/base.py",
        depth=2,
    )
    assert impact["found"] is True
    assert impact["resolution_mode"] == "file_symbol_aggregate"
    assert "dispatch" in impact["dependent_symbols"]
    assert "middleware" in impact["affected_symbols"]


def test_graph_index_records_scala_lexical_fallback_without_false_confidence(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "core/ReplicaManager.scala",
                "hash": "sha256:scala",
                "content": "class ReplicaManager { def run() = helper() }\n",
            }
        ],
        deleted_paths=[],
        revision=4,
    )

    status = index.status(workspace_id="repo-a", accepted_revision=4)
    assert status["ready"] is True
    assert status["unsupported_files"] == 0
    assert status["supported_files"] == 1
    assert status["edges"] == 0
    assert status["definitions"] >= 1

    impact = index.impact(
        workspace_id="repo-a",
        symbol="ReplicaManager",
        symbol_path="core/ReplicaManager.scala",
    )
    assert impact["found"] is True
    assert impact["definitions"][0]["name"] == "ReplicaManager"
    assert impact["evidence_providers"] == ["scala_lexical_fallback"]


def test_graph_index_persists_java_inheritance_references_and_tests(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    source = (
        "package demo;\n"
        "import demo.middleware.Filter;\n"
        "class BaseHandler {}\n"
        "class Handler extends BaseHandler {\n"
        "  void dispatch() { middleware(); }\n"
        "}\n"
    )
    test_source = (
        "class HandlerTest {\n"
        "  void verifies() { middleware(); }\n"
        "}\n"
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "src/main/java/demo/Handler.java",
                "hash": "sha256:java-main",
                "content": source,
            },
            {
                "path": "src/test/java/demo/HandlerTest.java",
                "hash": "sha256:java-test",
                "content": test_source,
            },
        ],
        deleted_paths=[],
        revision=10,
    )

    status = index.status(workspace_id="repo-a", accepted_revision=10)
    assert status["ready"] is True
    assert status["references"] >= 2
    assert status["inheritance_edges"] >= 1
    assert status["import_edges"] >= 1
    assert status["test_edges"] >= 1

    handler = index.impact(
        workspace_id="repo-a",
        symbol="Handler",
        symbol_path="src/main/java/demo/Handler.java",
    )
    assert handler["found"] is True
    assert "BaseHandler" in handler["inheritance"]["bases"]
    assert "tree_sitter_ast" in handler["evidence_providers"]

    middleware = index.impact(
        workspace_id="repo-a",
        symbol="middleware",
    )
    assert middleware["found"] is True
    assert any(
        row["path"] == "src/test/java/demo/HandlerTest.java"
        for row in middleware["references"]
    )
    assert "src/test/java/demo/HandlerTest.java" in middleware["test_candidates"]


def test_graph_index_scala_fallback_finds_definition_reference_and_test(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    main = (
        "package kafka.server\n"
        "class ReplicaManager extends BaseManager {\n"
        "  def checkpoint(): Unit = persist()\n"
        "}\n"
    )
    test = (
        "class ReplicaManagerTest {\n"
        "  def verifies(): Unit = ReplicaManager()\n"
        "}\n"
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "core/src/main/scala/kafka/server/ReplicaManager.scala",
                "hash": "sha256:scala-main",
                "content": main,
            },
            {
                "path": "core/src/test/scala/kafka/server/ReplicaManagerTest.scala",
                "hash": "sha256:scala-test",
                "content": test,
            },
        ],
        deleted_paths=[],
        revision=11,
    )

    impact = index.impact(
        workspace_id="repo-a",
        symbol="ReplicaManager",
        symbol_path="core/src/main/scala/kafka/server/ReplicaManager.scala",
    )

    assert impact["found"] is True
    assert impact["definitions"]
    assert "BaseManager" in impact["inheritance"]["bases"]
    assert any(
        row["path"].endswith("ReplicaManagerTest.scala")
        for row in impact["references"]
    )
    assert any(
        path.endswith("ReplicaManagerTest.scala")
        for path in impact["test_candidates"]
    )
    assert "scala_lexical_fallback" in impact["evidence_providers"]


def test_graph_index_lsp_reference_enrichment_is_persisted(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[{
            "path": "src/main/java/App.java",
            "hash": "sha256:app",
            "content": "class App {}\n",
        }],
        deleted_paths=[],
        revision=12,
    )

    count = index.upsert_lsp_references(
        workspace_id="repo-a",
        symbol="App",
        references=[{
            "file": "src/test/java/AppTest.java",
            "line": 9,
            "context": "new App()",
        }],
        revision=12,
        language="java",
        provider="jdtls",
    )

    impact = index.impact(
        workspace_id="repo-a",
        symbol="App",
    )
    assert count == 1
    assert any(row["source_provider"] == "jdtls" for row in impact["references"])
    assert "src/test/java/AppTest.java" in impact["test_candidates"]
    assert "jdtls" in impact["evidence_providers"]


def test_graph_index_bootstraps_complete_snapshot_coverage(tmp_path: Path) -> None:
    index = _index(tmp_path)
    first = "def target():\n    helper()\n"
    second = "def caller():\n    target()\n"
    for path, content in (
        ("pkg/target.py", first),
        ("pkg/caller.py", second),
    ):
        index.store.upsert(
            workspace_id="repo-a",
            path=path,
            content=content,
            hash_value=_sha(content),
            size=len(content),
            mtime_ms=1,
            encoding="utf-8",
            revision=5,
        )

    result = index.index_snapshot_store(
        workspace_id="repo-a",
        revision=5,
        force=True,
    )

    status = result["status"]
    assert result["records_seen"] == 2
    assert status["coverage_complete"] is True
    assert status["ready"] is True
    assert status["files"] == 2
    assert status["graph_indexed_revision"] == 5


def test_partial_incremental_graph_is_not_ready_until_bootstrap(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    content = "def caller():\n    target()\n"
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "pkg/caller.py",
                "hash": _sha(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=8,
        coverage_complete=False,
    )

    status = index.status(workspace_id="repo-a", accepted_revision=8)
    assert status["current"] is True
    assert status["coverage_complete"] is False
    assert status["ready"] is False
