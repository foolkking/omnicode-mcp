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
    toolchain_status: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    text_ready = bool(local_index_ready or line_fts_available)
    toolchain_status = toolchain_status if isinstance(toolchain_status, dict) else {}

    def _tool_available(name: str) -> bool:
        tools = toolchain_status.get("tools")
        row = tools.get(name) if isinstance(tools, dict) else None
        return bool(row.get("available")) if isinstance(row, dict) else False

    def _build_file_present(name: str) -> bool:
        build_files = toolchain_status.get("build_files")
        return bool(build_files.get(name)) if isinstance(build_files, dict) else False

    def _section(name: str) -> dict[str, Any]:
        row = toolchain_status.get(name)
        return row if isinstance(row, dict) else {}

    java_toolchain = _section("java")
    scala_toolchain = _section("scala")
    java_workspace_ready = bool(
        java_toolchain.get("workspace_diagnostics_ready")
    )
    scala_workspace_ready = bool(
        scala_toolchain.get("workspace_diagnostics_ready")
    )
    java_toolchain_ready = bool(java_toolchain.get("toolchain_ready"))
    scala_toolchain_ready = bool(scala_toolchain.get("toolchain_ready"))
    java_reason = str(java_toolchain.get("reason") or "jdtls_unavailable")
    scala_reason = str(scala_toolchain.get("reason") or "metals_unavailable")
    java_workspace_next_actions = (
        [
            "omni_index(action='bootstrap', scope='lsp', background=False, format='json')"
        ]
        if java_toolchain_ready and not java_workspace_ready
        else (
            []
            if java_workspace_ready
            else [
                "Install/configure JDT LS and Maven or Gradle, then rerun omni_status()."
            ]
        )
    )
    scala_workspace_next_actions = (
        [
            "omni_index(action='bootstrap', scope='lsp', background=False, format='json')"
        ]
        if scala_toolchain_ready and not scala_workspace_ready
        else (
            []
            if scala_workspace_ready
            else [
                "Install/configure Metals with sbt, Bloop, or Gradle, then rerun omni_status()."
            ]
        )
    )

    java_diag = (
        Capability(
            "diagnostics.java",
            "ready",
            "jdtls",
            safe_to_use_by_default=True,
        )
        if java_workspace_ready
        else Capability(
            "diagnostics.java",
            "partial",
            "tree_sitter_java+javac",
            reason=(
                "workspace diagnostics unavailable; syntax/single-file "
                "checks only"
            ),
            fallbacks=["tree_sitter_java", "javac"],
            safe_to_use_by_default=False,
        )
    )
    scala_diag = (
        Capability(
            "diagnostics.scala",
            "ready",
            "metals",
            safe_to_use_by_default=True,
        )
        if scala_workspace_ready
        else Capability(
            "diagnostics.scala",
            "unsupported",
            "none",
            reason=(
                "Scala workspace diagnostics unavailable; Metals/sbt/Bloop "
                "or Gradle toolchain is required"
            ),
            safe_to_use_by_default=False,
        )
    )

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
            java_diag.name,
            java_diag.state,
            java_diag.provider,
            reason=java_diag.reason,
            fallbacks=list(java_diag.fallbacks),
            safe_to_use_by_default=java_diag.safe_to_use_by_default,
            next_actions=list(java_diag.next_actions),
        ).to_dict(),
        "diagnostics.java.workspace": Capability(
            "diagnostics.java.workspace",
            "ready" if java_workspace_ready else "unavailable",
            "jdtls",
            reason="" if java_workspace_ready else java_reason,
            fallbacks=["javac", "tree_sitter_java"],
            safe_to_use_by_default=java_workspace_ready,
            next_actions=java_workspace_next_actions,
        ).to_dict(),
        "diagnostics.scala": Capability(
            scala_diag.name,
            scala_diag.state,
            scala_diag.provider,
            reason=scala_diag.reason,
            fallbacks=list(scala_diag.fallbacks),
            safe_to_use_by_default=scala_diag.safe_to_use_by_default,
            next_actions=list(scala_diag.next_actions),
        ).to_dict(),
        "diagnostics.scala.workspace": Capability(
            "diagnostics.scala.workspace",
            "ready" if scala_workspace_ready else "unavailable",
            "metals",
            reason="" if scala_workspace_ready else scala_reason,
            fallbacks=["sbt", "bloop", "gradle"],
            safe_to_use_by_default=scala_workspace_ready,
            next_actions=scala_workspace_next_actions,
        ).to_dict(),
        "lsp.jdtls": Capability(
            "lsp.jdtls",
            (
                "ready"
                if java_workspace_ready
                else "partial"
                if java_toolchain_ready
                else "unavailable"
            ),
            "path",
            reason="" if java_workspace_ready else java_reason,
            safe_to_use_by_default=False,
            next_actions=java_workspace_next_actions,
        ).to_dict(),
        "lsp.metals": Capability(
            "lsp.metals",
            (
                "ready"
                if scala_workspace_ready
                else "partial"
                if scala_toolchain_ready
                else "unavailable"
            ),
            "path",
            reason="" if scala_workspace_ready else scala_reason,
            safe_to_use_by_default=False,
            next_actions=scala_workspace_next_actions,
        ).to_dict(),
        "build.maven": Capability(
            "build.maven",
            "ready" if _tool_available("mvn") else "unavailable",
            "path",
            reason="" if _tool_available("mvn") else "mvn_unavailable",
            safe_to_use_by_default=False,
        ).to_dict(),
        "build.gradle": Capability(
            "build.gradle",
            "ready" if (_tool_available("gradle") or _build_file_present("gradle_wrapper")) else "unavailable",
            "path_or_wrapper",
            reason="" if (_tool_available("gradle") or _build_file_present("gradle_wrapper")) else "gradle_unavailable",
            safe_to_use_by_default=False,
        ).to_dict(),
        "build.sbt": Capability(
            "build.sbt",
            "ready" if _tool_available("sbt") else "unavailable",
            "path",
            reason="" if _tool_available("sbt") else "sbt_unavailable",
            safe_to_use_by_default=False,
        ).to_dict(),
        "build.bloop": Capability(
            "build.bloop",
            "ready" if _tool_available("bloop") else "unavailable",
            "path",
            reason="" if _tool_available("bloop") else "bloop_unavailable",
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
