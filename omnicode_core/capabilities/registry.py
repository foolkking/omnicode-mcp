"""Runtime capability registry for MCP-facing contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CapabilityState = Literal[
    "ready",
    "partial",
    "degraded",
    "unavailable",
    "unsupported",
]


@dataclass(frozen=True)
class Capability:
    name: str
    state: CapabilityState
    provider: str
    reason: str = ""
    fallbacks: list[str] = field(default_factory=list)
    safe_to_use_by_default: bool = False
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "provider": self.provider,
            "reason": self.reason,
            "fallbacks": list(self.fallbacks),
            "safe_to_use_by_default": self.safe_to_use_by_default,
            "next_actions": list(self.next_actions),
        }


def build_runtime_capabilities(
    *,
    cloud_available: bool,
    local_index_ready: bool,
    line_fts_available: bool,
    embedding_available: bool,
    semantic_index_ready: bool = False,
    graph_index_ready: bool = False,
) -> dict[str, dict[str, Any]]:
    text_ready = bool(local_index_ready or line_fts_available)
    return {
        "read.full": Capability(
            "read.full",
            "ready",
            "local_fs",
            safe_to_use_by_default=True,
        ).to_dict(),
        "read.range": Capability(
            "read.range",
            "ready",
            "local_fs",
            safe_to_use_by_default=True,
        ).to_dict(),
        "read.outline": Capability(
            "read.outline",
            "ready",
            "local_parser",
            safe_to_use_by_default=True,
        ).to_dict(),
        "read.symbol": Capability(
            "read.symbol",
            "partial" if local_index_ready else "degraded",
            "local_parser",
            reason="" if local_index_ready else "local index may be missing; parser fallback only",
            fallbacks=["range"],
            safe_to_use_by_default=True,
        ).to_dict(),
        "search.symbol_exact": Capability(
            "search.symbol_exact",
            "ready" if local_index_ready else "degraded",
            "sqlite_exact_index" if local_index_ready else "parser_scan",
            reason="" if local_index_ready else "run omni_index(scope='workspace') for deterministic symbol search",
            fallbacks=["parser_scan"],
            safe_to_use_by_default=local_index_ready,
            next_actions=[] if local_index_ready else [
                "omni_index(action='bootstrap', scope='workspace', background=False, format='json')"
            ],
        ).to_dict(),
        "search.text_exact": Capability(
            "search.text_exact",
            "ready" if text_ready else "degraded",
            "sqlite_fts" if line_fts_available else "grep_fallback",
            reason="" if text_ready else "line FTS unavailable; grep fallback may be used",
            fallbacks=["workspace_grep", "cloud_snapshot_grep"],
            safe_to_use_by_default=True,
        ).to_dict(),
        "search.regex": Capability(
            "search.regex",
            "ready",
            "grep_fallback",
            fallbacks=["python_grep"],
            safe_to_use_by_default=True,
        ).to_dict(),
        "search.references": Capability(
            "search.references",
            "degraded",
            "text_refs",
            reason=(
                "LSP/indexed reference graph may be unavailable; "
                "references use deterministic text/symbol fallback"
            ),
            fallbacks=["search.text_exact", "search.symbol_exact"],
            safe_to_use_by_default=False,
        ).to_dict(),
        "search.semantic": Capability(
            "search.semantic",
            "ready" if semantic_index_ready and embedding_available else "unavailable",
            "faiss",
            reason="" if semantic_index_ready and embedding_available else "semantic index or embedding model unavailable",
            safe_to_use_by_default=False,
            next_actions=[
                "omnicode models status",
                "omni_index(action='bootstrap', scope='semantic', background=True, format='json')",
            ],
        ).to_dict(),
        "impact.graph": Capability(
            "impact.graph",
            "ready" if graph_index_ready else "degraded",
            "graph_index",
            reason="" if graph_index_ready else "graph index unavailable; impact should return deterministic fallback only",
            fallbacks=["text_references", "test_candidates"],
            safe_to_use_by_default=False,
        ).to_dict(),
        "context.deterministic": Capability(
            "context.deterministic",
            "ready",
            "local_parser+exact_search",
            fallbacks=["file_outline", "nearby_range"],
            safe_to_use_by_default=True,
        ).to_dict(),
        "diagnostics.python": Capability(
            "diagnostics.python",
            "partial",
            "guard",
            reason="project type environment may be incomplete",
            safe_to_use_by_default=True,
        ).to_dict(),
        "diagnostics.java": Capability(
            "diagnostics.java",
            "partial",
            "optional_project_tooling",
            reason="requires project-native Java tooling",
            safe_to_use_by_default=False,
        ).to_dict(),
        "diagnostics.scala": Capability(
            "diagnostics.scala",
            "unsupported",
            "none",
            reason="Scala diagnostics are not implemented",
            safe_to_use_by_default=False,
        ).to_dict(),
        "patch.safe_edit": Capability(
            "patch.safe_edit",
            "ready",
            "local_patch_manager",
            safe_to_use_by_default=True,
        ).to_dict(),
        "sync.cloud": Capability(
            "sync.cloud",
            "ready" if cloud_available else "unavailable",
            "http_sync",
            reason="" if cloud_available else "cloud backend unavailable",
            safe_to_use_by_default=cloud_available,
        ).to_dict(),
        "embedding.local": Capability(
            "embedding.local",
            "ready" if embedding_available else "unavailable",
            "sentence_transformers",
            reason="" if embedding_available else "embedding model unavailable or not cached",
            safe_to_use_by_default=False,
        ).to_dict(),
    }


__all__ = ["Capability", "build_runtime_capabilities"]
