"""Composer that aggregates the eight Intelligence Layer capabilities.

The composer is **not** a god object — every capability stays inside its
own module and the composer just calls the public surface. This keeps
the dependency graph one-way:

    composer → services → core data structures

Errors in any single capability are caught and reported back via the
``capability_status`` field instead of failing the whole call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capability surface
# ---------------------------------------------------------------------------
class Capability(str, Enum):
    """The eight capabilities listed in architecture-v2 §17."""

    CODE_UNDERSTANDING = "code_understanding"
    CONTEXT_COMPRESSION = "context_compression"
    SEARCH = "search"
    IMPACT_ANALYSIS = "impact_analysis"
    SAFE_PATCH = "safe_patch"
    MEMORY_RECALL = "memory_recall"
    DEBUG_CONSOLE = "debug_console"
    LLM_ENHANCEMENT = "llm_enhancement"

    @classmethod
    def all(cls) -> List["Capability"]:
        return list(cls)


@dataclass
class CapabilityStatus:
    """Probe result for a single capability."""

    capability: Capability
    available: bool
    detail: str = ""
    backend: str = ""
    state: str = ""
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "capability": self.capability.value,
            "available": self.available,
            "detail": self.detail,
            "backend": self.backend,
        }
        if self.state:
            data["state"] = self.state
        if self.reason:
            data["reason"] = self.reason
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


def list_capabilities() -> List[CapabilityStatus]:
    """Probe every capability against the current service singletons.

    Imports are local so this module stays cheap to import in tests
    that don't spin up the whole application lifespan.
    """
    from core import (
        get_ast_parser,
        get_llm_router,
        get_memory_manager,
        get_search_engine,
    )

    statuses: List[CapabilityStatus] = []

    # 1. Code understanding — AST parser
    parser = get_ast_parser()
    statuses.append(
        CapabilityStatus(
            Capability.CODE_UNDERSTANDING,
            available=parser is not None,
            detail="tree-sitter parsers (py/js/ts/cpp/java/go/rust)",
            backend="tree-sitter",
        )
    )

    # 2. Context compression — TokenManager (independent module)
    try:
        from omnicode.llm.token_manager import TokenManager  # noqa: F401

        statuses.append(
            CapabilityStatus(
                Capability.CONTEXT_COMPRESSION,
                available=True,
                detail="comment strip + function fold + priority-based pruner",
                backend="omnicode.llm.token_manager",
            )
        )
    except Exception as exc:
        statuses.append(
            CapabilityStatus(
                Capability.CONTEXT_COMPRESSION,
                available=False,
                detail=f"unavailable: {exc}",
            )
        )

    # 3. Search — SearchEngine (semantic + symbol + text)
    engine = get_search_engine()
    search_state = "unavailable"
    search_detail = "engine not initialised"
    search_reason = ""
    search_backend = ""
    search_meta: Dict[str, Any] = {}
    if engine:
        try:
            stats = engine.get_stats()
        except Exception:
            stats = {}
        embedding_model = getattr(engine, "embedding_model", None)
        search_backend = getattr(embedding_model, "name", "") or ""
        semantic_available = bool(
            stats.get("semantic_available")
            if "semantic_available" in stats
            else (
                getattr(engine, "semantic_available", lambda: False)()
                if callable(getattr(engine, "semantic_available", None))
                else False
            )
        )
        search_meta = {
            "semantic_available": semantic_available,
            "total_files": stats.get("total_files"),
            "total_chunks": stats.get("total_chunks"),
            "total_symbols": stats.get("total_symbols"),
            "semantic_unavailable_reason": stats.get(
                "semantic_unavailable_reason"
            ),
        }
        if semantic_available:
            search_state = "ready"
            search_detail = "semantic + deterministic symbol/text search"
        else:
            search_state = "degraded"
            search_detail = (
                "deterministic symbol/text search available; semantic "
                "embeddings are unavailable"
            )
            search_reason = (
                str(stats.get("semantic_unavailable_reason") or "")
                or "semantic embedding backend unavailable"
            )
    statuses.append(
        CapabilityStatus(
            Capability.SEARCH,
            available=engine is not None,
            detail=search_detail,
            backend=search_backend,
            state=search_state,
            reason=search_reason,
            metadata=search_meta,
        )
    )

    # 4. Impact analysis — ImpactAnalyzer (graph BFS)
    try:
        from omnicode_core.graph.impact import ImpactAnalyzer  # noqa: F401

        statuses.append(
            CapabilityStatus(
                Capability.IMPACT_ANALYSIS,
                available=True,
                detail="deterministic fallback; graph BFS when graph is available",
                backend="omnicode_core.graph.impact",
                state="degraded",
                reason=(
                    "graph index is not persisted by default; impact should "
                    "return low-confidence fallback unless graph evidence exists"
                ),
                metadata={"graph_index_ready": False},
            )
        )
    except Exception as exc:
        statuses.append(
            CapabilityStatus(
                Capability.IMPACT_ANALYSIS,
                available=False,
                detail=f"unavailable: {exc}",
            )
        )

    # 5. Safe patch — PatchManager (preview/validate/apply/rollback)
    try:
        from omnicode_core.edit.patch import PatchManager  # noqa: F401

        statuses.append(
            CapabilityStatus(
                Capability.SAFE_PATCH,
                available=True,
                detail="preview → validate → apply → rollback",
                backend="omnicode_core.edit.patch",
            )
        )
    except Exception as exc:
        statuses.append(
            CapabilityStatus(
                Capability.SAFE_PATCH,
                available=False,
                detail=f"unavailable: {exc}",
            )
        )

    # 6. Memory recall — MemoryAdvisor wraps MemoryManager
    mem = get_memory_manager()
    memory_state = "ready" if mem is not None else "unavailable"
    memory_detail = "MemoryAdvisor + multi-angle search"
    memory_reason = ""
    memory_meta: Dict[str, Any] = {}
    if mem is not None:
        status_fn = getattr(mem, "get_embedding_status", None)
        if callable(status_fn):
            try:
                memory_meta["embedding"] = status_fn()
            except Exception as exc:
                memory_meta["embedding"] = {
                    "available": False,
                    "error": str(exc),
                }
        embedding = memory_meta.get("embedding") or {}
        if embedding and not bool(embedding.get("available")):
            memory_state = "degraded"
            memory_detail = (
                "lexical/tag memory recall; semantic memory vectors unavailable"
            )
            memory_reason = (
                str(embedding.get("error_code") or "")
                or "memory embedding backend unavailable"
            )
    statuses.append(
        CapabilityStatus(
            Capability.MEMORY_RECALL,
            available=mem is not None,
            detail=memory_detail,
            backend="omnicode_core.memory.advisory",
            state=memory_state,
            reason=memory_reason,
            metadata=memory_meta,
        )
    )

    # 7. Debug console — always present (FastAPI itself)
    statuses.append(
        CapabilityStatus(
            Capability.DEBUG_CONSOLE,
            available=True,
            detail="REST + WebSocket + /capabilities + /health",
            backend="fastapi",
        )
    )

    # 8. LLM enhancement — LLMRouter
    router = get_llm_router()
    statuses.append(
        CapabilityStatus(
            Capability.LLM_ENHANCEMENT,
            available=router is not None,
            detail="LLMRouter (LiteLLM-backed, multi-provider, optional)",
            backend="omnicode.llm.router",
        )
    )

    return statuses


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------
@dataclass
class IntelligenceContext:
    """Aggregated payload returned to AI editors."""

    request: Dict[str, Any]
    capability_status: List[Dict[str, Any]]
    code_understanding: Dict[str, Any] = field(default_factory=dict)
    search: Dict[str, Any] = field(default_factory=dict)
    impact: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    git_history: Dict[str, Any] = field(default_factory=dict)
    advisories: List[str] = field(default_factory=list)
    token_estimate: int = 0
    token_budget: int = 0
    elapsed_ms: int = 0
    errors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "request": self.request,
            "capability_status": self.capability_status,
            "code_understanding": self.code_understanding,
            "search": self.search,
            "impact": self.impact,
            "memory": self.memory,
            "git_history": self.git_history,
            "advisories": self.advisories,
            "token_estimate": self.token_estimate,
            "token_budget": self.token_budget,
            "elapsed_ms": self.elapsed_ms,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------
class IntelligenceComposer:
    """Single-call orchestrator for the eight capabilities."""

    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir

    async def build(
        self,
        *,
        task: Optional[str] = None,
        file_path: Optional[str] = None,
        symbol: Optional[str] = None,
        query: Optional[str] = None,
        max_search_results: int = 5,
        impact_depth: int = 2,
        memory_max: int = 5,
        token_budget: int = 4096,
        include_git_history: bool = True,
        include_impact: bool = True,
        include_memory: bool = True,
    ) -> IntelligenceContext:
        """Run as many capabilities as the inputs allow and combine results.

        Each capability is best-effort: a failure is recorded in
        ``errors[<capability>]`` and the rest of the pipeline continues.
        """
        started = time.monotonic()

        ctx = IntelligenceContext(
            request={
                "task": task,
                "file_path": file_path,
                "symbol": symbol,
                "query": query,
                "max_search_results": max_search_results,
                "impact_depth": impact_depth,
                "memory_max": memory_max,
                "token_budget": token_budget,
            },
            capability_status=[s.to_dict() for s in list_capabilities()],
            token_budget=token_budget,
        )

        # ---- 1. Code understanding (only when a file is supplied) -----------
        if file_path:
            await self._run_code_understanding(ctx, file_path)

        # ---- 3. Search (any time we have a query OR a symbol) ---------------
        if query or symbol:
            await self._run_search(
                ctx, query=query or symbol, max_results=max_search_results
            )

        # ---- 4. Impact analysis (when symbol is named) ----------------------
        if include_impact and symbol:
            await self._run_impact(ctx, symbol=symbol, depth=impact_depth)

        # ---- 6. Memory recall ----------------------------------------------
        if include_memory:
            await self._run_memory(
                ctx,
                file_path=file_path,
                symbol=symbol,
                task=task,
                max_memories=memory_max,
            )

        # ---- Bonus: git history (lightweight) ------------------------------
        if include_git_history and file_path:
            await self._run_git_history(ctx, file_path=file_path)

        # ---- 2. Context compression / token budget enforcement -------------
        await self._compress(ctx)

        # ---- Advisory roll-up ----------------------------------------------
        self._collect_advisories(ctx)

        ctx.elapsed_ms = int((time.monotonic() - started) * 1000)
        return ctx

    # ----------------------------------------------------- capability runners
    async def _run_code_understanding(self, ctx: IntelligenceContext, file_path: str):
        try:
            from core import get_search_engine

            engine = get_search_engine()
            if engine is None:
                ctx.errors["code_understanding"] = "search engine not initialised"
                return
            data = await engine.list_symbols_in_file(file_path)
            symbols = data.get("symbols") or []
            ctx.code_understanding = {
                "file": file_path,
                "language": data.get("language", ""),
                "symbol_count": len(symbols),
                "symbols": [
                    {
                        "name": s.get("name"),
                        "kind": s.get("type"),
                        "start_line": s.get("start_line"),
                        "end_line": s.get("end_line"),
                        "parent": s.get("parent"),
                    }
                    for s in symbols[:50]  # cap to keep payload sane
                ],
            }
        except Exception as exc:
            logger.warning("composer.code_understanding failed: %s", exc)
            ctx.errors["code_understanding"] = str(exc)

    async def _run_search(self, ctx: IntelligenceContext, query: str, max_results: int):
        try:
            from core import get_search_engine
            from omnicode.search.models import SearchRequest

            engine = get_search_engine()
            if engine is None:
                ctx.errors["search"] = "search engine not initialised"
                return
            req = SearchRequest(
                query=query,
                search_type="semantic",
                max_results=max_results,
            )
            results = await engine.search(req)
            ctx.search = {
                "query": query,
                "result_count": len(results),
                "results": [
                    {
                        "file": getattr(r, "file_path", None) or getattr(r, "file", None),
                        "score": getattr(r, "score", None),
                        "snippet": (getattr(r, "content", "") or "")[:400],
                        "start_line": getattr(r, "start_line", None),
                        "end_line": getattr(r, "end_line", None),
                    }
                    for r in results
                ],
            }
        except Exception as exc:
            logger.warning("composer.search failed: %s", exc)
            ctx.errors["search"] = str(exc)

    async def _run_impact(self, ctx: IntelligenceContext, symbol: str, depth: int):
        try:
            from omnicode_core.graph.impact import ImpactAnalyzer

            analyser = ImpactAnalyzer(self.working_dir)
            ctx.impact = await analyser.get_impact_radius(symbol=symbol, depth=depth)
        except Exception as exc:
            logger.warning("composer.impact failed: %s", exc)
            ctx.errors["impact"] = str(exc)

    async def _run_memory(
        self,
        ctx: IntelligenceContext,
        file_path: Optional[str],
        symbol: Optional[str],
        task: Optional[str],
        max_memories: int,
    ):
        try:
            from core import get_memory_manager
            from omnicode_core.memory.advisory import MemoryAdvisor

            mm = get_memory_manager()
            if mm is None:
                ctx.errors["memory"] = "memory manager not initialised"
                return
            advisor = MemoryAdvisor(mm)
            ctx.memory = await advisor.generate_advisory(
                file_path=file_path,
                symbol=symbol,
                task=task,
                max_memories=max_memories,
            )
        except Exception as exc:
            logger.warning("composer.memory failed: %s", exc)
            ctx.errors["memory"] = str(exc)

    async def _run_git_history(self, ctx: IntelligenceContext, file_path: str):
        try:
            from omnicode.git_context.history import GitHistoryAnalyzer

            analyser = GitHistoryAnalyzer(self.working_dir, max_commits_scanned=30)
            report = analyser.analyze_file(file_path)
            ctx.git_history = {
                "file": report.file_path,
                "total_commits": report.total_commits,
                "risk_score": report.risk_score,
                "risk_level": report.risk_level,
                "advisory": report.advisory,
                "co_changed_files": report.co_changed_files[:5],
            }
        except Exception as exc:
            logger.warning("composer.git_history failed: %s", exc)
            ctx.errors["git_history"] = str(exc)

    # ----------------------------------------------------- compression / advisory
    async def _compress(self, ctx: IntelligenceContext):
        """Estimate token usage and shrink the search snippets if we're over."""
        try:
            from omnicode.llm.token_manager import ContextPruner

            # We use a synthetic provider here because the pruner only needs
            # ``count_tokens`` to size strings — never to call the LLM.
            class _NullProvider:
                def count_tokens(self, text: str) -> int:
                    # ~4 chars/token heuristic; matches token_manager fallback.
                    return max(1, len(text) // 4)

            pruner = ContextPruner(_NullProvider())  # type: ignore[arg-type]

            # Build flat list of strings to estimate.
            blobs: List[str] = []
            if ctx.code_understanding:
                blobs.append(str(ctx.code_understanding))
            if ctx.search:
                blobs.extend(r.get("snippet", "") for r in ctx.search.get("results", []))
            if ctx.impact:
                blobs.append(str(ctx.impact))
            if ctx.memory:
                blobs.append(ctx.memory.get("advisory", "") or "")
            if ctx.git_history:
                blobs.append(ctx.git_history.get("advisory", "") or "")

            ctx.token_estimate = sum(pruner.count_tokens(b) for b in blobs)

            # If we're way over budget, trim the search snippets first
            # (they're the biggest blobs and easiest to truncate without
            # losing structural information).
            if ctx.token_estimate > ctx.token_budget and ctx.search:
                shrink_to = max(120, ctx.token_budget * 4 // max(1, len(ctx.search.get("results", []))))
                for r in ctx.search.get("results", []):
                    s = r.get("snippet") or ""
                    if len(s) > shrink_to:
                        r["snippet"] = s[:shrink_to] + "…[truncated]"
                ctx.token_estimate = sum(pruner.count_tokens(b) for b in blobs[:1]) + sum(
                    pruner.count_tokens(r.get("snippet", "")) for r in ctx.search.get("results", [])
                )
        except Exception as exc:
            logger.warning("composer._compress failed: %s", exc)
            ctx.errors["compression"] = str(exc)

    def _collect_advisories(self, ctx: IntelligenceContext):
        """Roll up notable items into a flat advisory list."""
        if ctx.git_history.get("risk_level") in ("medium", "high"):
            ctx.advisories.append(
                f"⚠️ Git risk: {ctx.git_history['risk_level']} — {ctx.git_history.get('advisory', '')}"
            )
        if ctx.impact.get("total_blast_radius", 0) >= 10:
            ctx.advisories.append(
                f"⚠️ Impact: changing this symbol may affect "
                f"{ctx.impact['total_blast_radius']} symbols across "
                f"{ctx.impact.get('files_count', 0)} files."
            )
        if ctx.memory.get("advisory"):
            ctx.advisories.append(ctx.memory["advisory"])
        if ctx.token_estimate > ctx.token_budget:
            ctx.advisories.append(
                f"ℹ️ Estimated context ({ctx.token_estimate} tok) exceeds budget "
                f"({ctx.token_budget} tok). Search snippets were truncated."
            )


__all__ = [
    "Capability",
    "CapabilityStatus",
    "IntelligenceContext",
    "IntelligenceComposer",
    "list_capabilities",
]
