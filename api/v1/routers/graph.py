"""Call-graph impact analysis endpoints (Wave 1, gap §11).

Exposes the seven public methods of
:class:`omnicode_core.graph.impact.ImpactAnalyzer` over REST so AI
editors can reason about blast radius before touching a symbol.

Endpoints:

* ``GET /graph/impact``           — BFS callees + callers up to depth N
* ``GET /graph/entrypoints``      — top-level entry points reaching a symbol
* ``GET /graph/dead``             — symbols with 0 callers
* ``GET /graph/related-tests``    — test files that likely cover a symbol
* ``GET /graph/risk``             — low/medium/high rating with reasons

Visualisation/builder endpoints already exist under ``/project/graph``;
this module is purpose-built for *answering questions about a single
symbol*.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from core.config import get_settings
from omnicode_core.graph.impact import ImpactAnalyzer
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/graph", tags=["graph"])


def _build() -> ImpactAnalyzer:
    return ImpactAnalyzer(get_settings().WORKING_DIR)


@router.get("/impact")
async def graph_impact(
    symbol: str = Query(..., description="Symbol name (function/class/method)"),
    depth: int = Query(2, ge=1, le=5),
    max_files: int = Query(200, ge=10, le=5000),
    scope_path: Optional[str] = Query(None, description="Optional path prefix"),
):
    """BFS the call graph from ``symbol`` to compute blast radius.

    Returns affected (callees) + dependents (callers) symbols, the unique
    set of files that touch any of them, and a total blast radius
    suitable for showing in a UI badge.
    """
    result = await _build().get_impact_radius(
        symbol=symbol, depth=depth, max_files=max_files, scope_path=scope_path
    )
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/entrypoints")
async def graph_entrypoints(
    symbol: str = Query(..., description="Symbol to trace back from"),
    max_files: int = Query(200, ge=10, le=5000),
):
    """Find top-level entry points (0-caller roots) that eventually
    reach ``symbol``."""
    result = await _build().find_entrypoints(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/dead")
async def graph_dead_symbols(
    max_files: int = Query(200, ge=10, le=5000),
):
    """List symbols with 0 callers (potential dead code).

    Excludes known entry-point patterns (`main`, `app`, `__init__`,
    `setup`, `teardown`, `conftest`) and `test_*` functions.
    """
    result = await _build().find_dead_symbols(max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/related-tests")
async def graph_related_tests(
    symbol: str = Query(..., description="Symbol to find tests for"),
    max_files: int = Query(200, ge=10, le=5000),
):
    """Suggest test files that likely cover ``symbol``.

    Uses two signals: (1) call-graph reachability from `test_*`
    functions, (2) filename heuristics. Returns ready-to-run pytest
    commands as ``suggested_commands``.
    """
    result = await _build().suggest_related_tests(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/risk")
async def graph_risk(
    symbol: str = Query(..., description="Symbol to assess"),
    max_files: int = Query(200, ge=10, le=5000),
):
    """Compute a low/medium/high risk rating for changing ``symbol``.

    Factors in caller count, file footprint, and whether tests cover it.
    Useful for the editor to decide whether to require a confirmation
    step before applying a patch.
    """
    result = await _build().assess_risk(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


__all__ = ["router"]
