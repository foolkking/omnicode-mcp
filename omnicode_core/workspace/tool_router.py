"""Hybrid local/cloud routing policy for OmniCode MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

LOCAL_AUTHORITY_TOOLS = frozenset(
    {
        "omni_read",
        "omni_patch",
        "omni_diagnostics",
        "omni_memory",
        "omni_skill",
        "discover_tools",
        "omni_edit",
        "omni_intelligence",
    }
)

LOCAL_AUTHORITY_REQUIRED_TOOLS = frozenset({"omni_read", "omni_patch"})

CLOUD_AFTER_BARRIER_TOOLS = frozenset(
    {
        "omni_search",
        "omni_context",
        "omni_impact",
        "omni_analyze",
    }
)

AGGREGATE_TOOLS = frozenset({"omni_status"})

KNOWN_TOOLS = LOCAL_AUTHORITY_TOOLS | CLOUD_AFTER_BARRIER_TOOLS | AGGREGATE_TOOLS


@dataclass(frozen=True)
class SyncRevisionState:
    local_revision: int = 0
    accepted_revision: int = 0
    indexed_revision: int = 0
    cloud_available: bool = True
    pending_count: int = 0
    required_revision: Optional[int] = None

    @property
    def cloud_required_revision(self) -> int:
        if self.required_revision is not None:
            return max(0, int(self.required_revision))
        if self.pending_count <= 0 and self.accepted_revision > 0:
            return self.accepted_revision
        return self.local_revision

    @property
    def cloud_is_current(self) -> bool:
        return (
            self.pending_count <= 0
            and self.indexed_revision >= self.cloud_required_revision
        )


@dataclass(frozen=True)
class ToolRoute:
    tool: str
    target: str
    reason: str
    requires_barrier: bool = False
    barrier_min_revision: Optional[int] = None
    stale: bool = False
    local_revision: int = 0
    accepted_revision: int = 0
    indexed_revision: int = 0
    local_authority: bool = False
    local_first: bool = False
    next_actions: list[str] = field(default_factory=list)


class HybridToolRouter:
    """Decide whether a tool should run locally, in cloud, or be blocked."""

    def __init__(self, *, executor: str = "hybrid") -> None:
        self.executor = (executor or "hybrid").strip().lower()
        if self.executor not in {"local", "remote", "hybrid"}:
            raise ValueError("executor must be one of: local, remote, hybrid")

    def route(
        self,
        tool: str,
        *,
        sync_state: Optional[SyncRevisionState] = None,
    ) -> ToolRoute:
        name = (tool or "").strip()
        if not name:
            return ToolRoute(
                tool=name,
                target="blocked",
                reason="tool name is required",
            )
        state = sync_state or SyncRevisionState()

        if name not in KNOWN_TOOLS:
            return ToolRoute(
                tool=name,
                target="local",
                reason="unknown tool; preserve existing local behavior",
                local_revision=state.local_revision,
                accepted_revision=state.accepted_revision,
                indexed_revision=state.indexed_revision,
            )

        if name in AGGREGATE_TOOLS:
            return ToolRoute(
                tool=name,
                target="aggregate",
                reason="status combines local runtime and cloud sync state",
                local_revision=state.local_revision,
                accepted_revision=state.accepted_revision,
                indexed_revision=state.indexed_revision,
            )

        if self.executor == "local" or name in LOCAL_AUTHORITY_TOOLS:
            local_authority = name in LOCAL_AUTHORITY_REQUIRED_TOOLS
            local_first = name == "omni_diagnostics"
            if local_authority:
                reason = "tool requires local workspace authority"
            elif local_first:
                reason = "tool runs local-first diagnostics"
            elif self.executor == "local":
                reason = "executor is local"
            else:
                reason = "tool is served by the local MCP runtime"
            return ToolRoute(
                tool=name,
                target="local",
                reason=reason,
                local_revision=state.local_revision,
                accepted_revision=state.accepted_revision,
                indexed_revision=state.indexed_revision,
                local_authority=local_authority,
                local_first=local_first,
            )

        if name in CLOUD_AFTER_BARRIER_TOOLS:
            if not state.cloud_available:
                return ToolRoute(
                    tool=name,
                    target="blocked",
                    reason="cloud backend is unavailable",
                    local_revision=state.local_revision,
                    accepted_revision=state.accepted_revision,
                    indexed_revision=state.indexed_revision,
                    next_actions=[
                        "Run omni_status() to inspect sync state.",
                        "Retry after the cloud backend is reachable.",
                    ],
                )
            if state.pending_count > 0:
                return ToolRoute(
                    tool=name,
                    target="blocked",
                    reason="local changes are pending sync",
                    requires_barrier=True,
                    barrier_min_revision=state.cloud_required_revision,
                    stale=True,
                    local_revision=state.local_revision,
                    accepted_revision=state.accepted_revision,
                    indexed_revision=state.indexed_revision,
                    next_actions=[
                        "Push pending sync changes and wait for indexing to finish.",
                        "Run omni_status() to inspect sync state.",
                    ],
                )
            if state.cloud_is_current:
                return ToolRoute(
                    tool=name,
                    target="cloud",
                    reason="cloud index is current for the local revision",
                    requires_barrier=True,
                    barrier_min_revision=state.cloud_required_revision,
                    local_revision=state.local_revision,
                    accepted_revision=state.accepted_revision,
                    indexed_revision=state.indexed_revision,
                )
            return ToolRoute(
                tool=name,
                target="blocked",
                reason="cloud index is stale for the local revision",
                requires_barrier=True,
                barrier_min_revision=state.cloud_required_revision,
                stale=True,
                local_revision=state.local_revision,
                accepted_revision=state.accepted_revision,
                indexed_revision=state.indexed_revision,
                next_actions=[
                    "Push pending sync changes and wait for indexing to finish.",
                    "Run omni_status() to inspect sync state.",
                ],
            )

        return ToolRoute(
            tool=name,
            target="local",
            reason="default local route",
            local_revision=state.local_revision,
            accepted_revision=state.accepted_revision,
            indexed_revision=state.indexed_revision,
        )


__all__ = [
    "AGGREGATE_TOOLS",
    "CLOUD_AFTER_BARRIER_TOOLS",
    "HybridToolRouter",
    "KNOWN_TOOLS",
    "LOCAL_AUTHORITY_REQUIRED_TOOLS",
    "LOCAL_AUTHORITY_TOOLS",
    "SyncRevisionState",
    "ToolRoute",
]
