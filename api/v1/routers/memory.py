"""
Memory system endpoints
Handles memory storage, search, context, and statistics
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core import get_memory_manager
from core.config import get_settings
from memory_system.api_endpoints import (
    get_context_summary_endpoint,
    get_memory_stats_endpoint,
    search_memories_endpoint,
    store_memory_endpoint,
    update_memory_endpoint,
)
from memory_system.models import MemoryRequest, MemorySearchRequest
from utils import (
    create_detailed_error_response,
    create_error_response,
    create_success_response,
)

router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/store")
async def store_memory(request: MemoryRequest):
    """Store a new memory in the knowledge base"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        result = await store_memory_endpoint(memory_manager, request)
        return result

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Memory storage failed: {str(e)}", 500)


@router.post("/search")
async def search_memories(request: MemorySearchRequest):
    """Search memories using semantic search and filters"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        result = await search_memories_endpoint(memory_manager, request)
        return result

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Memory search failed: {str(e)}", 500)


@router.get("/context")
async def get_memory_context(session_id: Optional[str] = Query(None)):
    """Get context summary for new chat session"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        result = await get_context_summary_endpoint(memory_manager, session_id)
        return result

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Memory context retrieval failed: {str(e)}", 500)


@router.put("/{memory_id}")
async def update_memory(memory_id: int, updates: Dict[str, Any]):
    """Update existing memory with proper error handling"""
    try:
        memory_manager = get_memory_manager()
        settings = get_settings()

        if not memory_manager:
            return create_detailed_error_response(
                "Memory system not initialized",
                500,
                "ServiceNotAvailable",
                {"service": "MemoryManager"},
                "MemorySystem",
                "initialization",
                settings.WORKING_DIR,
            )

        # Validate memory_id exists
        try:
            search_result = await memory_manager.search_memories(
                query="*", max_results=1, filters={"id": memory_id}
            )

            if not search_result or not search_result.get("memories"):
                return create_detailed_error_response(
                    f"Memory with ID {memory_id} not found",
                    404,
                    "MemoryNotFound",
                    {
                        "memory_id": memory_id,
                        "suggestion": "Use memory search to find existing memory IDs",
                    },
                    "MemorySystem",
                    "memory_lookup",
                    settings.WORKING_DIR,
                )

        except Exception as lookup_error:
            return create_detailed_error_response(
                f"Error looking up memory {memory_id}: {str(lookup_error)}",
                500,
                "MemoryLookupError",
                {"memory_id": memory_id, "lookup_error": str(lookup_error)},
                "MemorySystem",
                "memory_lookup",
                settings.WORKING_DIR,
            )

        result = await update_memory_endpoint(memory_manager, memory_id, updates)
        return result

    except HTTPException:
        raise
    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Memory update failed: {str(e)}",
            500,
            "MemoryUpdateError",
            {
                "memory_id": memory_id,
                "updates": updates,
                "exception_type": type(e).__name__,
            },
            "MemorySystem",
            "update_memory",
            settings.WORKING_DIR,
        )


@router.get("/stats")
async def get_memory_statistics():
    """Get memory system statistics"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        result = get_memory_stats_endpoint(memory_manager)
        return result

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Memory stats retrieval failed: {str(e)}", 500)


@router.delete("/{memory_id}")
async def archive_memory(memory_id: int):
    """Archive (soft delete) a memory"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        await update_memory_endpoint(memory_manager, memory_id, {"status": "archived"})

        return create_success_response(f"Memory {memory_id} archived successfully")

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Memory archive failed: {str(e)}", 500)


@router.get("/list")
async def list_all_memories(
    category: Optional[str] = Query(None, description="Filter by category"),
    importance_min: Optional[int] = Query(1, description="Minimum importance level"),
    importance_max: Optional[int] = Query(5, description="Maximum importance level"),
    limit: int = Query(50, description="Maximum memories to return", le=200),
    offset: int = Query(0, description="Offset for pagination"),
    sort_by: str = Query(
        "timestamp", description="Sort field (timestamp, importance, category)"
    ),
    sort_order: str = Query("desc", description="Sort order (asc, desc)"),
):
    """List memories with advanced filtering and pagination"""
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        # Build search request
        search_request = MemorySearchRequest(
            query=None,
            category=category,
            min_importance=importance_min,
            max_results=limit,
            include_archived=False,
        )

        results = await memory_manager.search_memories_advanced(search_request)

        # Apply additional filtering
        memories = results.get("memories", [])

        if importance_max:
            memories = [m for m in memories if m.get("importance", 1) <= importance_max]

        # Sort memories
        if sort_by == "importance":
            memories.sort(
                key=lambda x: x.get("importance", 1), reverse=(sort_order == "desc")
            )
        elif sort_by == "category":
            memories.sort(
                key=lambda x: x.get("category", ""), reverse=(sort_order == "desc")
            )
        else:  # timestamp
            memories.sort(
                key=lambda x: x.get("timestamp", ""), reverse=(sort_order == "desc")
            )

        # Apply pagination
        paginated_memories = memories[offset : offset + limit]

        return create_success_response(
            {
                "memories": paginated_memories,
                "total_count": len(memories),
                "returned_count": len(paginated_memories),
                "offset": offset,
                "limit": limit,
                "has_more": len(memories) > offset + limit,
            }
        )

    except Exception as e:
        return create_error_response(f"Failed to list memories: {str(e)}", 500)



@router.post("/dedupe")
async def dedupe_memories():
    """Collapse duplicate active memories.

    Groups by content fingerprint; for each duplicate group keeps the
    oldest row (sums access_count, merges tags + related_files, takes
    higher importance) and archives the rest.
    """
    try:
        memory_manager = get_memory_manager()
        if not memory_manager:
            return create_error_response("Memory system not initialized", 500)

        report = memory_manager.dedupe_existing()
        return create_success_response({
            "message": (
                f"Deduplicated {report['groups']} group(s), "
                f"archived {report['archived']} duplicate row(s)."
            ),
            **report,
        })
    except Exception as e:
        return create_error_response(f"Memory dedupe failed: {str(e)}", 500)


# ---------------------------------------------------------------------------
# Memory advisory (Wave 1, gap §10) — proactive recall for code-edit tasks.
# ---------------------------------------------------------------------------
class _AdvisoryRequest(BaseModel):
    """Inputs for :class:`MemoryAdvisor.generate_advisory`."""

    file_path: Optional[str] = None
    symbol: Optional[str] = None
    task: Optional[str] = None
    error_message: Optional[str] = None
    git_diff: Optional[str] = None
    max_memories: int = 5
    max_tokens: int = 800


@router.post("/advisory")
async def generate_memory_advisory(req: _AdvisoryRequest):
    """Build a concise, multi-angle advisory from past memories.

    Searches by file path, symbol, task description, error message, and
    git diff (when supplied), deduplicates, and returns a 300–800-token
    text block plus the IDs of the memories that were referenced. Use
    this to seed an LLM prompt when the editor is about to modify code
    that the project has touched before.
    """
    from omnicode_core.memory.advisory import MemoryAdvisor

    memory_manager = get_memory_manager()
    if not memory_manager:
        return create_error_response("Memory system not initialized", 500)
    advisor = MemoryAdvisor(memory_manager)
    result = await advisor.generate_advisory(
        file_path=req.file_path,
        symbol=req.symbol,
        task=req.task,
        error_message=req.error_message,
        git_diff=req.git_diff,
        max_memories=req.max_memories,
        max_tokens=req.max_tokens,
    )
    return create_success_response(result)
