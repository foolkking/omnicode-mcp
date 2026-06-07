"""Freshness checks shared by cloud analysis routes."""

from __future__ import annotations

from typing import Any, Optional

from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def cloud_freshness_state(
    *,
    workspace_id: Optional[str],
    min_revision: Optional[int],
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
    required_revision = max(min_revision, accepted_revision)
    snapshot_required_revision = min_revision
    semantic_fresh = indexed_revision >= required_revision
    snapshot_fresh = accepted_revision >= snapshot_required_revision
    freshness = (
        "fresh"
        if semantic_fresh
        else "snapshot_fresh"
        if snapshot_fresh
        else "stale"
    )
    return {
        "workspace_id": workspace_id.strip(),
        "accepted_revision": accepted_revision,
        "indexed_revision": indexed_revision,
        "required_revision": required_revision,
        "snapshot_required_revision": snapshot_required_revision,
        "semantic_fresh": semantic_fresh,
        "snapshot_fresh": snapshot_fresh,
        "semantic_stale": not semantic_fresh,
        "freshness": freshness,
    }


def cloud_freshness_error(
    *,
    workspace_id: Optional[str],
    min_revision: Optional[int],
    allow_snapshot_fresh: bool = False,
) -> Optional[dict[str, Any]]:
    """Return a structured stale-index error when a min revision is unmet.

    ``allow_snapshot_fresh`` is for endpoints that answer directly from the
    object-store snapshot (for example exact symbol/text bootstrap). Those
    routes may proceed when the snapshot has accepted the requested revision
    even if the semantic/vector index is still catching up.
    """
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
    if allow_snapshot_fresh and state["snapshot_fresh"]:
        return None

    return {
        "ok": False,
        "success": False,
        "stale": True,
        "freshness": "stale",
        "error": "Cloud index is stale",
        "workspace_id": state["workspace_id"],
        "accepted_revision": state["accepted_revision"],
        "indexed_revision": state["indexed_revision"],
        "required_revision": state["required_revision"],
        "next_actions": [
            "Wait for indexing to finish and retry the analysis request.",
            "GET /sync/status?workspace_id=<workspace_id> to inspect revisions.",
        ],
    }
