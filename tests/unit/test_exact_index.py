from __future__ import annotations

import hashlib
from pathlib import Path

from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_exact_index_updates_symbol_and_text_rows(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path / "cloud-sync")
    index = SnapshotExactIndex(store=store)
    content = (
        "class BaseHandler:\n"
        "    def load_middleware(self):\n"
        "        return 'middleware-chain'\n"
    )

    revision = index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    status = index.status(workspace_id="repo-a")
    assert revision == 11
    assert status["exact_indexed_revision"] == 11
    assert status["files"] == 1
    assert status["symbols"] == 2
    assert status["lines"] == 3
    assert status["schema_version"] >= 2
    assert status["index_kind"] == "workspace_exact"
    assert "line_fts_available" in status
    assert status["line_fts_mode"] in {"auto", "on", "off"}
    assert status["line_fts_auto_line_limit"] == 50000
    assert "line_fts_reason" in status

    symbols = index.search_symbols(
        workspace_id="repo-a",
        query="BaseHandler",
        max_results=5,
    )
    assert len(symbols) == 1
    assert symbols[0].path == "django/core/handlers/base.py"
    assert symbols[0].name == "BaseHandler"
    assert symbols[0].why == "symbol:exact"

    text = index.search_text(
        workspace_id="repo-a",
        query="middleware-chain",
        max_results=5,
        context_lines=1,
    )
    assert len(text) == 1
    assert text[0].path == "django/core/handlers/base.py"
    assert text[0].line_no == 3
    assert text[0].context_before == ["    def load_middleware(self):"]


def test_exact_index_token_overlap_finds_multi_token_lines(tmp_path: Path) -> None:
    store = CloudSnapshotStore(root=tmp_path / "cloud-sync")
    index = SnapshotExactIndex(store=store)
    content = (
        "class BaseHandler:\n"
        "    def get_response(self, request):\n"
        "        response = self._middleware_chain(request)\n"
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            },
            {
                "path": "django/conf/global_settings.py",
                "hash": _sha("# middleware for request phase\n"),
                "size": 31,
                "content": "# middleware for request phase\n",
            },
        ],
        deleted_paths=[],
        revision=12,
    )

    rows = index.search_token_overlap(
        workspace_id="repo-a",
        tokens=["request", "middleware", "chain"],
        max_results=5,
    )

    assert rows
    assert rows[0].path == "django/core/handlers/base.py"
    assert set(rows[0].matched_tokens) == {"request", "middleware", "chain"}


def test_exact_index_fts_off_keeps_symbols_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EXACT_LINE_FTS", "off")
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )
    content = "class BaseHandler:\n    pass\n"
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=4,
    )

    status = index.status(workspace_id="repo-a")
    assert status["line_fts_available"] is False
    assert status["line_fts_reason"] == "disabled_by_env"
    assert index.search_symbols(workspace_id="repo-a", query="BaseHandler")


def test_exact_index_workspace_bootstrap_indexes_text_and_symbols(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "core" / "src" / "main" / "scala" / "kafka" / "server"
    target.mkdir(parents=True)
    (target / "ReplicaManager.scala").write_text(
        "class ReplicaManager(val config: KafkaConfig) {\n"
        "  def startup(): Unit = {}\n"
        "}\n",
        encoding="utf-8",
    )
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )

    result = index.index_workspace_root(
        workspace_id="repo-a",
        root=workspace,
        revision=12,
        force=True,
    )

    assert result["files_indexed"] == 1
    assert result["status"]["files"] == 1
    symbols = index.search_symbols(
        workspace_id="repo-a",
        query="ReplicaManager",
    )
    assert symbols[0].path.endswith("ReplicaManager.scala")
    text = index.search_text(
        workspace_id="repo-a",
        query="class ReplicaManager",
    )
    assert text[0].path.endswith("ReplicaManager.scala")


def test_exact_index_deletes_stale_rows(tmp_path: Path) -> None:
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )
    content = "class BaseHandler:\n    pass\n"
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=2,
    )

    revision = index.update_batch(
        workspace_id="repo-a",
        changed_files=[],
        deleted_paths=["django/core/handlers/base.py"],
        revision=3,
    )

    assert revision == 3
    assert index.search_symbols(
        workspace_id="repo-a",
        query="BaseHandler",
    ) == []
    status = index.status(workspace_id="repo-a")
    assert status["files"] == 0
    assert status["symbols"] == 0
    assert status["lines"] == 0


def test_exact_index_maintains_counts_without_full_rescan_each_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )
    first = "class Service:\n    pass\n"
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[{
            "path": "pkg/service.py",
            "hash": _sha(first),
            "size": len(first),
            "content": first,
        }],
        deleted_paths=[],
        revision=1,
    )

    def _unexpected_rescan(_conn) -> None:
        raise AssertionError("existing count metadata must avoid a full-table rescan")

    monkeypatch.setattr(index, "_refresh_count_meta", _unexpected_rescan)
    second = "def one():\n    return 1\n\ndef two():\n    return 2\n"
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[{
            "path": "pkg/service.py",
            "hash": _sha(second),
            "size": len(second),
            "content": second,
        }],
        deleted_paths=[],
        revision=2,
    )

    status = index.status(workspace_id="repo-a")
    assert status["files"] == 1
    assert status["symbols"] == 2
    assert status["lines"] == 5


def test_exact_symbol_ranking_prefers_case_exact_class_over_method(
    tmp_path: Path,
) -> None:
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )
    broker = "class BrokerServer:\n    def replicaManager: ReplicaManager = x\n"
    replica = (
        "object ReplicaManager {\n"
        "  val MetricNames = Seq.empty[String]\n"
        "}\n"
        "class ReplicaManager(val config: KafkaConfig,\n"
    )
    index.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "core/src/main/scala/kafka/server/BrokerServer.scala",
                "hash": _sha(broker),
                "size": len(broker),
                "content": broker,
            },
            {
                "path": "core/src/main/scala/kafka/server/ReplicaManager.scala",
                "hash": _sha(replica),
                "size": len(replica),
                "content": replica,
            },
        ],
        deleted_paths=[],
        revision=10,
    )

    symbols = index.search_symbols(
        workspace_id="repo-a",
        query="ReplicaManager",
        max_results=5,
    )

    assert symbols[0].path == "core/src/main/scala/kafka/server/ReplicaManager.scala"
    assert symbols[0].name == "ReplicaManager"
    assert symbols[0].kind == "class"
    assert symbols[0].line_start == 4
    assert symbols[1].kind == "object"
