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

from typing import Any, Optional

from fastapi import APIRouter, Header, Query

from api.v1.routers.freshness import cloud_freshness_error
from core.config import get_settings
from omnicode_core.graph.impact import ImpactAnalyzer
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/graph", tags=["graph"])


def _build() -> ImpactAnalyzer:
    return ImpactAnalyzer(get_settings().WORKING_DIR)


def _snapshot_symbol_row(
    *,
    workspace_id: Optional[str],
    symbol: str,
) -> Optional[dict[str, Any]]:
    if not workspace_id or not symbol.strip():
        return None
    try:
        from api.v1.routers.search import _snapshot_symbol_search

        rows = _snapshot_symbol_search(
            workspace_id=workspace_id.strip(),
            query=symbol.strip(),
            symbol_type=None,
            file_pattern=None,
            fuzzy=False,
            min_score=1.0,
            max_results=1,
            existing_keys=set(),
        )
    except Exception:
        return None
    return rows[0] if rows else None


def _snapshot_revision_state(workspace_id: Optional[str]) -> dict[str, Any]:
    if not workspace_id:
        return {}
    try:
        status = CloudSnapshotStore().status(workspace_id.strip())
    except Exception:
        return {}
    return {
        "accepted_revision": int(status.get("accepted_revision", 0)),
        "indexed_revision": int(status.get("indexed_revision", 0)),
    }


def _impact_has_no_graph_evidence(result: dict[str, Any]) -> bool:
    return (
        int(result.get("affected_count") or 0) == 0
        and int(result.get("dependent_count") or 0) == 0
        and int(result.get("files_count") or 0) == 0
        and int(result.get("total_blast_radius") or 0) <= 1
    )


def _mark_snapshot_graph_unknown(
    result: dict[str, Any],
    *,
    workspace_id: Optional[str],
    symbol_row: dict[str, Any],
) -> dict[str, Any]:
    revision_state = _snapshot_revision_state(workspace_id)
    result.update(
        {
            "graph_available": False,
            "graph_status": "unavailable",
            "impact_status": "unknown",
            "confidence": "low",
            "symbol_found": True,
            "symbol_source": "snapshot_store",
            "snapshot_symbol": symbol_row,
            "note": (
                "Symbol exists in the cloud snapshot, but no call-graph evidence "
                "is available for this snapshot workspace."
            ),
            **revision_state,
        }
    )
    return result


@router.get("/impact")
async def graph_impact(
    symbol: str = Query(..., description="Symbol name (function/class/method)"),
    depth: int = Query(2, ge=1, le=5),
    max_files: int = Query(200, ge=10, le=5000),
    scope_path: Optional[str] = Query(None, description="Optional path prefix"),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """BFS the call graph from ``symbol`` to compute blast radius.

    Returns affected (callees) + dependents (callers) symbols, the unique
    set of files that touch any of them, and a total blast radius
    suitable for showing in a UI badge.
    """
    stale = cloud_freshness_error(
        workspace_id=x_omnicode_workspace,
        min_revision=x_omnicode_min_revision,
    )
    if stale is not None:
        return stale
    result = await _build().get_impact_radius(
        symbol=symbol, depth=depth, max_files=max_files, scope_path=scope_path
    )
    if "error" in result:
        return create_error_response(result["error"], 500)
    symbol_row = _snapshot_symbol_row(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
    )
    if symbol_row and _impact_has_no_graph_evidence(result):
        result = _mark_snapshot_graph_unknown(
            result,
            workspace_id=x_omnicode_workspace,
            symbol_row=symbol_row,
        )
    return create_success_response(result)


@router.get("/entrypoints")
async def graph_entrypoints(
    symbol: str = Query(..., description="Symbol to trace back from"),
    max_files: int = Query(200, ge=10, le=5000),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Find top-level entry points (0-caller roots) that eventually
    reach ``symbol``."""
    stale = cloud_freshness_error(
        workspace_id=x_omnicode_workspace,
        min_revision=x_omnicode_min_revision,
    )
    if stale is not None:
        return stale
    result = await _build().find_entrypoints(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/dead")
async def graph_dead_symbols(
    max_files: int = Query(200, ge=10, le=5000),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """List symbols with 0 callers (potential dead code).

    Excludes known entry-point patterns (`main`, `app`, `__init__`,
    `setup`, `teardown`, `conftest`) and `test_*` functions.
    """
    stale = cloud_freshness_error(
        workspace_id=x_omnicode_workspace,
        min_revision=x_omnicode_min_revision,
    )
    if stale is not None:
        return stale
    result = await _build().find_dead_symbols(max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/related-tests")
async def graph_related_tests(
    symbol: str = Query(..., description="Symbol to find tests for"),
    max_files: int = Query(200, ge=10, le=5000),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Suggest test files that likely cover ``symbol``.

    Uses two signals: (1) call-graph reachability from `test_*`
    functions, (2) filename heuristics. Returns ready-to-run pytest
    commands as ``suggested_commands``.
    """
    stale = cloud_freshness_error(
        workspace_id=x_omnicode_workspace,
        min_revision=x_omnicode_min_revision,
    )
    if stale is not None:
        return stale
    result = await _build().suggest_related_tests(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)


@router.get("/risk")
async def graph_risk(
    symbol: str = Query(..., description="Symbol to assess"),
    max_files: int = Query(200, ge=10, le=5000),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Compute a low/medium/high risk rating for changing ``symbol``.

    Factors in caller count, file footprint, and whether tests cover it.
    Useful for the editor to decide whether to require a confirmation
    step before applying a patch.
    """
    stale = cloud_freshness_error(
        workspace_id=x_omnicode_workspace,
        min_revision=x_omnicode_min_revision,
    )
    if stale is not None:
        return stale
    result = await _build().assess_risk(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
    symbol_row = _snapshot_symbol_row(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
    )
    if (
        symbol_row
        and int(result.get("direct_callers") or 0) == 0
        and int(result.get("files_affected") or 0) == 0
    ):
        result.update(
            {
                "risk": "unknown",
                "risk_score": None,
                "reasons": [
                    "Call graph is not available for this snapshot workspace"
                ],
                "graph_available": False,
                "graph_status": "unavailable",
                "confidence": "low",
                "symbol_found": True,
                "symbol_source": "snapshot_store",
                "snapshot_symbol": symbol_row,
                **_snapshot_revision_state(x_omnicode_workspace),
            }
        )
    return create_success_response(result)


__all__ = ["router"]
