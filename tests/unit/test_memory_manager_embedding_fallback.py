from __future__ import annotations

import pytest

from memory_system.memory_manager import MemoryManager
from memory_system.models import (
    MemoryCategory,
    MemoryRequest,
    MemorySearchRequest,
)
from omnicode_core.embeddings.backend import UnavailableEmbeddingBackend


@pytest.mark.asyncio
async def test_memory_manager_degrades_when_embedding_backend_unavailable(
    tmp_path,
    monkeypatch,
):
    def _raise_backend(model_name: str):
        raise OSError("model not cached")

    monkeypatch.setattr(
        "memory_system.memory_manager.resolve_backend",
        _raise_backend,
    )

    manager = MemoryManager(str(tmp_path))
    await manager.initialize()

    status = manager.get_embedding_status()
    assert status["available"] is False
    assert isinstance(manager.embedding_model, UnavailableEmbeddingBackend)

    memory = await manager.store_memory(
        MemoryRequest(
            category=MemoryCategory.MISTAKE,
            content="Offline memory fallback should still store and search.",
            tags=["offline", "fallback"],
        )
    )
    assert memory.id is not None
    assert memory.embedding_vector is None

    results = await manager.search_memories(
        MemorySearchRequest(query="offline memory fallback")
    )
    assert results
    assert results[0].memory.id == memory.id
    assert results[0].keyword_score and results[0].keyword_score > 0
    assert results[0].semantic_score == 0.0
