"""Freshness checks shared by cloud analysis routes."""

from __future__ import annotations

from typing import Any, Optional

from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.graph_index import WorkspaceGraphIndex
from omnicode_core.workspace.readiness import build_index_readiness_contract
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def cloud_freshness_state(
    *,
    workspace_id: Optional[str],
    min_revision: Optional[int],
    include_exact: bool = True,
    include_graph: bool = True,
) -> Optional[dict[str, Any]]:
    """Return cloud revision state for a hybrid freshness requirement.

    The check is opt-in: legacy/direct clients that do not send
    X-Omnicode-Min-Revision keep the existing behavior. Hybrid MCP clients send
    the header when they require cloud analysis to be at least as fresh as the
    local revision.
    """
    if min_revision is None:
        return None
    if min_revision <= 0:
        return None
    if not workspace_id or not workspace_id.strip():
        return {
            "freshness": "unknown",
            "error": "X-Omnicode-Workspace is required for freshness checks",
            "required_revision": min_revision,
        }

    status = CloudSnapshotStore().status(workspace_id.strip())
    accepted_revision = int(status.get("accepted_revision", 0))
    indexed_revision = int(status.get("indexed_revision", 0))
    semantic_initial_exact_only = bool(
        status.get("semantic_initial_exact_only", False)
    )
    semantic_index_coverage = str(
        status.get("semantic_index_coverage") or "unknown"
    )
    if include_exact:
        try:
            exact_index = SnapshotExactIndex()
            if hasattr(exact_index, "try_status"):
                exact_status = exact_index.try_status(
                    workspace_id=workspace_id.strip(),
                    lock_timeout_ms=75,
                )
            else:
                exact_status = exact_index.status(
                    workspace_id=workspace_id.strip()
                )
        except Exception:
            exact_status = {}
    else:
        exact_status = {}
    if include_graph:
        try:
            graph_index = WorkspaceGraphIndex(store=CloudSnapshotStore())
            readiness_probe = getattr(graph_index, "try_readiness", None)
            if callable(readiness_probe):
                graph_status = readiness_probe(
                    workspace_id=workspace_id.strip(),
                    accepted_revision=accepted_revision,
                    lock_timeout_ms=75,
                )
            elif hasattr(graph_index, "try_status"):
                graph_status = graph_index.try_status(
                    workspace_id=workspace_id.strip(),
                    accepted_revision=accepted_revision,
                    lock_timeout_ms=75,
                )
            else:
                graph_status = graph_index.status(
                    workspace_id=workspace_id.strip(),
                    accepted_revision=accepted_revision,
                )
        except Exception:
            graph_status = {}
    else:
        graph_status = {}
    exact_indexed_revision = int(exact_status.get("exact_indexed_revision") or 0)
    graph_indexed_revision = int(graph_status.get("graph_indexed_revision") or 0)
    required_revision = max(min_revision, accepted_revision)
    snapshot_required_revision = min_revision
    semantic_fresh = (
        indexed_revision >= required_revision and not semantic_initial_exact_only
    )
    exact_fresh = exact_indexed_revision >= required_revision
    graph_fresh = bool(
        graph_status.get("ready")
        and graph_indexed_revision >= required_revision
    )
    snapshot_fresh = accepted_revision >= snapshot_required_revision
    freshness = (
        "fresh"
        if semantic_fresh
        else "exact_fresh"
        if exact_fresh
        else "snapshot_fresh"
        if snapshot_fresh
        else "stale"
    )
    readiness_contract = build_index_readiness_contract(
        workspace_id=workspace_id.strip(),
        accepted_revision=accepted_revision,
        semantic_indexed_revision=indexed_revision,
        exact_indexed_revision=exact_indexed_revision,
        snapshot_files=status.get("file_count") or 0,
        snapshot_deletes=status.get("delete_count") or 0,
        exact_files=(
            exact_status.get("files")
            or (1 if exact_indexed_revision >= required_revision else 0)
        ),
        exact_symbols=exact_status.get("symbols") or 0,
        exact_lines=exact_status.get("lines") or 0,
        exact_line_fts_available=bool(
            exact_status.get("line_fts_available", False)
        ),
        semantic_index_coverage=semantic_index_coverage,
        semantic_initial_exact_only=semantic_initial_exact_only,
        graph_index_ready=graph_fresh,
    )
    return {
        "workspace_id": workspace_id.strip(),
        "accepted_revision": accepted_revision,
        "indexed_revision": indexed_revision,
        "exact_indexed_revision": exact_indexed_revision,
        "graph_indexed_revision": graph_indexed_revision,
        "required_revision": required_revision,
        "snapshot_required_revision": snapshot_required_revision,
        "semantic_fresh": semantic_fresh,
        "semantic_index_coverage": semantic_index_coverage,
        "semantic_initial_exact_only": semantic_initial_exact_only,
        "exact_fresh": exact_fresh,
        "graph_fresh": graph_fresh,
        "snapshot_fresh": snapshot_fresh,
        "semantic_stale": not semantic_fresh,
        "exact_stale": not exact_fresh,
        "freshness": freshness,
        "recommended_query_mode": readiness_contract["recommended_query_mode"],
        "query_mode_reason": readiness_contract["query_mode_reason"],
        "supported_query_modes": readiness_contract["supported_query_modes"],
        "strict_semantic_safe": readiness_contract["strict_semantic_safe"],
        "exact_query_safe": readiness_contract["exact_query_safe"],
    }


def cloud_freshness_error(
    *,
    workspace_id: Optional[str],
    min_revision: Optional[int],
    allow_snapshot_fresh: bool = False,
    allow_exact_fresh: bool = False,
    allow_graph_fresh: bool = False,
) -> Optional[dict[str, Any]]:
    """Return a structured stale-index error when a min revision is unmet.

    ``allow_snapshot_fresh`` is for endpoints that answer directly from the
    object-store snapshot (for example exact symbol/text bootstrap). Those
    routes may proceed when the snapshot has accepted the requested revision
    even if the semantic/vector index is still catching up.
    """
    if allow_snapshot_fresh and min_revision and min_revision > 0 and workspace_id:
        try:
            status = CloudSnapshotStore().status(workspace_id.strip())
            if int(status.get("accepted_revision", 0)) >= int(min_revision):
                return None
        except Exception:
            pass

    state = cloud_freshness_state(
        workspace_id=workspace_id,
        min_revision=min_revision,
    )
    if state is None:
        return None
    if "error" in state:
        return {
            "ok": False,
            "success": False,
            "stale": True,
            **state,
        }
    if state["semantic_fresh"]:
        return None
    if allow_exact_fresh and state.get("exact_fresh"):
        return None
    if allow_graph_fresh and state.get("graph_fresh"):
        return None
    if allow_snapshot_fresh and state["snapshot_fresh"]:
        return None

    exact_fresh = bool(state.get("exact_fresh"))
    return {
        "ok": False,
        "success": False,
        "stale": True,
        "freshness": state.get("freshness", "stale"),
        "error": (
            "Cloud semantic index is stale"
            if exact_fresh
            else "Cloud index is stale"
        ),
        "workspace_id": state["workspace_id"],
        "accepted_revision": state["accepted_revision"],
        "indexed_revision": state["indexed_revision"],
        "exact_indexed_revision": state.get("exact_indexed_revision", 0),
        "graph_indexed_revision": state.get("graph_indexed_revision", 0),
        "required_revision": state["required_revision"],
        "semantic_index_coverage": state.get("semantic_index_coverage", "unknown"),
        "semantic_initial_exact_only": bool(
            state.get("semantic_initial_exact_only", False)
        ),
        "recommended_query_mode": state.get("recommended_query_mode"),
        "query_mode_reason": state.get("query_mode_reason"),
        "supported_query_modes": state.get("supported_query_modes", []),
        "strict_semantic_safe": bool(state.get("strict_semantic_safe", False)),
        "exact_query_safe": bool(state.get("exact_query_safe", False)),
        "graph_query_safe": bool(state.get("graph_fresh", False)),
        "next_actions": [
            (
                "Use exact symbol/text search for fresh code lookup, or wait for "
                "semantic indexing to finish before semantic/hybrid analysis."
                if exact_fresh
                else "Wait for indexing to finish and retry the analysis request."
            ),
            "GET /sync/status?workspace_id=<workspace_id> to inspect revisions.",
        ],
    }
