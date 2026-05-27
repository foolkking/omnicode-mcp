"""Intelligence Layer endpoint — single-call, eight-capability orchestration.

Implements architecture-v2.md §17. AI editors call this once and get
back a structured payload combining the eight capabilities so they can
construct an LLM prompt without making 8 round-trips of their own.

Endpoints:

* ``GET  /capabilities`` — capability fingerprint of this deployment.
* ``POST /intelligence/context`` — run the composer.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from omnicode.config.settings import get_settings
from omnicode_core.intelligence import (
    IntelligenceComposer,
    list_capabilities,
)

router = APIRouter(tags=["intelligence"])


def _ok(payload):
    return {"result": payload, "success": True}


# ---------------------------------------------------------------------------
# Capability fingerprint
# ---------------------------------------------------------------------------
@router.get("/capabilities")
async def get_capabilities():
    """Return the eight-capability fingerprint of this deployment.

    Useful for AI editors to negotiate features at startup — they can
    decide e.g. whether to skip an LLM-rerank step when the deployment
    has no provider configured.
    """
    statuses = [s.to_dict() for s in list_capabilities()]
    return _ok(
        {
            "version": get_settings().API_VERSION,
            "capabilities": statuses,
            "total": len(statuses),
            "available": sum(1 for s in statuses if s["available"]),
        }
    )


# ---------------------------------------------------------------------------
# Composer entry point
# ---------------------------------------------------------------------------
class IntelligenceRequest(BaseModel):
    task: Optional[str] = Field(
        default=None,
        description="Free-form description of what the editor is trying to do.",
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Repo-relative path the editor is currently editing.",
    )
    symbol: Optional[str] = Field(
        default=None,
        description="Symbol the editor is focused on (function/class).",
    )
    query: Optional[str] = Field(
        default=None,
        description="Free-text search query. Falls back to `symbol` if omitted.",
    )
    max_search_results: int = Field(default=5, ge=1, le=30)
    impact_depth: int = Field(default=2, ge=1, le=5)
    memory_max: int = Field(default=5, ge=0, le=20)
    token_budget: int = Field(default=4096, ge=512, le=32_000)
    include_git_history: bool = Field(default=True)
    include_impact: bool = Field(default=True)
    include_memory: bool = Field(default=True)


@router.post("/intelligence/context")
async def build_context(req: IntelligenceRequest):
    """Single-call multi-capability composer.

    Returns a structured ``IntelligenceContext`` payload that fits in
    the requested token budget. Failures inside any individual
    capability are reported per-capability via the ``errors`` field
    rather than failing the whole call — this keeps editors usable
    even when (say) the call graph hasn't been built yet.
    """
    composer = IntelligenceComposer(working_dir=get_settings().WORKING_DIR)
    ctx = await composer.build(
        task=req.task,
        file_path=req.file_path,
        symbol=req.symbol,
        query=req.query,
        max_search_results=req.max_search_results,
        impact_depth=req.impact_depth,
        memory_max=req.memory_max,
        token_budget=req.token_budget,
        include_git_history=req.include_git_history,
        include_impact=req.include_impact,
        include_memory=req.include_memory,
    )
    return _ok(ctx.to_dict())


__all__ = ["router"]
