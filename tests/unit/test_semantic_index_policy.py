from __future__ import annotations

import numpy as np
import pytest

from omnicode.search.engine import SemanticSearchEngine
from omnicode_core.workspace.semantic_index_policy import (
    semantic_index_decision,
    semantic_index_metadata,
    semantic_index_policy_payload,
)


def test_semantic_policy_skips_low_value_tests_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_SEMANTIC_TEST_MODE", raising=False)
    include, reason = semantic_index_decision(
        "integration-tests/src/test/scala/kafka/FatIntegrationSuite.scala",
        "class FatIntegrationSuite\n",
        {},
    )

    assert include is False
    assert reason == "semantic_low_value_test_path"


def test_semantic_policy_keeps_small_unit_tests_with_lower_chunk_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_SEMANTIC_TEST_MODE", raising=False)
    path = "core/src/test/scala/kafka/server/ReplicaManagerTest.scala"
    include, reason = semantic_index_decision(path, "class ReplicaManagerTest\n", {})
    metadata = semantic_index_metadata(path, "class ReplicaManagerTest\n", {})

    assert include is True
    assert reason == "included"
    assert metadata["semantic_path_category"] == "test"
    assert metadata["semantic_max_chunks_per_file"] == 12


def test_semantic_full_bootstrap_limited_mode_skips_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_SEMANTIC_TEST_MODE", raising=False)
    include, reason = semantic_index_decision(
        "core/src/test/scala/kafka/server/ReplicaManagerTest.scala",
        "class ReplicaManagerTest\n",
        {"phase": "semantic_full_bootstrap"},
    )

    assert include is False
    assert reason == "semantic_test_path_limited_bootstrap"


def test_semantic_policy_payload_exposes_test_and_chunk_limits() -> None:
    payload = semantic_index_policy_payload()

    assert payload["test_mode"] in {"limited", "full", "off"}
    assert payload["max_chunks_per_file"] >= 1
    assert payload["test_max_chunks_per_file"] >= 1
    assert "integration-tests" in payload["low_value_test_parts"]


@pytest.mark.asyncio
async def test_semantic_bulk_upsert_applies_chunk_limit(tmp_path) -> None:
    class _Embedding:
        name = "test-embedding"
        dimension = 4
        _model_name = "test-embedding"

        @staticmethod
        def encode(values):
            if isinstance(values, list):
                return np.ones((len(values), 4), dtype=np.float32)
            return np.ones(4, dtype=np.float32)

    engine = SemanticSearchEngine(str(tmp_path / "repo"), db_dir=str(tmp_path / "db"))
    engine.embedding_model = _Embedding()
    content = "\n".join(
        [f"def f_{idx}():\n    return {idx}\n" for idx in range(20)]
    )

    chunks = await engine.upsert_contents(
        [
            (
                "pkg/many_symbols.py",
                content,
                {
                    "workspace_id": "repo-a",
                    "snapshot_revision": 1,
                    "semantic_max_chunks_per_file": 5,
                },
            )
        ],
        refresh=False,
    )

    assert chunks == 5
    assert engine.last_upsert_stats["files_truncated_by_chunk_limit"] == 1
    assert engine.last_upsert_stats["chunks_dropped_by_limit"] > 0

    cursor = engine.vector_store.conn.cursor()
    cursor.execute("SELECT metadata FROM chunks LIMIT 1")
    metadata = cursor.fetchone()[0]
    assert "semantic_chunk_limit_applied" in metadata
