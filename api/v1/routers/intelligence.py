"""Intelligence Layer endpoint — single-call, eight-capability orchestration.

Implements architecture.md §17. AI editors call this once and get
back a structured payload combining the eight capabilities so they can
construct an LLM prompt without making 8 round-trips of their own.

Endpoints:

* ``GET  /capabilities`` — capability fingerprint of this deployment.
* ``POST /intelligence/context`` — run the composer.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from api.v1.routers.freshness import cloud_freshness_error, cloud_freshness_state
from omnicode.config.settings import get_settings
from omnicode_core.intelligence import (
    IntelligenceComposer,
    list_capabilities,
)

router = APIRouter(tags=["intelligence"])


def _ok(payload):
    return {"result": payload, "success": True}


def _llm_runtime_status() -> dict:
    mode = (os.environ.get("OMNICODE_LLM_MODE") or "off").strip().lower()
    router_raw = os.environ.get("OMNICODE_LLM_ROUTER")
    router_enabled = (
        False
        if router_raw is not None
        and router_raw.strip().lower() in {"0", "false", "no", "off"}
        else True
    )
    available = mode != "off" and router_enabled
    reason = (
        "LLM mode is off"
        if mode == "off"
        else "OMNICODE_LLM_ROUTER is disabled"
        if not router_enabled
        else "LLM runtime is enabled"
    )
    return {
        "mode": mode,
        "available": available,
        "router_enabled": router_enabled,
        "reason": reason,
    }


def _apply_llm_runtime_status(statuses: list[dict]) -> tuple[list[dict], dict]:
    llm = _llm_runtime_status()
    for item in statuses:
        if item.get("capability") == "llm_enhancement":
            item["available"] = bool(llm["available"])
            item["detail"] = llm["reason"]
            item["state"] = "ready" if llm["available"] else "unavailable"
            item["reason"] = llm["reason"]
            if not llm["available"]:
                item["backend"] = ""
    return statuses, llm


def _snapshot_exact_symbol(
    *,
    workspace_id: Optional[str],
    symbol: Optional[str],
) -> Optional[dict[str, Any]]:
    if not workspace_id or not symbol or not symbol.strip():
        return None
    try:
        from api.v1.routers.search import _exact_index

        exact_rows = _exact_index().search_symbols(
            workspace_id=workspace_id,
            query=symbol.strip(),
            fuzzy=False,
            min_score=1.0,
            max_results=1,
        )
        if exact_rows:
            row = exact_rows[0]
            return {
                "file_path": row.path,
                "symbol_name": row.name,
                "symbol_type": row.kind,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "signature": row.signature,
                "relevance_score": row.score,
                "why_matched": [row.why, "exact_index"],
                "source": "exact_index",
                "hash": row.hash,
                "revision": row.revision,
            }
    except Exception:
        pass
    try:
        from api.v1.routers.search import _snapshot_symbol_search

        rows = _snapshot_symbol_search(
            workspace_id=workspace_id,
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


def _seed_context_from_snapshot_symbol(
    payload: dict[str, Any],
    *,
    row: dict[str, Any],
    freshness: Optional[dict[str, Any]],
) -> dict[str, Any]:
    file_path = row.get("file_path")
    symbol_name = row.get("symbol_name")
    search_row = {
        "file": file_path,
        "file_path": file_path,
        "symbol": symbol_name,
        "score": row.get("relevance_score", 1.0),
        "snippet": row.get("signature", ""),
        "start_line": row.get("line_start"),
        "end_line": row.get("line_end"),
        "source": row.get("source", "snapshot_store"),
        "hash": row.get("hash"),
        "revision": row.get("revision"),
        "why_matched": row.get("why_matched", []),
    }

    search = payload.setdefault("search", {})
    existing = search.get("results") or []
    deduped = [
        item
        for item in existing
        if not (
            (item.get("file") or item.get("file_path")) == file_path
            and item.get("start_line") == row.get("line_start")
        )
    ]
    same_file = [
        item
        for item in deduped
        if (item.get("file") or item.get("file_path")) == file_path
    ]
    other_files = [
        item
        for item in deduped
        if (item.get("file") or item.get("file_path")) != file_path
    ]
    search["query"] = search.get("query") or symbol_name
    search["results"] = [search_row, *same_file, *other_files]
    search["result_count"] = len(search["results"])
    search["snapshot_exact_priority"] = True
    search["primary_result_source"] = "snapshot_exact_symbol"
    search["semantic_noise_demoted"] = len(other_files)

    code = payload.get("code_understanding") or {}
    if int(code.get("symbol_count") or 0) == 0:
        payload["code_understanding"] = {
            "file": file_path,
            "language": "python" if str(file_path).endswith(".py") else "",
            "symbol_count": 1,
            "symbols": [
                {
                    "name": symbol_name,
                    "kind": row.get("symbol_type"),
                    "start_line": row.get("line_start"),
                    "end_line": row.get("line_end"),
                    "signature": row.get("signature"),
                    "source": "snapshot_store",
                    "hash": row.get("hash"),
                    "revision": row.get("revision"),
                }
            ],
            "source": "snapshot_store",
        }

    impact = payload.get("impact") or {}
    if (
        int(impact.get("affected_count") or 0) == 0
        and int(impact.get("dependent_count") or 0) == 0
        and int(impact.get("files_count") or 0) == 0
        and int(impact.get("total_blast_radius") or 0) <= 1
    ):
        impact.update(
            {
                "symbol": impact.get("symbol") or symbol_name,
                "graph_available": False,
                "graph_status": "unavailable",
                "impact_status": "unknown",
                "confidence": "low",
                "symbol_found": True,
                "symbol_source": "snapshot_store",
                "snapshot_symbol": row,
                "note": (
                    "Symbol exists in the cloud snapshot, but no call-graph "
                    "evidence is available for this snapshot workspace."
                ),
            }
        )
        payload["impact"] = impact

    payload["snapshot_store_used"] = True
    payload["snapshot_exact_symbol"] = True
    payload["context_quality"] = {
        "primary_anchor": "snapshot_exact_symbol",
        "primary_file": file_path,
        "primary_symbol": symbol_name,
        "same_file_results_promoted": len(same_file),
        "semantic_noise_demoted": len(other_files),
    }
    payload["freshness"] = (freshness or {}).get("freshness", "snapshot_available")
    payload["semantic_stale"] = bool((freshness or {}).get("semantic_stale", False))
    payload["accepted_revision"] = (freshness or {}).get("accepted_revision")
    payload["indexed_revision"] = (freshness or {}).get("indexed_revision")
    return payload


def _apply_snapshot_graph_context(
    payload: dict[str, Any],
    *,
    workspace_id: Optional[str],
    symbol: Optional[str],
    symbol_row: Optional[dict[str, Any]],
    depth: int,
) -> dict[str, Any]:
    """Align context impact/capability with the persisted snapshot graph."""

    if not workspace_id or not symbol or not symbol_row:
        return payload
    try:
        from api.v1.routers.graph import (
            _persisted_graph_impact,
            _persisted_graph_status,
        )

        graph_status = _persisted_graph_status(workspace_id)
        impact = None
        if graph_status.get("ready"):
            impact = _persisted_graph_impact(
                workspace_id=workspace_id,
                symbol=symbol,
                depth=depth,
                symbol_row=symbol_row,
            )
        if impact is None:
            impact = _lightweight_snapshot_graph_context_impact(
                symbol=symbol,
                depth=depth,
                symbol_row=symbol_row,
                graph_status=graph_status,
            )
    except Exception as exc:
        payload.setdefault("warnings", []).append(
            f"snapshot_graph_context_unavailable: {exc.__class__.__name__}"
        )
        return payload

    return _attach_snapshot_context_impact(payload, impact)


def _attach_snapshot_context_impact(
    payload: dict[str, Any],
    impact: dict[str, Any],
) -> dict[str, Any]:
    """Attach bounded impact metadata to a snapshot context payload."""

    payload["impact"] = impact
    graph_ready = bool(
        impact.get("graph_available")
        and impact.get("graph_status") == "ready"
    )
    for capability in payload.get("capability_status") or []:
        if capability.get("capability") != "impact_analysis":
            continue
        capability.update({
            "available": True,
            "state": "ready" if graph_ready else "degraded",
            "detail": (
                "persisted workspace graph with deterministic fallback"
                if graph_ready
                else "deterministic references; persisted graph unavailable"
            ),
            "backend": (
                "workspace_graph_index"
                if graph_ready
                else "snapshot_exact_text"
            ),
            "reason": (
                ""
                if graph_ready
                else str(
                    impact.get("fallback_reason")
                    or impact.get("note")
                    or "graph_index_unavailable"
                )
            ),
            "metadata": {
                "graph_index_ready": graph_ready,
                "graph_status": impact.get("graph_status"),
                "impact_status": impact.get("impact_status"),
                "accepted_revision": impact.get("accepted_revision"),
                "indexed_revision": impact.get("indexed_revision"),
            },
        })
        break
    payload.setdefault("context_quality", {})["graph_evidence"] = (
        "persisted_workspace_graph"
        if graph_ready
        else "deterministic_fallback"
    )
    return payload


def _context_fast_graph_probe_enabled() -> bool:
    raw = os.environ.get("OMNICODE_CONTEXT_FAST_GRAPH")
    return bool(raw and raw.strip().lower() in {"1", "true", "yes", "on"})


def _lightweight_snapshot_graph_context_impact(
    *,
    symbol: str,
    depth: int,
    symbol_row: dict[str, Any],
    graph_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return fast, honest graph-degraded context without reference scans.

    ``omni_context`` is an editor opening move. When a large snapshot graph is
    not ready, synchronously scanning exact text references can dominate the
    request latency. Keep the verified definition anchor and let callers ask
    ``omni_impact`` for deeper fallback references when they need them.
    """

    status = dict(graph_status or {})
    return {
        "symbol": symbol,
        "depth": depth,
        "affected_symbols": [],
        "dependent_symbols": [],
        "direct_callers": [],
        "direct_callees": [],
        "affected_count": 0,
        "dependent_count": 0,
        "files_involved": [],
        "files_count": 0,
        "total_blast_radius": 1,
        "references": [],
        "test_candidates": [],
        "graph_index": status,
        "graph_available": False,
        "graph_status": (
            "degraded"
            if status.get("files") or status.get("definitions")
            else "unavailable"
        ),
        "impact_status": "unknown",
        "confidence": "low",
        "symbol_found": True,
        "symbol_source": "snapshot_exact_index",
        "snapshot_symbol": symbol_row,
        "fallback_used": True,
        "fallback_reason": "graph_index_unavailable",
        "fallback": {
            "reason": "graph_index_unavailable",
            "references": [],
            "test_candidates": [],
            "reference_source": "not_scanned_in_context_fast_path",
            "test_candidate_source": "not_scanned_in_context_fast_path",
        },
        "note": (
            "Symbol exists in the exact snapshot index, but the workspace "
            "graph is not ready. Context skipped the expensive reference scan; "
            "call omni_impact for deeper fallback references."
        ),
        "next_actions": [
            f"omni_impact(symbol='{symbol}', depth={depth}, format='json')",
        ],
    }


def _estimate_context_tokens(payload: dict[str, Any]) -> int:
    """Return a conservative JSON token estimate for transport budgeting."""

    probe = dict(payload)
    probe["token_estimate"] = 0
    rendered = json.dumps(
        probe,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return max(1, (len(rendered.encode("utf-8")) + 3) // 4)


def _finalize_snapshot_fast_context(
    payload: dict[str, Any],
    *,
    token_budget: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    """Bound graph-heavy fast-path payloads to the requested token budget."""

    truncated_fields: dict[str, dict[str, int]] = {}

    def trim_list(
        container: dict[str, Any],
        key: str,
        limit: int,
        *,
        label: Optional[str] = None,
    ) -> None:
        value = container.get(key)
        if not isinstance(value, list) or len(value) <= limit:
            return
        field = label or key
        truncated_fields[field] = {
            "original": len(value),
            "returned": max(limit, 0),
        }
        container[key] = value[: max(limit, 0)]

    impact = payload.get("impact")
    if isinstance(impact, dict):
        budget_scale = max(1, min(int(token_budget), 8000))
        compact_limits = {
            "direct_callers": max(5, min(20, budget_scale // 300)),
            "direct_callees": max(8, min(30, budget_scale // 220)),
            "references": max(5, min(30, budget_scale // 250)),
            "test_candidates": max(5, min(20, budget_scale // 300)),
            "files_involved": max(8, min(30, budget_scale // 220)),
            "seed_symbols": max(8, min(30, budget_scale // 220)),
            "ambiguous_seed_symbols": 10,
            "callers": max(5, min(20, budget_scale // 300)),
            "callees": max(8, min(30, budget_scale // 220)),
        }
        for key, limit in compact_limits.items():
            trim_list(impact, key, limit, label=f"impact.{key}")
        inheritance = impact.get("inheritance")
        if isinstance(inheritance, dict):
            trim_list(
                inheritance,
                "edges",
                max(5, min(20, budget_scale // 300)),
                label="impact.inheritance.edges",
            )
            trim_list(
                inheritance,
                "subclasses",
                max(5, min(20, budget_scale // 300)),
                label="impact.inheritance.subclasses",
            )
            trim_list(
                inheritance,
                "bases",
                10,
                label="impact.inheritance.bases",
            )
        fallback = impact.get("fallback")
        if isinstance(fallback, dict):
            trim_list(
                fallback,
                "references",
                max(5, min(20, budget_scale // 300)),
                label="impact.fallback.references",
            )
            trim_list(
                fallback,
                "test_candidates",
                10,
                label="impact.fallback.test_candidates",
            )

    estimate = _estimate_context_tokens(payload)
    if estimate > token_budget and isinstance(impact, dict):
        # Tight budgets retain one verifiable sample per evidence category.
        for key in (
            "references",
            "direct_callees",
            "direct_callers",
            "files_involved",
            "test_candidates",
            "seed_symbols",
            "callers",
            "callees",
        ):
            trim_list(impact, key, 3, label=f"impact.{key}")
        inheritance = impact.get("inheritance")
        if isinstance(inheritance, dict):
            trim_list(
                inheritance,
                "edges",
                3,
                label="impact.inheritance.edges",
            )
        fallback = impact.get("fallback")
        if isinstance(fallback, dict):
            trim_list(
                fallback,
                "references",
                3,
                label="impact.fallback.references",
            )
        estimate = _estimate_context_tokens(payload)

    payload["elapsed_ms"] = max(int(elapsed_ms), 0)
    payload["truncated"] = bool(truncated_fields)
    payload["truncation_reasons"] = (
        ["token_budget"] if truncated_fields else []
    )
    payload["truncated_fields"] = truncated_fields
    payload["token_estimate"] = _estimate_context_tokens(payload)
    payload["token_budget"] = int(token_budget)
    payload["budget_respected"] = payload["token_estimate"] <= int(token_budget)
    return payload


def _snapshot_fast_context_allowed(
    req: "IntelligenceRequest",
    *,
    row: dict[str, Any],
) -> bool:
    """Return true when exact snapshot data is enough for a useful context."""
    file_path = str(row.get("file_path") or "")
    symbol_name = str(row.get("symbol_name") or "")
    if not file_path or not symbol_name:
        return False
    if req.file_path and req.file_path.replace("\\", "/") != file_path:
        return False
    if req.query and req.query.strip() != symbol_name:
        return False
    if req.include_memory or req.include_git_history:
        return False
    return True


def _build_snapshot_fast_context(
    req: "IntelligenceRequest",
    *,
    row: dict[str, Any],
    freshness: Optional[dict[str, Any]],
    workspace_id: Optional[str],
) -> dict[str, Any]:
    """Build a lightweight context from exact snapshot data only."""
    started = time.perf_counter()
    file_path = row.get("file_path")
    symbol_name = row.get("symbol_name")
    statuses, llm = _apply_llm_runtime_status(
        [s.to_dict() for s in list_capabilities()]
    )
    statuses = [
        {
            key: status.get(key)
            for key in (
                "capability",
                "available",
                "state",
                "backend",
                "reason",
            )
            if key in status
        }
        for status in statuses
    ]
    payload: dict[str, Any] = {
        "request": {
            "task": req.task,
            "file_path": req.file_path or file_path,
            "symbol": req.symbol,
            "query": req.query or symbol_name,
            "fast_path": "snapshot_exact_symbol",
        },
        "capability_status": statuses,
        "code_understanding": {
            "file": file_path,
            "language": "python" if str(file_path).endswith(".py") else "",
            "symbol_count": 1,
            "symbols": [
                {
                    "name": symbol_name,
                    "kind": row.get("symbol_type"),
                    "start_line": row.get("line_start"),
                    "end_line": row.get("line_end"),
                    "signature": row.get("signature"),
                    "source": "snapshot_store",
                    "hash": row.get("hash"),
                    "revision": row.get("revision"),
                }
            ],
            "source": "snapshot_store",
        },
        "search": {
            "query": req.query or symbol_name,
            "result_count": 0,
            "results": [],
        },
        "impact": {},
        "memory": {"skipped": True, "reason": "include_memory=false"},
        "git_history": {"skipped": True, "reason": "include_git_history=false"},
        "advisories": [],
        "token_estimate": 0,
        "token_budget": req.token_budget,
        "elapsed_ms": 0,
        "errors": {},
        "llm": llm,
        "context_fast_path": True,
        "context_fast_path_reason": (
            "snapshot exact symbol satisfied file/symbol context without "
            "memory or git history"
        ),
    }
    payload = _seed_context_from_snapshot_symbol(
        payload,
        row=row,
        freshness=freshness,
    )
    if _context_fast_graph_probe_enabled():
        payload = _apply_snapshot_graph_context(
            payload,
            workspace_id=workspace_id,
            symbol=req.symbol,
            symbol_row=row,
            depth=req.impact_depth,
        )
    elif req.symbol:
        impact = _lightweight_snapshot_graph_context_impact(
            symbol=req.symbol,
            depth=req.impact_depth,
            symbol_row=row,
            graph_status={
                "ready": False,
                "current": False,
                "not_checked": True,
                "reason": "context_fast_path_graph_probe_disabled",
            },
        )
        payload = _attach_snapshot_context_impact(payload, impact)
    return _finalize_snapshot_fast_context(
        payload,
        token_budget=req.token_budget,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


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
    statuses, llm = _apply_llm_runtime_status(
        [s.to_dict() for s in list_capabilities()]
    )
    llm_enhancement = next(
        (s for s in statuses if s.get("capability") == "llm_enhancement"),
        {"available": False},
    )
    return _ok(
        {
            "version": get_settings().API_VERSION,
            "capabilities": statuses,
            "total": len(statuses),
            "available": sum(1 for s in statuses if s["available"]),
            "llm": llm,
            "llm_enhancement": llm_enhancement,
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
async def build_context(
    req: IntelligenceRequest,
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Single-call multi-capability composer.

    Returns a structured ``IntelligenceContext`` payload that fits in
    the requested token budget. Failures inside any individual
    capability are reported per-capability via the ``errors`` field
    rather than failing the whole call — this keeps editors usable
    even when (say) the call graph hasn't been built yet.
    """
    effective_workspace_id = None
    if x_omnicode_workspace:
        from api.v1.routers.search import _resolve_search_workspace

        effective_workspace_id = _resolve_search_workspace(x_omnicode_workspace)
    pre_stale = cloud_freshness_error(
        workspace_id=effective_workspace_id,
        min_revision=x_omnicode_min_revision,
        allow_snapshot_fresh=True,
    )
    if pre_stale is not None:
        return pre_stale
    snapshot_row = _snapshot_exact_symbol(
        workspace_id=effective_workspace_id,
        symbol=req.symbol,
    )
    if not snapshot_row:
        stale = cloud_freshness_error(
            workspace_id=effective_workspace_id,
            min_revision=x_omnicode_min_revision,
        )
        if stale is not None:
            return stale
    freshness = cloud_freshness_state(
        workspace_id=effective_workspace_id,
        min_revision=x_omnicode_min_revision,
        include_graph=False,
    )
    if snapshot_row and _snapshot_fast_context_allowed(req, row=snapshot_row):
        return _ok(
            _build_snapshot_fast_context(
                req,
                row=snapshot_row,
                freshness=freshness,
                workspace_id=effective_workspace_id,
            )
        )

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
    payload = ctx.to_dict()
    statuses, llm = _apply_llm_runtime_status(
        list(payload.get("capability_status") or [])
    )
    payload["capability_status"] = statuses
    payload["llm"] = llm
    if snapshot_row:
        payload = _seed_context_from_snapshot_symbol(
            payload,
            row=snapshot_row,
            freshness=freshness,
        )
        payload = _apply_snapshot_graph_context(
            payload,
            workspace_id=effective_workspace_id,
            symbol=req.symbol,
            symbol_row=snapshot_row,
            depth=req.impact_depth,
        )
    return _ok(payload)


__all__ = ["router"]
