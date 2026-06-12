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
    assert index.status(workspace_id="repo-a")["files"] == 0


def test_exact_symbol_ranking_prefers_case_exact_class_over_method(
    tmp_path: Path,
) -> None:
    index = SnapshotExactIndex(
        store=CloudSnapshotStore(root=tmp_path / "cloud-sync")
    )
    broker = "class BrokerServer:\n    def replicaManager: ReplicaManager = x\n"
    replica = "class ReplicaManager(val config: KafkaConfig,\n"
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
