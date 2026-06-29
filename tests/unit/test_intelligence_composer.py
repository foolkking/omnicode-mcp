"""Unit tests for the Intelligence Composer (architecture-v2 §17)."""

from __future__ import annotations

import pytest

from omnicode_core.intelligence.composer import (
    Capability,
    CapabilityStatus,
    IntelligenceComposer,
    IntelligenceContext,
    list_capabilities,
)


def test_capability_enum_has_eight_members():
    """The composer covers exactly the eight capabilities §17 calls out."""
    assert len(Capability.all()) == 8


def test_capability_status_serialises():
    s = CapabilityStatus(
        Capability.SEARCH,
        available=True,
        detail="ok",
        backend="x",
        state="degraded",
        reason="semantic unavailable",
        metadata={"semantic_available": False},
    )
    d = s.to_dict()
    assert d["capability"] == "search"
    assert d["available"] is True
    assert d["detail"] == "ok"
    assert d["state"] == "degraded"
    assert d["reason"] == "semantic unavailable"
    assert d["metadata"]["semantic_available"] is False


def test_list_capabilities_returns_eight(monkeypatch):
    """All eight slots are reported even when service singletons are None."""
    # Force every singleton getter to return None so we exercise the
    # 'unavailable' branch of every probe.
    import core

    for name in (
        "get_search_engine",
        "get_memory_manager",
        "get_git_manager",
        "get_llm_router",
        "get_ast_parser",
    ):
        monkeypatch.setattr(core, name, lambda: None)

    out = list_capabilities()
    assert len(out) == 8
    by_cap = {s.capability: s for s in out}
    # These capabilities don't depend on a singleton so they must still
    # report available=True.
    assert by_cap[Capability.CONTEXT_COMPRESSION].available is True
    assert by_cap[Capability.SAFE_PATCH].available is True
    assert by_cap[Capability.IMPACT_ANALYSIS].available is True
    assert by_cap[Capability.DEBUG_CONSOLE].available is True
    # Singleton-backed ones should be unavailable now.
    assert by_cap[Capability.SEARCH].available is False
    assert by_cap[Capability.MEMORY_RECALL].available is False
    assert by_cap[Capability.LLM_ENHANCEMENT].available is False


def test_list_capabilities_marks_semantic_and_memory_degraded(monkeypatch):
    import core

    class _Embedding:
        name = "embedding-unavailable"

    class _SearchEngine:
        embedding_model = _Embedding()

        def get_stats(self):
            return {
                "semantic_available": False,
                "semantic_unavailable_reason": "EMBEDDING_MODEL_NOT_FOUND",
                "total_files": 10,
                "total_chunks": 0,
                "total_symbols": 4,
            }

    class _Memory:
        def get_embedding_status(self):
            return {
                "available": False,
                "error_code": "EMBEDDING_MODEL_NOT_FOUND",
            }

    monkeypatch.setattr(core, "get_search_engine", lambda: _SearchEngine())
    monkeypatch.setattr(core, "get_memory_manager", lambda: _Memory())
    monkeypatch.setattr(core, "get_llm_router", lambda: None)
    monkeypatch.setattr(core, "get_ast_parser", lambda: None)

    out = list_capabilities()
    by_cap = {s.capability: s.to_dict() for s in out}

    assert by_cap[Capability.SEARCH]["available"] is True
    assert by_cap[Capability.SEARCH]["state"] == "degraded"
    assert by_cap[Capability.SEARCH]["metadata"]["semantic_available"] is False
    assert "EMBEDDING_MODEL_NOT_FOUND" in by_cap[Capability.SEARCH]["reason"]
    assert by_cap[Capability.MEMORY_RECALL]["available"] is True
    assert by_cap[Capability.MEMORY_RECALL]["state"] == "degraded"
    assert by_cap[Capability.MEMORY_RECALL]["reason"] == "EMBEDDING_MODEL_NOT_FOUND"
    assert by_cap[Capability.IMPACT_ANALYSIS]["state"] == "degraded"


@pytest.mark.asyncio
async def test_composer_with_no_inputs_returns_empty_context(tmp_path, monkeypatch):
    """No file / symbol / query → composer still returns a valid context."""
    import core

    for name in (
        "get_search_engine",
        "get_memory_manager",
        "get_git_manager",
        "get_llm_router",
        "get_ast_parser",
    ):
        monkeypatch.setattr(core, name, lambda: None)

    composer = IntelligenceComposer(working_dir=str(tmp_path))
    ctx = await composer.build()

    assert isinstance(ctx, IntelligenceContext)
    assert ctx.code_understanding == {}
    assert ctx.search == {}
    assert ctx.impact == {}
    assert ctx.token_budget == 4096
    assert isinstance(ctx.elapsed_ms, int)
    assert ctx.elapsed_ms >= 0
    # Capability status must always be present so the editor can negotiate.
    assert len(ctx.capability_status) == 8


@pytest.mark.asyncio
async def test_composer_preserves_request_fields(tmp_path, monkeypatch):
    import core

    for name in (
        "get_search_engine",
        "get_memory_manager",
        "get_git_manager",
        "get_llm_router",
        "get_ast_parser",
    ):
        monkeypatch.setattr(core, name, lambda: None)

    composer = IntelligenceComposer(working_dir=str(tmp_path))
    ctx = await composer.build(
        task="add logging",
        symbol="my_func",
        token_budget=2048,
        impact_depth=3,
    )
    assert ctx.request["task"] == "add logging"
    assert ctx.request["symbol"] == "my_func"
    assert ctx.request["token_budget"] == 2048
    assert ctx.request["impact_depth"] == 3


@pytest.mark.asyncio
async def test_composer_records_errors_per_capability(tmp_path, monkeypatch):
    """Failures inside one capability must not block the others."""
    import core

    class _BoomEngine:
        async def list_symbols_in_file(self, path):
            raise RuntimeError("kaboom")

        async def search(self, req):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(core, "get_search_engine", lambda: _BoomEngine())
    monkeypatch.setattr(core, "get_memory_manager", lambda: None)
    monkeypatch.setattr(core, "get_git_manager", lambda: None)
    monkeypatch.setattr(core, "get_llm_router", lambda: None)
    monkeypatch.setattr(core, "get_ast_parser", lambda: None)

    composer = IntelligenceComposer(working_dir=str(tmp_path))
    ctx = await composer.build(file_path="x.py", query="hello")

    assert "code_understanding" in ctx.errors
    assert "search" in ctx.errors
    # Despite errors the call returned a valid context object.
    assert isinstance(ctx.token_estimate, int)


@pytest.mark.asyncio
async def test_advisories_collected_from_high_risk_signals(tmp_path, monkeypatch):
    """Composer rolls up high-risk signals into the flat advisory list."""
    import core

    monkeypatch.setattr(core, "get_search_engine", lambda: None)
    monkeypatch.setattr(core, "get_memory_manager", lambda: None)
    monkeypatch.setattr(core, "get_git_manager", lambda: None)
    monkeypatch.setattr(core, "get_llm_router", lambda: None)
    monkeypatch.setattr(core, "get_ast_parser", lambda: None)

    composer = IntelligenceComposer(working_dir=str(tmp_path))
    ctx = await composer.build()
    # Inject signals that the advisory collector should react to.
    ctx.git_history = {"risk_level": "high", "advisory": "many defensive commits"}
    ctx.impact = {"total_blast_radius": 25, "files_count": 7}
    ctx.memory = {"advisory": "you broke this in PR #12; double-check the locking"}
    composer._collect_advisories(ctx)

    joined = " ".join(ctx.advisories)
    assert "Git risk: high" in joined
    assert "may affect 25 symbols" in joined
    assert "PR #12" in joined


@pytest.mark.asyncio
async def test_compression_truncates_oversized_search_snippets(tmp_path, monkeypatch):
    """When estimated tokens exceed budget, snippets are truncated."""
    import core

    for name in (
        "get_search_engine",
        "get_memory_manager",
        "get_git_manager",
        "get_llm_router",
        "get_ast_parser",
    ):
        monkeypatch.setattr(core, name, lambda: None)

    composer = IntelligenceComposer(working_dir=str(tmp_path))
    ctx = await composer.build(token_budget=512)

    # Inject 5 huge snippets (>>budget) and re-run compression.
    big = "x" * 5000
    ctx.search = {
        "query": "anything",
        "result_count": 5,
        "results": [{"snippet": big} for _ in range(5)],
    }
    await composer._compress(ctx)

    snippets = [r["snippet"] for r in ctx.search["results"]]
    # Every snippet must be shorter after compression.
    assert all(len(s) < len(big) for s in snippets)
    assert any(s.endswith("…[truncated]") for s in snippets)
