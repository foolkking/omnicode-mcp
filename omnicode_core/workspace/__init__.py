"""Workspace registry — multi-project bookkeeping for the MCP server.

A "workspace" is a tuple ``(id, name, path)`` where ``path`` is an
absolute directory on the host. The active workspace defines what
``WORKING_DIR`` resolves to for downstream services. The registry
persists to ``~/.kiro/codebase-mcp/workspaces.json`` so that the user
sees the same list across server restarts.

Backend-only — the existing ``/working-directory`` router still works
for "switch immediate cwd"; this layer adds saved bookmarks +
multi-project access (P2 step 4 of architecture-v2).
"""

from omnicode_core.workspace.registry import (
    Workspace,
    WorkspaceRegistry,
    get_workspace_registry,
)
from omnicode_core.workspace.agent_auto import (
    AgentAutoDecision,
    decide_agent_auto,
)
from omnicode_core.workspace.request import (
    WorkspaceRequest,
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from omnicode_core.workspace.tool_router import (
    HybridToolRouter,
    SyncRevisionState,
    ToolRoute,
)

__all__ = [
    "Workspace",
    "WorkspaceRegistry",
    "AgentAutoDecision",
    "WorkspaceRequest",
    "WorkspaceResolutionError",
    "HybridToolRouter",
    "SyncRevisionState",
    "ToolRoute",
    "decide_agent_auto",
    "get_workspace_registry",
    "resolve_workspace_request",
]
