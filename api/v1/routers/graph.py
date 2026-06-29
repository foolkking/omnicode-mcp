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

import os
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, Query

from api.v1.routers.freshness import cloud_freshness_error
from core.config import get_settings
from omnicode_core.graph.impact import ImpactAnalyzer
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.graph_index import WorkspaceGraphIndex
from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    normalize_snapshot_path,
)
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/graph", tags=["graph"])
_GRAPH_INDEX_CACHE: dict[str, WorkspaceGraphIndex] = {}
_EXACT_INDEX_CACHE: dict[str, SnapshotExactIndex] = {}
_LOCAL_SYMBOL_EXTENSIONS = {
    ".py",
    ".pyi",
    ".java",
    ".scala",
    ".sc",
    ".kt",
    ".kts",
}
_LOCAL_SKIP_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "target",
    "vendor",
    "vendors",
}


def _graph_fallback_text_max_lines() -> int:
    raw = (os.environ.get("OMNICODE_GRAPH_FALLBACK_TEXT_MAX_LINES") or "200000").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 200000


def _build() -> ImpactAnalyzer:
    return ImpactAnalyzer(get_settings().WORKING_DIR)


def _graph_index() -> WorkspaceGraphIndex:
    store = CloudSnapshotStore()
    key = str(store.workspaces_root)
    index = _GRAPH_INDEX_CACHE.get(key)
    if index is None:
        index = WorkspaceGraphIndex(store=store)
        _GRAPH_INDEX_CACHE[key] = index
    return index


def _exact_index() -> SnapshotExactIndex:
    store = CloudSnapshotStore()
    key = str(store.workspaces_root)
    index = _EXACT_INDEX_CACHE.get(key)
    if index is None:
        index = SnapshotExactIndex(store=store)
        _EXACT_INDEX_CACHE[key] = index
    return index


def _snapshot_symbol_row(
    *,
    workspace_id: Optional[str],
    symbol: str,
) -> Optional[dict[str, Any]]:
    if not workspace_id or not symbol.strip():
        return None
    try:
        rows = _exact_index().search_symbols(
            workspace_id=workspace_id.strip(),
            query=symbol.strip(),
            symbol_type=None,
            file_pattern=None,
            fuzzy=False,
            min_score=1.0,
            max_results=1,
        )
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    return {
        "file_path": row.path,
        "symbol_name": row.name,
        "symbol_type": row.kind,
        "line_start": row.line_start,
        "line_end": row.line_end,
        "signature": row.signature,
        "relevance_score": row.score,
        "why_matched": [row.why, "snapshot_exact_index"],
        "source": "snapshot_exact_index",
        "hash": row.hash,
        "revision": row.revision,
    }


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


def _local_symbol_patterns(symbol: str) -> list[tuple[str, re.Pattern[str]]]:
    escaped = re.escape(symbol.strip())
    return [
        (
            "python",
            re.compile(
                rf"^\s*(?P<kind>class|async\s+def|def)\s+{escaped}\b"
            ),
        ),
        (
            "java",
            re.compile(
                rf"\b(?P<kind>class|interface|enum|record)\s+{escaped}\b"
            ),
        ),
        (
            "scala",
            re.compile(
                rf"\b(?P<kind>class|object|trait|def)\s+{escaped}\b"
            ),
        ),
    ]


def _local_source_files(
    root: Path,
    *,
    scope_path: Optional[str] = None,
    max_files: int = 5000,
) -> list[Path]:
    base = root
    if scope_path:
        candidate = (root / scope_path).resolve()
        try:
            candidate.relative_to(root.resolve())
            base = candidate if candidate.is_dir() else candidate.parent
        except ValueError:
            return []
    files: list[Path] = []
    for path in base.rglob("*"):
        if len(files) >= max_files:
            break
        if not path.is_file() or path.suffix.lower() not in _LOCAL_SYMBOL_EXTENSIONS:
            continue
        rel_parts = {
            part.lower()
            for part in path.relative_to(root).parts
        }
        if rel_parts & _LOCAL_SKIP_PARTS:
            continue
        files.append(path)
    return files


def _local_symbol_row(
    *,
    symbol: str,
    scope_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    symbol = symbol.strip()
    if not symbol:
        return None
    try:
        root = Path(get_settings().WORKING_DIR).resolve()
    except Exception:
        return None
    patterns = _local_symbol_patterns(symbol)
    for path in _local_source_files(root, scope_path=scope_path):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for language, pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                raw_kind = (match.groupdict().get("kind") or "").replace(
                    "async def",
                    "function",
                )
                kind = {
                    "def": "function",
                    "function": "function",
                    "class": "class",
                    "object": "object",
                    "trait": "trait",
                    "interface": "interface",
                    "enum": "enum",
                    "record": "record",
                }.get(raw_kind, raw_kind or "symbol")
                return {
                    "file_path": rel,
                    "symbol_name": symbol,
                    "symbol_type": kind,
                    "line_start": line_no,
                    "line_end": line_no,
                    "signature": line.strip(),
                    "relevance_score": 1.0,
                    "why_matched": ["local_symbol_scan"],
                    "source": "local_symbol_scan",
                    "language": language,
                }
    return None


def _persisted_graph_status(
    workspace_id: Optional[str],
    *,
    accepted_revision: Optional[int] = None,
) -> dict[str, Any]:
    if not workspace_id:
        return {}
    revision_state = _snapshot_revision_state(workspace_id)
    required_revision = (
        int(accepted_revision)
        if accepted_revision is not None
        else int(revision_state.get("accepted_revision") or 0)
    )
    try:
        graph_index = _graph_index()
        readiness = getattr(graph_index, "try_readiness", None)
        probe = readiness if callable(readiness) else graph_index.try_status
        return probe(
            workspace_id=workspace_id.strip(),
            accepted_revision=required_revision,
            lock_timeout_ms=75,
        )
    except Exception as exc:
        return {
            "ready": False,
            "current": False,
            "last_error": f"{exc.__class__.__name__}: {exc}",
        }


def _persisted_graph_impact(
    *,
    workspace_id: Optional[str],
    symbol: str,
    depth: int,
    symbol_row: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not workspace_id:
        return None
    status = _persisted_graph_status(workspace_id)
    if symbol_row:
        try:
            symbol_revision = int(symbol_row.get("revision") or 0)
        except (TypeError, ValueError):
            symbol_revision = 0
        if symbol_revision > 0:
            status = _persisted_graph_status(
                workspace_id,
                accepted_revision=symbol_revision,
            )
    if not status.get("ready"):
        return None
    symbol_path = None
    if symbol_row:
        symbol_path = symbol_row.get("file_path") or symbol_row.get("file")
        graph_defs = _graph_index().find_definitions(
            workspace_id=workspace_id.strip(),
            symbol=symbol,
            symbol_path=str(symbol_path) if symbol_path else None,
            limit=3,
            lock_timeout_ms=75,
        )
        if not graph_defs:
            result = {
                "symbol": symbol,
                "found": False,
                "depth": depth,
                "graph_available": False,
                "graph_status": "partial",
                "graph_source": "persisted_sqlite",
                "graph_index": status,
                "impact_status": "unknown",
                "confidence": "low",
                "symbol_found": True,
                "symbol_source": "snapshot_exact_index",
                "snapshot_symbol": symbol_row,
                "resolution_mode": "snapshot_symbol_without_graph_definition",
                "note": (
                    "The symbol definition is available in the exact index, "
                    "but the persisted graph has no matching definition. "
                    "Deterministic references are returned as a fallback."
                ),
                "fallback_reason": "graph_symbol_not_indexed",
                **_snapshot_revision_state(workspace_id),
            }
            return _attach_snapshot_fallback(
                result,
                workspace_id=workspace_id,
                symbol=symbol,
                symbol_row=symbol_row,
            )
    result = _graph_index().impact(
        workspace_id=workspace_id.strip(),
        symbol=symbol,
        depth=depth,
        symbol_path=str(symbol_path) if symbol_path else None,
    )
    result.update(
        {
            "depth": depth,
            "graph_available": True,
            "graph_status": "ready",
            "graph_source": "persisted_sqlite",
            "graph_index": status,
            "confidence": (
                "medium"
                if result.get("resolution_mode") == "file_symbol_aggregate"
                else "high"
            ),
            **_snapshot_revision_state(workspace_id),
        }
    )
    if symbol_row:
        result["symbol_found"] = True
        result["symbol_source"] = "snapshot_exact_index"
        result["snapshot_symbol"] = symbol_row
    if result.get("resolution_mode") == "unsupported_symbol_language":
        result.update(
            {
                "graph_available": False,
                "graph_status": "unsupported",
                "impact_status": "unknown",
                "confidence": "low",
                "note": (
                    str(result.get("reason"))
                    if result.get("reason")
                    else "The persisted graph index is ready, but this symbol's "
                    "language is not supported by graph analysis."
                ),
            }
        )
        return _attach_snapshot_fallback(
            result,
            workspace_id=workspace_id,
            symbol=symbol,
            symbol_row=symbol_row,
        )
    ambiguous = list(result.get("ambiguous_seed_symbols") or [])
    if ambiguous:
        result["graph_warnings"] = [
            "Ambiguous member names were excluded from graph traversal: "
            + ", ".join(ambiguous[:20])
        ]
    has_relation_evidence = bool(
        result.get("direct_callers")
        or result.get("direct_callees")
        or (result.get("inheritance") or {}).get("edges")
    )
    evidence_providers = set(result.get("evidence_providers") or [])
    definitions = list(result.get("definitions") or [])
    symbol_path_norm = (
        normalize_snapshot_path(str(symbol_path))
        if symbol_path
        else None
    )
    seed_definition_providers = {
        str(row.get("source_provider") or "")
        for row in definitions
        if (
            str(row.get("name") or "") == symbol
            or str(row.get("qualified_name") or "") == symbol
        )
        and (
            not symbol_path_norm
            or normalize_snapshot_path(str(row.get("path") or "")) == symbol_path_norm
        )
        and row.get("source_provider")
    }
    seed_scala_lexical_only = bool(
        seed_definition_providers
        and seed_definition_providers <= {"scala_lexical_fallback"}
    )
    lexical_only = bool(
        evidence_providers
        and evidence_providers <= {"scala_lexical_fallback"}
    )
    if result.get("found") and seed_scala_lexical_only:
        result.update(
            {
                "graph_available": False,
                "graph_status": "partial",
                "impact_status": "unknown",
                "confidence": "low",
                "note": (
                    "The target symbol is indexed through the Scala lexical "
                    "fallback. Related references are useful hints, but they "
                    "are not high-confidence graph evidence."
                ),
                "fallback_reason": "scala_lexical_graph_evidence",
            }
        )
        return _attach_snapshot_fallback(
            result,
            workspace_id=workspace_id,
            symbol=symbol,
            symbol_row=symbol_row,
        )
    if result.get("found") and has_relation_evidence and not lexical_only:
        result["impact_status"] = "available"
    elif result.get("found"):
        result.update(
            {
                "graph_available": False,
                "graph_status": "partial",
                "impact_status": "unknown",
                "confidence": "low" if lexical_only else "medium",
                "note": (
                    "The symbol definition is indexed, but the current graph "
                    "has no high-confidence call/inheritance evidence. "
                    "Deterministic references are returned as a fallback."
                ),
                "fallback_reason": "graph_relation_evidence_incomplete",
            }
        )
        return _attach_snapshot_fallback(
            result,
            workspace_id=workspace_id,
            symbol=symbol,
            symbol_row=symbol_row,
        )
    else:
        result.update(
            {
                "graph_available": False if symbol_row else result.get("graph_available"),
                "graph_status": (
                    "partial" if symbol_row else result.get("graph_status", "ready")
                ),
                "impact_status": "unknown",
                "confidence": "low",
                "note": (
                    str(result.get("reason"))
                    if result.get("reason")
                    else "The persisted graph is current, but it contains no "
                    "call edges for this symbol. Deterministic references "
                    "should be used as a fallback."
                ),
                "fallback_reason": "graph_symbol_not_indexed",
            }
        )
        if symbol_row:
            return _attach_snapshot_fallback(
                result,
                workspace_id=workspace_id,
                symbol=symbol,
                symbol_row=symbol_row,
            )
    return result


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


def _snapshot_symbol_path(symbol_row: Optional[dict[str, Any]]) -> Optional[str]:
    if not symbol_row:
        return None
    value = symbol_row.get("file_path") or symbol_row.get("file")
    return str(value) if value else None


def _is_test_like_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = Path(normalized).name
    parts = set(normalized.split("/"))
    return (
        "test" in parts
        or "tests" in parts
        or "src/test" in normalized
        or name.startswith("test_")
        or name in {"test.py", "tests.py"}
        or name.endswith(
            (
                "_test.py",
                "_tests.py",
                "test.java",
                "tests.java",
                "test.scala",
                "tests.scala",
                ".spec.js",
                ".spec.ts",
                ".test.js",
                ".test.ts",
            )
        )
    )


def _snapshot_reference_fallback(
    *,
    workspace_id: Optional[str],
    symbol: str,
    symbol_row: Optional[dict[str, Any]],
    max_results: int = 40,
) -> dict[str, Any]:
    """Return deterministic references/test hints from the exact text index.

    This is intentionally lightweight: it never scans the live workspace and
    never depends on the semantic or graph indexes. It lets AI editors continue
    with verifiable line-level evidence when graph parsing is unsupported for a
    language such as Scala.
    """
    if not workspace_id or not symbol.strip():
        return {
            "references": [],
            "test_candidates": [],
            "reference_source": "none",
            "test_candidate_source": "none",
        }
    references: list[dict[str, Any]] = []
    test_candidates: set[str] = set()
    defining_path = _snapshot_symbol_path(symbol_row)
    if defining_path and _is_test_like_path(defining_path):
        test_candidates.add(defining_path)
    if symbol_row and defining_path:
        references.append(
            {
                "file": defining_path,
                "line": int(symbol_row.get("line_start") or 1),
                "snippet": str(symbol_row.get("signature") or "").strip(),
                "source": "snapshot_exact_symbol",
                "confidence": "high",
                "is_definition": True,
                "hash": symbol_row.get("hash"),
                "revision": symbol_row.get("revision"),
            }
        )
    try:
        exact_status = _exact_index().status(workspace_id=workspace_id.strip())
    except Exception:
        exact_status = {}
    fts_available = bool(exact_status.get("line_fts_available", False))
    line_count = int(exact_status.get("lines") or 0)
    max_scan_lines = _graph_fallback_text_max_lines()
    if (
        not fts_available
        and max_scan_lines > 0
        and line_count > max_scan_lines
    ):
        return {
            "references": references,
            "test_candidates": sorted(test_candidates),
            "reference_source": (
                "snapshot_exact_symbol" if references else "none"
            ),
            "test_candidate_source": "path_heuristic" if test_candidates else "none",
            "fallback_warning": (
                "exact text reference scan skipped because line FTS is unavailable "
                f"and exact index has {line_count} lines"
            ),
            "line_fts_available": fts_available,
            "line_count": line_count,
            "max_scan_lines": max_scan_lines,
        }
    try:
        rows = _exact_index().search_text(
            workspace_id=workspace_id.strip(),
            query=symbol.strip(),
            file_pattern=None,
            use_regex=False,
            case_sensitive=True,
            max_results=max_results,
            context_lines=1,
        )
    except Exception as exc:
        return {
            "references": [],
            "test_candidates": sorted(test_candidates),
            "reference_source": "snapshot_exact_text_error",
            "test_candidate_source": "path_heuristic" if test_candidates else "none",
            "fallback_error": f"{exc.__class__.__name__}: {exc}",
        }
    seen: set[tuple[str, int]] = set()
    if references:
        seen.update(
            (str(row.get("file") or ""), int(row.get("line") or 0))
            for row in references
        )
    for row in rows:
        key = (row.path, row.line_no)
        if key in seen:
            continue
        seen.add(key)
        is_definition = (
            bool(defining_path)
            and row.path == defining_path
            and int(symbol_row.get("line_start") or 0) == row.line_no
        )
        references.append(
            {
                "file": row.path,
                "line": row.line_no,
                "snippet": row.line_text.strip(),
                "source": "snapshot_exact_text",
                "confidence": "medium",
                "is_definition": is_definition,
                "hash": row.hash,
                "revision": row.revision,
            }
        )
        if _is_test_like_path(row.path):
            test_candidates.add(row.path)
    return {
        "references": references,
        "test_candidates": sorted(test_candidates),
        "reference_source": (
            "snapshot_exact_text"
            if any(row.get("source") == "snapshot_exact_text" for row in references)
            else "snapshot_exact_symbol"
            if references
            else "none"
        ),
        "test_candidate_source": "path_heuristic" if test_candidates else "none",
        "line_fts_available": fts_available,
        "line_count": line_count,
        "max_scan_lines": max_scan_lines,
    }


def _attach_snapshot_fallback(
    result: dict[str, Any],
    *,
    workspace_id: Optional[str],
    symbol: str,
    symbol_row: Optional[dict[str, Any]],
    max_results: int = 40,
) -> dict[str, Any]:
    fallback = _snapshot_reference_fallback(
        workspace_id=workspace_id,
        symbol=symbol,
        symbol_row=symbol_row,
        max_results=max_results,
    )
    result.setdefault("fallback_used", True)
    result.setdefault("fallback_reason", "graph_index_unavailable")
    result["fallback"] = {
        "reason": result.get("fallback_reason") or "graph_index_unavailable",
        "references": fallback["references"],
        "test_candidates": fallback["test_candidates"],
        "reference_source": fallback["reference_source"],
        "test_candidate_source": fallback["test_candidate_source"],
    }
    if fallback.get("fallback_error"):
        result["fallback"]["error"] = fallback["fallback_error"]
    if fallback.get("fallback_warning"):
        result["fallback"]["warning"] = fallback["fallback_warning"]
    for key in ("line_fts_available", "line_count", "max_scan_lines"):
        if key in fallback:
            result["fallback"][key] = fallback[key]
    result["references"] = fallback["references"]
    result["test_candidates"] = fallback["test_candidates"]
    return result


def _snapshot_graph_unavailable_impact(
    *,
    workspace_id: Optional[str],
    symbol: str,
    depth: int,
    symbol_row: dict[str, Any],
    graph_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fast deterministic fallback for snapshot workspaces without graph data.

    Large snapshot workspaces must not fall through to the legacy
    ImpactAnalyzer scan when the exact index has already proven that the
    symbol exists. The honest answer is "symbol found, graph unavailable",
    not a slow rescan and not ``symbol not found``.
    """
    result = {
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
        "graph_index": graph_status or _persisted_graph_status(workspace_id),
        "fallback_used": True,
        "fallback_reason": "graph_index_unavailable",
    }
    result = _mark_snapshot_graph_unknown(
        result,
        workspace_id=workspace_id,
        symbol_row=symbol_row,
    )
    return _attach_snapshot_fallback(
        result,
        workspace_id=workspace_id,
        symbol=symbol,
        symbol_row=symbol_row,
    )


def _snapshot_graph_unavailable_risk(
    *,
    workspace_id: Optional[str],
    symbol: str,
    symbol_row: dict[str, Any],
    graph_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    result = {
        "symbol": symbol,
        "risk": "unknown",
        "risk_score": None,
        "reasons": ["Call graph is not available for this snapshot workspace"],
        "direct_callers": 0,
        "files_affected": 0,
        "test_coverage": 0,
        "suggested_checks": [],
        "graph_available": False,
        "graph_status": "unavailable",
        "impact_status": "unknown",
        "confidence": "low",
        "symbol_found": True,
        "symbol_source": "snapshot_store",
        "snapshot_symbol": symbol_row,
        "graph_index": graph_status or _persisted_graph_status(workspace_id),
        "fallback_used": True,
        "fallback_reason": "graph_index_unavailable",
        **_snapshot_revision_state(workspace_id),
    }
    return _attach_snapshot_fallback(
        result,
        workspace_id=workspace_id,
        symbol=symbol,
        symbol_row=symbol_row,
    )


def _snapshot_graph_unavailable_related_tests(
    *,
    workspace_id: Optional[str],
    symbol: str,
    symbol_row: dict[str, Any],
    max_files: int = 200,
    graph_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    fallback = _snapshot_reference_fallback(
        workspace_id=workspace_id,
        symbol=symbol,
        symbol_row=symbol_row,
        max_results=max_files,
    )
    test_files = fallback["test_candidates"]
    return {
        "symbol": symbol,
        "test_files": test_files,
        "count": len(test_files),
        "suggested_commands": [f"pytest {path}" for path in test_files[:5]],
        "graph_available": False,
        "graph_status": "unavailable",
        "confidence": "low",
        "symbol_found": True,
        "symbol_source": "snapshot_store",
        "snapshot_symbol": symbol_row,
        "graph_index": graph_status or _persisted_graph_status(workspace_id),
        "fallback_used": True,
        "fallback_reason": "graph_index_unavailable",
        "reference_source": fallback["reference_source"],
        "test_candidate_source": fallback["test_candidate_source"],
        "references": fallback["references"],
        "note": (
            "Symbol exists in the cloud snapshot, but graph-linked test "
            "coverage is unavailable for this snapshot workspace. Returned "
            "deterministic path/reference heuristics instead."
        ),
        **_snapshot_revision_state(workspace_id),
    }


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
        allow_graph_fresh=True,
    )
    if stale is not None:
        return stale
    symbol_row = _snapshot_symbol_row(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
    )
    persisted = _persisted_graph_impact(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
        depth=depth,
        symbol_row=symbol_row,
    )
    if persisted is not None:
        return create_success_response(persisted)
    if x_omnicode_workspace and symbol_row:
        return create_success_response(
            _snapshot_graph_unavailable_impact(
                workspace_id=x_omnicode_workspace,
                symbol=symbol,
                depth=depth,
                symbol_row=symbol_row,
            )
        )

    result = await _build().get_impact_radius(
        symbol=symbol, depth=depth, max_files=max_files, scope_path=scope_path
    )
    if "error" in result:
        return create_error_response(result["error"], 500)
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
        allow_graph_fresh=True,
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
        allow_graph_fresh=True,
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
        allow_graph_fresh=True,
    )
    if stale is not None:
        return stale
    symbol_row = _snapshot_symbol_row(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
    )
    graph_status = _persisted_graph_status(x_omnicode_workspace)
    if x_omnicode_workspace and graph_status.get("ready"):
        symbol_path = _snapshot_symbol_path(symbol_row)
        test_files = _graph_index().related_tests(
            workspace_id=x_omnicode_workspace.strip(),
            symbol=symbol,
            symbol_path=str(symbol_path) if symbol_path else None,
            max_results=min(max_files, 200),
        )
        return create_success_response(
            {
                "symbol": symbol,
                "test_files": test_files,
                "count": len(test_files),
                "suggested_commands": [
                    f"pytest {path}" for path in test_files[:5]
                ],
                "graph_available": True,
                "graph_status": "ready",
                "graph_source": "persisted_sqlite",
                "graph_index": graph_status,
            }
        )
    if x_omnicode_workspace and symbol_row:
        return create_success_response(
            _snapshot_graph_unavailable_related_tests(
                workspace_id=x_omnicode_workspace,
                symbol=symbol,
                symbol_row=symbol_row,
                graph_status=graph_status,
            )
        )

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
        allow_graph_fresh=True,
    )
    if stale is not None:
        return stale
    symbol_row = _snapshot_symbol_row(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
    )
    persisted = _persisted_graph_impact(
        workspace_id=x_omnicode_workspace,
        symbol=symbol,
        depth=2,
        symbol_row=symbol_row,
    )
    if persisted is not None and persisted.get("found"):
        test_files = _graph_index().related_tests(
            workspace_id=x_omnicode_workspace.strip(),
            symbol=symbol,
            symbol_path=(
                str(symbol_row.get("file_path") or symbol_row.get("file"))
                if symbol_row
                and (symbol_row.get("file_path") or symbol_row.get("file"))
                else None
            ),
        )
        caller_count = int(persisted.get("dependent_count") or 0)
        file_count = int(persisted.get("files_count") or 0)
        score = 0
        reasons: list[str] = []
        if caller_count > 10:
            score += 3
            reasons.append(f"High caller count ({caller_count})")
        elif caller_count > 3:
            score += 2
            reasons.append(f"Moderate caller count ({caller_count})")
        elif caller_count > 0:
            score += 1
        if file_count > 5:
            score += 2
            reasons.append(f"Affects {file_count} files")
        elif file_count > 2:
            score += 1
        if not test_files:
            score += 2
            reasons.append("No graph-linked test coverage found")
        elif len(test_files) < 2:
            score += 1
            reasons.append("Limited graph-linked test coverage")
        return create_success_response(
            {
                "symbol": symbol,
                "risk": "high" if score >= 5 else "medium" if score >= 3 else "low",
                "risk_score": score,
                "reasons": reasons,
                "direct_callers": caller_count,
                "files_affected": file_count,
                "test_coverage": len(test_files),
                "suggested_checks": [
                    f"pytest {path}" for path in test_files[:5]
                ],
                "graph_available": True,
                "graph_status": "ready",
                "graph_source": "persisted_sqlite",
                "graph_index": persisted.get("graph_index"),
                "confidence": persisted.get("confidence"),
            }
        )
    if x_omnicode_workspace and symbol_row:
        return create_success_response(
            _snapshot_graph_unavailable_risk(
                workspace_id=x_omnicode_workspace,
                symbol=symbol,
                symbol_row=symbol_row,
            )
        )

    result = await _build().assess_risk(symbol=symbol, max_files=max_files)
    if "error" in result:
        return create_error_response(result["error"], 500)
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
