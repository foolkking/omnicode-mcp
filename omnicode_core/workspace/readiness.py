"""Shared index readiness contract for hybrid workspace analysis.

The cloud backend has several independent readiness layers:

* snapshot/object store accepted the latest files
* exact SQLite index can answer path/text/symbol lookups
* semantic/vector index can enrich search/context
* graph/code-intel index can answer impact precisely

Keeping the classification in one place prevents HTTP status, freshness
checks, and MCP status from drifting into different meanings.
"""

from __future__ import annotations

from typing import Any, Iterable


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_modes(modes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for mode in modes:
        if mode and mode not in seen:
            seen.add(mode)
            ordered.append(mode)
    return ordered


_SEMANTIC_READY_COVERAGES = frozenset(
    {
        "semantic_full",
        "selected_files",
        "filtered",
        "partial_after_exact_only",
    }
)


def build_index_readiness_contract(
    *,
    workspace_id: str,
    accepted_revision: Any = 0,
    semantic_indexed_revision: Any = 0,
    exact_indexed_revision: Any = 0,
    snapshot_files: Any = 0,
    snapshot_deletes: Any = 0,
    exact_files: Any = 0,
    exact_symbols: Any = 0,
    exact_lines: Any = 0,
    exact_line_fts_available: bool = False,
    semantic_index_coverage: str = "unknown",
    semantic_initial_exact_only: bool = False,
    index_worker_busy: bool = False,
    last_index_error: Any = None,
    graph_index_ready: bool = False,
) -> dict[str, Any]:
    """Return a stable, AI-editor-oriented readiness contract."""

    accepted = _as_int(accepted_revision)
    semantic_indexed = _as_int(semantic_indexed_revision)
    exact_indexed = _as_int(exact_indexed_revision)
    files = _as_int(snapshot_files)
    deletes = _as_int(snapshot_deletes)
    exact_file_count = _as_int(exact_files)
    exact_symbol_count = _as_int(exact_symbols)
    exact_line_count = _as_int(exact_lines)
    semantic_coverage = (semantic_index_coverage or "unknown").strip() or "unknown"

    snapshot_ready = files > 0 or accepted > 0 or deletes > 0
    exact_pending = max(accepted - exact_indexed, 0)
    semantic_pending = max(accepted - semantic_indexed, 0)
    exact_ready = bool(
        snapshot_ready
        and exact_file_count > 0
        and exact_indexed >= accepted
    )
    semantic_ready = bool(
        snapshot_ready
        and semantic_indexed >= accepted
        and not index_worker_busy
        and not last_index_error
        and not semantic_initial_exact_only
        and semantic_coverage in _SEMANTIC_READY_COVERAGES
    )

    if not snapshot_ready:
        reason = "empty_workspace"
    elif not exact_ready:
        reason = "exact_index_catching_up"
    elif semantic_ready:
        reason = semantic_coverage
    elif semantic_initial_exact_only:
        reason = "exact_only_initial_sync"
    elif last_index_error:
        reason = "index_error"
    elif index_worker_busy or semantic_pending > 0:
        reason = "semantic_index_catching_up"
    elif semantic_coverage == "filtered_empty":
        reason = "semantic_filtered_empty"
    elif semantic_coverage in {"deletes_only", "unchanged", "unknown"}:
        reason = f"semantic_{semantic_coverage}"
    else:
        reason = "semantic_not_ready"

    if semantic_ready:
        recommended_query_mode = "semantic_first"
    elif exact_ready:
        recommended_query_mode = "exact_first"
    elif snapshot_ready:
        recommended_query_mode = "snapshot_only"
    else:
        recommended_query_mode = "local_only"

    supported_modes = ["local"]
    if snapshot_ready:
        supported_modes.append("snapshot")
    if exact_ready:
        supported_modes.extend(["exact_text", "exact_symbol"])
    if semantic_ready:
        supported_modes.append("semantic")
    if graph_index_ready:
        supported_modes.append("graph")

    semantic_degraded = bool(
        snapshot_ready and not semantic_ready and (
            bool(last_index_error)
            or bool(semantic_initial_exact_only)
            or bool(index_worker_busy)
            or semantic_pending > 0
            or exact_ready
        )
    )

    return {
        "schema_version": "index_readiness.v1",
        "workspace_id": workspace_id,
        "snapshot_ready": snapshot_ready,
        "exact_index_ready": exact_ready,
        "semantic_index_ready": semantic_ready,
        "graph_index_ready": bool(graph_index_ready),
        "search_degraded": semantic_degraded,
        "recommended_query_mode": recommended_query_mode,
        "query_mode_reason": reason,
        "supported_query_modes": _compact_modes(supported_modes),
        "exact_query_safe": exact_ready,
        "strict_semantic_safe": semantic_ready,
        "semantic_query_safe": semantic_ready,
        "snapshot": {
            "ready": snapshot_ready,
            "accepted_revision": accepted,
            "files": files,
            "deletes": deletes,
        },
        "exact": {
            "ready": exact_ready,
            "indexed_revision": exact_indexed,
            "pending_revisions": exact_pending,
            "files": exact_file_count,
            "symbols": exact_symbol_count,
            "lines": exact_line_count,
            "line_fts_available": bool(exact_line_fts_available),
        },
        "semantic": {
            "ready": semantic_ready,
            "indexed_revision": semantic_indexed,
            "pending_revisions": semantic_pending,
            "coverage": semantic_coverage,
            "initial_exact_only": bool(semantic_initial_exact_only),
            "worker_busy": bool(index_worker_busy),
            "last_error": str(last_index_error) if last_index_error else None,
        },
        "graph": {
            "ready": bool(graph_index_ready),
            "reason": None
            if graph_index_ready
            else "persistent graph index is not ready; impact may use fallback analysis.",
        },
    }


def contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    """Return the flat fields commonly surfaced by status endpoints."""

    semantic = contract.get("semantic") or {}
    exact = contract.get("exact") or {}
    return {
        "snapshot_ready": bool(contract.get("snapshot_ready", False)),
        "exact_index_ready": bool(contract.get("exact_index_ready", False)),
        "semantic_index_ready": bool(contract.get("semantic_index_ready", False)),
        "graph_index_ready": bool(contract.get("graph_index_ready", False)),
        "search_degraded": bool(contract.get("search_degraded", False)),
        "recommended_query_mode": contract.get("recommended_query_mode"),
        "query_mode_reason": contract.get("query_mode_reason"),
        "supported_query_modes": list(contract.get("supported_query_modes") or []),
        "exact_query_safe": bool(contract.get("exact_query_safe", False)),
        "strict_semantic_safe": bool(contract.get("strict_semantic_safe", False)),
        "semantic_query_safe": bool(contract.get("semantic_query_safe", False)),
        "semantic_pending_revisions": _as_int(semantic.get("pending_revisions")),
        "exact_pending_revisions": _as_int(exact.get("pending_revisions")),
    }
