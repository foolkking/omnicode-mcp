"""
Search and indexing endpoints
Provides semantic search, text search, symbol search, and index management
"""

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core import get_ast_parser, get_search_engine
from core.config import get_settings
from omnicode.ast_engine.graph import CallGraphBuilder
from omnicode.search.models import SearchRequest
from utils import (
    create_error_response,
    create_success_response,
    validate_file_path,
)

router = APIRouter(prefix="/search", tags=["search"])


@router.post("")
async def search_codebase(request: SearchRequest):
    """Search the codebase using semantic search"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        results = await search_engine.search(request)

        # Format results for API response
        formatted_results = []
        for result in results:
            formatted_results.append(
                {
                    "file_path": result.file_path,
                    "symbol_name": result.symbol_name,
                    "chunk_type": result.chunk_type,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                    "signature": result.signature,
                    "docstring": result.docstring,
                    "relevance_score": result.relevance_score,
                }
            )

        return create_success_response(
            {
                "query": request.query,
                "search_type": request.search_type,
                "results": formatted_results,
                "total_results": len(formatted_results),
            }
        )

    except Exception as e:
        return create_error_response(f"Search failed: {str(e)}", 500)


@router.post("/index")
async def index_codebase(
    force: bool = Query(False, description="Force full rebuild (ignore file tracker cache)"),
):
    """Index the codebase incrementally (or force full rebuild).

    By default, only new/modified files are re-indexed and deleted files
    are removed.  Unchanged files are skipped entirely.  This typically
    reduces indexing time from 30-60s to 2-3s.

    Pass ?force=true to clear the file tracker and rebuild everything.
    """
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        if force:
            # Clear the file tracker so everything looks "new"
            import os
            from omnicode_core.index.file_tracker import FileTracker
            tracker_db = os.path.join(search_engine.db_dir, "file_tracker.db")
            FileTracker(tracker_db).clear()

        await search_engine.index_codebase()
        stats = search_engine.get_stats()

        return create_success_response(
            {"message": "Codebase indexing completed", "stats": stats}
        )

    except Exception as e:
        return create_error_response(f"Indexing failed: {str(e)}", 500)


@router.post("/update_file")
async def update_file_index(
    file_path: str = Query(..., description="File path to update")
):
    """Update index for a specific file"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        settings = get_settings()
        await validate_file_path(file_path, settings.WORKING_DIR)
        await search_engine.update_file(file_path)

        return create_success_response(f"File index updated: {file_path}")

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"File update failed: {str(e)}", 500)


@router.post("/text")
async def text_search(
    query: str = Query(..., description="Text to search for"),
    file_pattern: str = Query("*.py", description="File pattern filter"),
    use_regex: bool = Query(False, description="Use regex matching"),
    case_sensitive: bool = Query(False, description="Case sensitive search"),
    max_results: int = Query(50, description="Maximum results"),
):
    """Search for text content in files"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        request = SearchRequest(
            query=query,
            search_type="text",
            file_pattern=file_pattern,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

        results = await search_engine.search(request)

        # Format results for API response
        formatted_results = []
        for result in results:
            formatted_results.append(
                {
                    "file_path": result.file_path,
                    "line_number": result.line_start,
                    "content": result.chunk_type,  # Contains the matched line content
                    "match_type": "text",
                }
            )

        return create_success_response(
            {
                "query": query,
                "search_type": "text",
                "file_pattern": file_pattern,
                "use_regex": use_regex,
                "results": formatted_results,
                "total_results": len(formatted_results),
            }
        )

    except Exception as e:
        return create_error_response(f"Text search failed: {str(e)}", 500)


@router.post("/symbols")
async def symbol_search(
    query: str = Query(..., description="Symbol name to search for"),
    symbol_type: Optional[str] = Query(
        None, description="Symbol type filter (function, class, interface)"
    ),
    file_pattern: Optional[str] = Query(None, description="File pattern filter"),
    fuzzy: bool = Query(True, description="Enable fuzzy matching"),
    min_score: float = Query(0.5, description="Minimum fuzzy match score"),
    max_results: int = Query(20, description="Maximum results"),
):
    """Search for symbols with fuzzy matching"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        search_type = "fuzzy_symbol" if fuzzy else "symbol_exact"

        request = SearchRequest(
            query=query,
            search_type=search_type,
            symbol_type=symbol_type,
            file_pattern=file_pattern,
            fuzzy=fuzzy,
            min_score=min_score,
            max_results=max_results,
        )

        results = await search_engine.search(request)

        # Format results for API response
        formatted_results = []
        for result in results:
            formatted_results.append(
                {
                    "file_path": result.file_path,
                    "symbol_name": result.symbol_name,
                    "symbol_type": result.chunk_type,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                    "signature": result.signature,
                    "relevance_score": result.relevance_score,
                }
            )

        return create_success_response(
            {
                "query": query,
                "search_type": search_type,
                "symbol_type": symbol_type,
                "fuzzy_enabled": fuzzy,
                "results": formatted_results,
                "total_results": len(formatted_results),
            }
        )

    except Exception as e:
        return create_error_response(f"Symbol search failed: {str(e)}", 500)


@router.get("/stats")
async def get_search_statistics():
    """Get detailed search engine statistics"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        stats = search_engine.get_stats()

        return create_success_response(
            {
                "index_stats": stats,
                "status": "healthy" if stats.get("total_files", 0) > 0 else "empty",
                "last_indexed": stats.get("last_indexed", "never"),
                "index_size_mb": (
                    stats.get("index_size", 0) / (1024 * 1024)
                    if stats.get("index_size")
                    else 0
                ),
            }
        )

    except Exception as e:
        return create_error_response(f"Failed to get search stats: {str(e)}", 500)



# ============================================================================
# STAGE 3.9 — AST query endpoints
# ============================================================================
class SymbolQueryRequest(BaseModel):
    symbol: str
    direction: str = "both"  # 'callers' | 'callees' | 'both'
    path: Optional[str] = None  # File or directory to scope the analysis (relative)
    max_files: int = 200


@router.post("/symbols/relations")
async def query_symbol_relations(req: SymbolQueryRequest):
    """Find callers and/or callees of ``symbol`` using AST analysis.

    The endpoint walks the supplied path (file or directory; defaults to the
    working directory) and builds an in-memory call graph, then returns the
    requested relations.
    """
    try:
        ast_parser = get_ast_parser()
        if ast_parser is None:
            return create_error_response("AST parser not initialized", 500)

        settings = get_settings()
        scope_path = settings.WORKING_DIR
        if req.path:
            candidate = Path(settings.WORKING_DIR) / req.path
            if not candidate.exists():
                return create_error_response(f"Path does not exist: {req.path}", 404)
            scope_path = str(candidate)

        builder = CallGraphBuilder(ast_parser)
        graph = builder.build_for_paths([scope_path], max_files=req.max_files)

        result = {
            "symbol": req.symbol,
            "direction": req.direction,
            "scope_path": scope_path,
            "total_edges": len(graph.edges),
            "total_callers_in_graph": len(graph.in_index),
            "total_callees_in_graph": len(graph.out_index),
        }

        if req.direction in ("callers", "both"):
            callers = graph.callers_of(req.symbol)
            edges = [e.model_dump() for e in graph.edges_for(req.symbol, "in")]
            result["callers"] = {
                "count": len(callers),
                "names": callers,
                "edges": edges[:200],
            }
        if req.direction in ("callees", "both"):
            callees = graph.callees_of(req.symbol)
            edges = [e.model_dump() for e in graph.edges_for(req.symbol, "out")]
            result["callees"] = {
                "count": len(callees),
                "names": callees,
                "edges": edges[:200],
            }

        return create_success_response(result)

    except Exception as e:  # pragma: no cover
        return create_error_response(f"Symbol relation query failed: {e}", 500)


@router.get("/symbols/graph")
async def get_symbols_graph(
    path: Optional[str] = Query(None, description="File or directory (relative)"),
    max_files: int = Query(200, description="Maximum files to scan"),
    max_nodes: int = Query(50, description="Max nodes in ASCII rendering"),
):
    """Return a full call-graph for the given scope as JSON + ASCII rendering."""
    try:
        ast_parser = get_ast_parser()
        if ast_parser is None:
            return create_error_response("AST parser not initialized", 500)

        settings = get_settings()
        scope_path = settings.WORKING_DIR
        if path:
            candidate = Path(settings.WORKING_DIR) / path
            if not candidate.exists():
                return create_error_response(f"Path does not exist: {path}", 404)
            scope_path = str(candidate)

        builder = CallGraphBuilder(ast_parser)
        graph = builder.build_for_paths([scope_path], max_files=max_files)

        # Edge cap scales with max_nodes so the frontend has enough connectivity
        # to actually render the requested node count.  The hard ceiling stops
        # us from blowing up the response payload on very large repos.
        edge_cap = max(500, min(8000, max_nodes * 30))

        return create_success_response(
            {
                "scope_path": scope_path,
                "summary": {
                    "total_edges": len(graph.edges),
                    "total_callers": len(graph.out_index),
                    "total_callees": len(graph.in_index),
                },
                "ascii": graph.render_ascii(max_nodes=max_nodes),
                "edges": [e.model_dump() for e in graph.edges[:edge_cap]],
            }
        )
    except Exception as e:  # pragma: no cover
        return create_error_response(f"Graph build failed: {e}", 500)



# ----------------------------------------------------------------------------
# STAGE 3.11 — Class inheritance hierarchy
# ----------------------------------------------------------------------------
@router.get("/inheritance")
async def get_inheritance_graph(
    path: Optional[str] = Query(
        None, description="File or directory (relative to working dir)"
    ),
    max_files: int = Query(500, description="Maximum files to scan"),
    max_nodes: int = Query(80, description="Maximum nodes in ASCII rendering"),
):
    """Build a class-inheritance graph (subclass → base) for the given scope.

    Supports Python / JS / TS / C++ / Java / Rust.  For Rust we treat
    ``impl Trait for Struct`` as ``Struct → Trait``.
    """
    try:
        from omnicode.ast_engine.inheritance import InheritanceGraphBuilder

        ast_parser = get_ast_parser()
        if ast_parser is None:
            return create_error_response("AST parser not initialized", 500)

        settings = get_settings()
        scope_path = settings.WORKING_DIR
        if path:
            candidate = Path(settings.WORKING_DIR) / path
            if not candidate.exists():
                return create_error_response(f"Path does not exist: {path}", 404)
            scope_path = str(candidate)

        builder = InheritanceGraphBuilder(ast_parser)
        graph = builder.build_for_paths([scope_path], max_files=max_files)

        edge_cap = max(500, min(8000, max_nodes * 30))

        return create_success_response(
            {
                "scope_path": scope_path,
                "summary": graph.stats(),
                "ascii": graph.render_ascii(max_nodes=max_nodes),
                "edges": [e.model_dump() for e in graph.edges[:edge_cap]],
            }
        )
    except Exception as e:  # pragma: no cover
        return create_error_response(f"Inheritance build failed: {e}", 500)


@router.get("/inheritance/{symbol}")
async def query_inheritance_for_symbol(
    symbol: str,
    direction: str = Query(
        "both",
        description="'ancestors' / 'descendants' / 'both' (default both)",
    ),
    max_depth: int = Query(8, description="Transitive query depth limit"),
    path: Optional[str] = Query(
        None, description="Optional scope path (relative)"
    ),
    max_files: int = Query(500, description="Maximum files to scan"),
):
    """Look up the inheritance neighbourhood of a single symbol."""
    try:
        from omnicode.ast_engine.inheritance import InheritanceGraphBuilder

        ast_parser = get_ast_parser()
        if ast_parser is None:
            return create_error_response("AST parser not initialized", 500)

        settings = get_settings()
        scope_path = settings.WORKING_DIR
        if path:
            candidate = Path(settings.WORKING_DIR) / path
            if not candidate.exists():
                return create_error_response(f"Path does not exist: {path}", 404)
            scope_path = str(candidate)

        builder = InheritanceGraphBuilder(ast_parser)
        graph = builder.build_for_paths([scope_path], max_files=max_files)

        result: Dict[str, Any] = {
            "symbol": symbol,
            "scope_path": scope_path,
            "stats": graph.stats(),
        }
        if direction in ("ancestors", "both"):
            result["base_classes"] = graph.base_classes_of(symbol)
            result["ancestors"]    = graph.ancestors_of(symbol, max_depth=max_depth)
        if direction in ("descendants", "both"):
            result["subclasses"]   = graph.subclasses_of(symbol)
            result["descendants"]  = graph.descendants_of(symbol, max_depth=max_depth)
        return create_success_response(result)
    except Exception as e:
        return create_error_response(f"Inheritance query failed: {e}", 500)


# ----------------------------------------------------------------------------
# CATCH-ALL — keep this LAST so specific routes like /symbols/graph and
# /symbols/relations resolve to their dedicated handlers above.
# ----------------------------------------------------------------------------
@router.get("/symbols/{file_path:path}")
async def list_file_symbols(file_path: str):
    """List all symbols in a specific file."""
    # Belt-and-suspenders: even though FastAPI now resolves /symbols/graph and
    # /symbols/relations to the right handlers (they are declared before this
    # route), we still reject those names here so a typo with a trailing
    # slash doesn't silently match the wrong endpoint.
    if file_path in {"graph", "relations"} or file_path.startswith(("graph/", "relations/")):
        return create_error_response(
            f"Reserved path '/symbols/{file_path}' — please use the dedicated "
            "endpoint (/symbols/graph or POST /symbols/relations).",
            404,
        )
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        settings = get_settings()
        await validate_file_path(file_path, settings.WORKING_DIR)

        symbols_info = await search_engine.list_symbols_in_file(file_path)
        return create_success_response(symbols_info)
    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Failed to list symbols: {str(e)}", 500)
