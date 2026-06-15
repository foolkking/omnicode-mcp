from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from omnicode.search.engine import SemanticSearchEngine
from omnicode.search.models import SearchRequest
from omnicode.search.vector_store import VectorStore


class _FakeEmbeddingBackend:
    def __init__(self, dimension: int, name: str = "fake_embedding") -> None:
        self.dimension = dimension
        self.name = name

    def encode(self, text):
        if isinstance(text, list):
            return np.ones((len(text), self.dimension), dtype=np.float32)
        return np.ones((self.dimension,), dtype=np.float32)


def test_vector_store_records_and_checks_embedding_metadata(
    tmp_path: Path,
) -> None:
    store = VectorStore(str(tmp_path / "vector_store.db"), dimension=384)

    metadata = store.set_index_metadata(
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        embedding_revision="main",
        embedding_dimension=384,
        embedding_backend="sentence-transformers",
        chunker_version="chunker-v1",
        workspace_id="repo-a",
    )

    assert metadata["embedding_dimension"] == 384
    status = store.semantic_metadata_status(
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        embedding_dimension=384,
        chunker_version="chunker-v1",
    )
    assert status["semantic_index_invalid"] is False
    assert status["semantic_index_stale"] is False
    assert status["semantic_index_model"] == "sentence-transformers/all-MiniLM-L6-v2"

    mismatch = store.semantic_metadata_status(
        embedding_model="sentence-transformers/all-mpnet-base-v2",
        embedding_revision="main",
        embedding_dimension=768,
        chunker_version="chunker-v1",
    )
    assert mismatch["semantic_index_stale"] is True
    assert mismatch["semantic_index_invalid"] is True
    assert "embedding_dimension_mismatch" in mismatch["semantic_index_stale_reason"]

    revision_mismatch = store.semantic_metadata_status(
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        embedding_revision="abcdef",
        embedding_dimension=384,
        chunker_version="chunker-v1",
    )
    assert revision_mismatch["semantic_index_stale"] is True
    assert "embedding_revision_mismatch" in revision_mismatch[
        "semantic_index_stale_reason"
    ]


@pytest.mark.asyncio
async def test_semantic_upsert_writes_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    monkeypatch.delenv("OMNICODE_EMBEDDING_REVISION", raising=False)
    engine = SemanticSearchEngine(str(tmp_path / "repo"))
    engine.embedding_model = _FakeEmbeddingBackend(384)

    count = await engine.upsert_content(
        "pkg/a.py",
        "def target():\n    return 'needle'\n",
        workspace_id="repo-a",
    )

    metadata = engine.vector_store.get_index_metadata()
    assert count > 0
    assert metadata["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert metadata["embedding_dimension"] == 384
    assert metadata["embedding_backend"] == "fake_embedding"
    assert metadata["chunker_version"]
    assert metadata["workspace_id"] == "repo-a"
    assert engine.semantic_index_status()["semantic_index_ready"] is True


@pytest.mark.asyncio
async def test_semantic_query_blocks_mismatched_model_before_faiss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    engine = SemanticSearchEngine(str(tmp_path / "repo"))
    engine.embedding_model = _FakeEmbeddingBackend(384)
    await engine.upsert_content(
        "pkg/a.py",
        "def target():\n    return 'needle'\n",
        workspace_id="repo-a",
    )

    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-mpnet-base-v2",
    )
    engine.embedding_model = _FakeEmbeddingBackend(768)

    with pytest.raises(RuntimeError, match="SEMANTIC_INDEX_NOT_READY"):
        await engine.search(
            SearchRequest(
                query="needle",
                search_type="semantic",
                max_results=5,
            )
        )

    status = engine.semantic_index_status()
    assert status["semantic_index_ready"] is False
    assert status["semantic_index_invalid"] is True
    assert "embedding_dimension_mismatch" in status["semantic_index_stale_reason"]


@pytest.mark.asyncio
async def test_semantic_force_rebuild_resets_dimension_and_updates_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    engine = SemanticSearchEngine(str(tmp_path / "repo"))
    engine.embedding_model = _FakeEmbeddingBackend(384)
    await engine.upsert_content(
        "pkg/a.py",
        "def target():\n    return 'needle'\n",
        workspace_id="repo-a",
    )

    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-mpnet-base-v2",
    )
    engine.embedding_model = _FakeEmbeddingBackend(768)
    engine.prepare_semantic_index(force=True, workspace_id="repo-a")
    await engine.upsert_content(
        "pkg/b.py",
        "def target_two():\n    return 'needle two'\n",
        workspace_id="repo-a",
    )

    metadata = engine.vector_store.get_index_metadata()
    assert engine.vector_store.index_dimension() == 768
    assert metadata["embedding_model"] == "sentence-transformers/all-mpnet-base-v2"
    assert metadata["embedding_dimension"] == 768
    assert engine.semantic_index_status()["semantic_index_ready"] is True


@pytest.mark.asyncio
async def test_index_codebase_writes_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "module.py").write_text(
        "def indexed_target():\n    return 'needle'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    engine = SemanticSearchEngine(str(repo))
    engine.embedding_model = _FakeEmbeddingBackend(384)

    await engine.index_codebase()

    metadata = engine.vector_store.get_index_metadata()
    assert metadata["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert metadata["embedding_dimension"] == 384
    assert metadata["chunker_version"]
    assert engine.semantic_index_status()["semantic_index_ready"] is True
