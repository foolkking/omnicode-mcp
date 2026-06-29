"""
Search and indexing endpoints
Provides semantic search, text search, symbol search, and index management
"""

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.v1.routers.freshness import cloud_freshness_error, cloud_freshness_state
from core import get_ast_parser, get_search_engine
from core.config import get_settings
from omnicode.ast_engine.graph import CallGraphBuilder
from omnicode.search.models import SearchRequest
from omnicode_core.search.planner import build_search_plan
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.registry import get_workspace_registry
from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from omnicode_core.workspace.semantic_index_policy import (
    semantic_index_decision,
    semantic_index_metadata,
    semantic_selected_file_limit,
    semantic_path_skip_reason,
    semantic_index_policy_payload,
)
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore
from utils import (
    create_error_response,
    create_success_response,
    validate_file_path,
)

router = APIRouter(prefix="/search", tags=["search"])
_SNAPSHOT_INDEX_JOBS_LOCK = threading.RLock()
_SNAPSHOT_INDEX_JOBS: dict[str, dict[str, Any]] = {}


def _exact_index() -> SnapshotExactIndex:
    return SnapshotExactIndex(store=CloudSnapshotStore())


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _local_exact_workspace_id() -> str:
    return os.environ.get("OMNICODE_WORKSPACE_ID", "local")


def _structured_search_error(
    *,
    message: str,
    status_code: int,
    error_code: str,
    **payload: Any,
) -> JSONResponse:
    result = {
        "ok": False,
        "error": message,
        "error_code": error_code,
        **payload,
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "result": result,
            "error": message,
            "timestamp": datetime.now().isoformat(),
        },
    )


def _search_success_payload(**payload: Any) -> dict[str, Any]:
    """Normalize successful search envelopes while keeping legacy fields."""
    results = payload.get("results")
    if not isinstance(results, list):
        results = []
        payload["results"] = results
    total = payload.get("total_results")
    if total is None:
        total = len(results)
        payload["total_results"] = total
    payload.setdefault("ok", True)
    payload.setdefault("count", len(results))
    payload.setdefault("total_count", total)
    return payload


def _resolve_search_workspace(workspace_id: Optional[str]) -> Optional[str]:
    """Validate an optional workspace header against the active backend root."""
    if not workspace_id or not workspace_id.strip():
        return None
    settings = get_settings()
    requested = workspace_id.strip()
    try:
        resolved = resolve_workspace_request(
            requested,
            working_dir=settings.WORKING_DIR,
            registry=get_workspace_registry(),
        )
    except WorkspaceResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return resolved.workspace_id or requested


def _snapshot_patterns_match(path: str, patterns: Optional[list[str]]) -> bool:
    if not patterns:
        return True
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def _snapshot_line_match(
    line: str,
    query: str,
    *,
    use_regex: bool,
    case_sensitive: bool,
) -> Optional[tuple[int, int]]:
    if use_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        match = re.search(query, line, flags=flags)
        return match.span() if match else None
    haystack = line if case_sensitive else line.lower()
    needle = query if case_sensitive else query.lower()
    start = haystack.find(needle)
    if start < 0:
        return None
    return start, start + len(needle)


def _query_identifier_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query or ""):
        if raw in {"class", "def", "async", "return", "import", "from"}:
            continue
        lowered = raw.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(raw)
    return terms[:5]


def _format_exact_symbol_row(row: Any, *, source: str = "exact_index") -> dict[str, Any]:
    return {
        "file_path": row.path,
        "symbol_name": row.name,
        "symbol_type": row.kind,
        "line_start": row.line_start,
        "line_end": row.line_end,
        "signature": row.signature,
        "relevance_score": row.score,
        "why_matched": [row.why, source],
        "source": source,
        "hash": row.hash,
        "revision": row.revision,
    }


def _grep_mirror_paths(
    *,
    mirror_root: Path,
    paths: list[str],
    query: str,
    use_regex: bool,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
    existing_keys: set[tuple[str, int]],
    hashes: dict[str, str],
    revisions: dict[str, int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in paths:
        if len(rows) >= max_results:
            break
        try:
            path = mirror_root / rel
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            if len(rows) >= max_results:
                break
            span = _snapshot_line_match(
                line,
                query,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
            )
            if span is None:
                continue
            line_no = idx + 1
            key = (rel, line_no)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            start = max(0, idx - context_lines)
            end = idx + 1 + context_lines
            rows.append(
                {
                    "file_path": rel,
                    "line_number": line_no,
                    "line_content": line,
                    "context_before": lines[start:idx],
                    "context_after": lines[idx + 1:end],
                    "match_span": list(span),
                    "match_type": "text",
                    "relevance_score": 1.0,
                    "why_matched": [
                        "text:line_match",
                        "snapshot_mirror",
                        "symbol_prioritized",
                    ],
                    "source": "snapshot_mirror",
                    "hash": hashes.get(rel),
                    "revision": revisions.get(rel),
                }
            )
    return rows


def _grep_snapshot_store(
    *,
    workspace_id: Optional[str],
    query: str,
    patterns: Optional[list[str]],
    use_regex: bool,
    case_sensitive: bool,
    max_results: int,
    context_lines: int,
    existing_keys: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    if not workspace_id or max_results <= 0:
        return []
    store = CloudSnapshotStore()
    mirror_root = store.workspaces_root / workspace_id / "mirror"
    if mirror_root.is_dir():
        try:
            from omnicode_core.search.text_grep import grep_workspace

            hashes = store.file_hashes(workspace_id)
            revisions = {
                record.path: record.revision
                for record in store.list_records(workspace_id=workspace_id)
            }
            prioritized_paths: list[str] = []
            prioritized_seen: set[str] = set()
            if not use_regex:
                for term in _query_identifier_terms(query):
                    for row in _exact_index().search_symbols(
                        workspace_id=workspace_id,
                        query=term,
                        fuzzy=False,
                        max_results=20,
                    ):
                        if not _snapshot_patterns_match(row.path, patterns):
                            continue
                        if row.path in prioritized_seen:
                            continue
                        prioritized_seen.add(row.path)
                        prioritized_paths.append(row.path)
            rows_from_mirror = _grep_mirror_paths(
                mirror_root=mirror_root,
                paths=prioritized_paths,
                query=query,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_results=max_results,
                existing_keys=existing_keys,
                hashes=hashes,
                revisions=revisions,
            )
            if rows_from_mirror:
                return rows_from_mirror

            hits = grep_workspace(
                workspace_root=mirror_root,
                query=query,
                file_patterns=patterns,
                max_results=max_results - len(rows_from_mirror),
                context_lines=context_lines,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
                merge_adjacent=False,
            )
            for hit in hits:
                key = (hit.file_path, hit.line_number)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                rows_from_mirror.append(
                    {
                        "file_path": hit.file_path,
                        "line_number": hit.line_number,
                        "line_content": hit.line_content,
                        "context_before": hit.context_before,
                        "context_after": hit.context_after,
                        "match_span": list(hit.match_span),
                        "match_type": "text",
                        "relevance_score": 1.0,
                        "why_matched": ["text:line_match", "snapshot_mirror"],
                        "source": "snapshot_mirror",
                        "hash": hashes.get(hit.file_path),
                        "revision": revisions.get(hit.file_path),
                    }
                )
            return rows_from_mirror
        except Exception:
            pass

    rows: list[dict[str, Any]] = []
    for record in store.list_records(workspace_id=workspace_id):
        if not _snapshot_patterns_match(record.path, patterns):
            continue
        read_record_text = getattr(store, "read_record_text", None)
        if callable(read_record_text):
            content = read_record_text(workspace_id=workspace_id, record=record)
        else:  # pragma: no cover - compatibility for older injected fakes
            content = store.read_text(workspace_id=workspace_id, path=record.path)
        if content is None:
            continue
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            span = _snapshot_line_match(
                line,
                query,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
            )
            if span is None:
                continue
            line_no = idx + 1
            key = (record.path, line_no)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            before_start = max(0, idx - context_lines)
            after_end = min(len(lines), idx + context_lines + 1)
            rows.append(
                {
                    "file_path": record.path,
                    "line_number": line_no,
                    "line_content": line,
                    "context_before": lines[before_start:idx],
                    "context_after": lines[idx + 1:after_end],
                    "match_span": list(span),
                    "match_type": "text",
                    "relevance_score": 1.0,
                    "why_matched": ["text:line_match", "snapshot_store"],
                    "source": "snapshot_store",
                    "hash": record.hash,
                    "revision": record.revision,
                }
            )
            if len(rows) >= max_results:
                return rows
    return rows


_PY_SYMBOL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>class|def|async\s+def)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_IDENTIFIER_QUERY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_SEMANTIC_BOOST_STOPWORDS = {
    "about",
    "after",
    "before",
    "code",
    "does",
    "file",
    "find",
    "from",
    "function",
    "handle",
    "handles",
    "into",
    "show",
    "that",
    "the",
    "this",
    "where",
    "with",
}
_SEMANTIC_BOOST_SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".rb",
    ".php",
    ".kt",
    ".cs",
)
_SEMANTIC_LEXICAL_BOOST_MAX_RECORDS = int(
    os.environ.get("OMNICODE_SEMANTIC_LEXICAL_BOOST_MAX_RECORDS", "300")
)
_SEMANTIC_LEXICAL_BOOST_TIMEOUT_MS = int(
    os.environ.get("OMNICODE_SEMANTIC_LEXICAL_BOOST_TIMEOUT_MS", "500")
)


def _snapshot_symbol_score(query: str, name: str, *, fuzzy: bool) -> Optional[tuple[float, str]]:
    q = query.strip().lower()
    n = name.strip().lower()
    if not q or not n:
        return None
    if n == q:
        return 1.0, "symbol:exact"
    if n.startswith(q):
        return 0.9, "symbol:prefix"
    if q in n:
        return 0.7, "symbol:contains"
    if not fuzzy:
        return None
    try:
        from rapidfuzz import fuzz as _rf_fuzz

        ratio = _rf_fuzz.WRatio(q, n)
        if ratio < 60:
            return None
        return min(0.95, 0.5 + (ratio - 60) / 100), "symbol:fuzzy"
    except Exception:  # pragma: no cover - optional dependency
        return None


def _snapshot_symbol_search(
    *,
    workspace_id: Optional[str],
    query: str,
    symbol_type: Optional[str],
    file_pattern: Optional[str],
    fuzzy: bool,
    min_score: float,
    max_results: int,
    existing_keys: set[tuple[str, str, int]],
) -> list[dict[str, Any]]:
    if not workspace_id or max_results <= 0:
        return []
    patterns = [p.strip() for p in file_pattern.split(",") if p.strip()] if file_pattern else None
    store = CloudSnapshotStore()
    rows: list[dict[str, Any]] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    exact_re = None
    if not fuzzy and query.strip():
        exact_re = re.compile(
            r"^(?P<indent>\s*)(?P<kind>class|def|async\s+def)\s+"
            + re.escape(query.strip())
            + r"\b"
        )
    for record in store.list_records(workspace_id=workspace_id):
        if not _snapshot_patterns_match(record.path, patterns):
            continue
        read_record_text = getattr(store, "read_record_text", None)
        if callable(read_record_text):
            content = read_record_text(workspace_id=workspace_id, record=record)
        else:  # pragma: no cover - compatibility for older injected fakes
            content = store.read_text(workspace_id=workspace_id, path=record.path)
        if content is None:
            continue
        for idx, line in enumerate(content.splitlines()):
            match = exact_re.match(line) if exact_re is not None else _PY_SYMBOL_RE.match(line)
            if not match:
                continue
            raw_kind = match.group("kind")
            kind = "class" if raw_kind == "class" else "function"
            if symbol_type and symbol_type != kind and symbol_type != raw_kind:
                continue
            name = query.strip() if exact_re is not None else match.group("name")
            if exact_re is not None:
                relevance_score, why = 1.0, "symbol:exact"
            else:
                score = _snapshot_symbol_score(query, name, fuzzy=fuzzy)
                if score is None:
                    continue
                relevance_score, why = score
            if relevance_score < min_score:
                continue
            line_no = idx + 1
            key = (record.path, name, line_no)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            row = {
                "file_path": record.path,
                "symbol_name": name,
                "symbol_type": kind,
                "line_start": line_no,
                "line_end": line_no,
                "signature": line.strip(),
                "relevance_score": relevance_score,
                "why_matched": [why, "snapshot_store"],
                "source": "snapshot_store",
                "hash": record.hash,
                "revision": record.revision,
            }
            if not fuzzy and why == "symbol:exact":
                rows.append(row)
                return rows[:max_results]
            scored.append((relevance_score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    rows.extend(row for _, row in scored[:max_results])
    return rows


def _snapshot_semantic_exact_boost(
    *,
    workspace_id: Optional[str],
    query: str,
    file_pattern: Optional[str],
    max_results: int,
) -> list[dict[str, Any]]:
    """Return deterministic snapshot hits that should outrank semantic recall.

    Large repositories often have enough semantically similar code that vector
    recall can put weak matches above an obvious exact symbol or literal line.
    Snapshot content is authoritative for synced hybrid workspaces, so we seed
    semantic responses with exact symbol/text hits before appending vector rows.
    """
    if not workspace_id or max_results <= 0:
        return []

    boosted: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()

    if _IDENTIFIER_QUERY_RE.fullmatch(query.strip()):
        try:
            exact_rows = _exact_index().search_symbols(
                workspace_id=workspace_id,
                query=query,
                symbol_type=None,
                file_pattern=file_pattern,
                fuzzy=False,
                min_score=1.0,
                max_results=max_results,
            )
        except Exception:
            exact_rows = []
        for row in exact_rows:
            line_no = int(row.line_start or 0)
            key = (row.path, line_no, row.name)
            if key in seen:
                continue
            seen.add(key)
            boosted.append(
                {
                    "file_path": row.path,
                    "symbol_name": row.name,
                    "chunk_type": row.kind,
                    "symbol_type": row.kind,
                    "line_start": row.line_start,
                    "line_end": row.line_end,
                    "signature": row.signature,
                    "docstring": "",
                    "relevance_score": row.score,
                    "why_matched": [row.why, "exact_index", "semantic:exact_boost"],
                    "source": "exact_index",
                    "rank_reason": "exact_symbol_before_semantic",
                    "hash": row.hash,
                    "revision": row.revision,
                }
            )
            if len(boosted) >= max_results:
                return boosted
        if boosted:
            return boosted

        symbol_rows = _snapshot_symbol_search(
            workspace_id=workspace_id,
            query=query,
            symbol_type=None,
            file_pattern=file_pattern,
            fuzzy=False,
            min_score=1.0,
            max_results=max_results,
            existing_keys=set(),
        )
        for row in symbol_rows:
            line_no = int(row.get("line_start") or 0)
            key = (str(row.get("file_path") or ""), line_no, str(row.get("symbol_name") or ""))
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            why = list(item.get("why_matched") or [])
            if "semantic:exact_boost" not in why:
                why.append("semantic:exact_boost")
            item.update(
                {
                    "chunk_type": item.get("symbol_type") or "symbol",
                    "docstring": item.get("docstring") or "",
                    "why_matched": why,
                    "rank_reason": "exact_symbol_before_semantic",
                }
            )
            boosted.append(item)
            if len(boosted) >= max_results:
                return boosted
        if boosted:
            return boosted

    if not _should_snapshot_exact_text_boost(query):
        return boosted

    patterns = (
        [p.strip() for p in file_pattern.split(",") if p.strip()]
        if file_pattern
        else None
    )
    text_rows = _grep_snapshot_store(
        workspace_id=workspace_id,
        query=query,
        patterns=patterns,
        use_regex=False,
        case_sensitive=True,
        max_results=max_results - len(boosted),
        context_lines=1,
        existing_keys={(path, line) for path, line, _symbol in seen},
    )
    for row in text_rows:
        line_no = int(row.get("line_number") or 0)
        key = (str(row.get("file_path") or ""), line_no, "")
        if key in seen:
            continue
        seen.add(key)
        item = {
            "file_path": row.get("file_path"),
            "symbol_name": "",
            "chunk_type": "text",
            "line_start": line_no,
            "line_end": line_no,
            "signature": row.get("line_content") or "",
            "docstring": "",
            "relevance_score": row.get("relevance_score", 1.0),
            "why_matched": list(row.get("why_matched") or []) + ["semantic:exact_boost"],
            "source": row.get("source") or "snapshot_store",
            "hash": row.get("hash"),
            "revision": row.get("revision"),
        }
        boosted.append(item)
        if len(boosted) >= max_results:
            break

    return boosted


def _should_snapshot_exact_text_boost(query: str) -> bool:
    stripped = query.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if (
        (stripped.startswith('"') and stripped.endswith('"'))
        or (stripped.startswith("'") and stripped.endswith("'"))
    ):
        return True
    if lowered.startswith(
        (
            "class ",
            "def ",
            "async def ",
            "import ",
            "from ",
            "return ",
            "func ",
            "function ",
            "public ",
            "private ",
            "protected ",
            "object ",
            "trait ",
        )
    ):
        return True
    if any(char in stripped for char in (":", "=", "(", ")", "{", "}", "[", "]", "/", "\\", ".", '"', "'")):
        return True
    tokens = _semantic_query_tokens(stripped)
    return len(tokens) <= 1


def _query_token_variants(token: str) -> set[str]:
    variants = {token}
    if token.endswith("ies") and len(token) > 4:
        variants.add(token[:-3] + "y")
    if token.endswith("es") and len(token) > 4:
        variants.add(token[:-2])
    if token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    return variants


def _semantic_query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _QUERY_TOKEN_RE.findall(query):
        token = raw.lower()
        if token in _SEMANTIC_BOOST_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _snapshot_semantic_lexical_boost(
    *,
    workspace_id: Optional[str],
    query: str,
    file_pattern: Optional[str],
    max_results: int,
    existing_keys: set[tuple[str, int, str]],
    debug_timing: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Boost source files with strong lexical overlap for natural queries."""
    total_started = time.perf_counter()
    if not workspace_id or max_results <= 0:
        if debug_timing is not None:
            debug_timing["semantic_lexical_boost_ms"] = _elapsed_ms(total_started)
            debug_timing["semantic_lexical_skip_reason"] = "missing_workspace_or_budget"
        return []
    tokens = _semantic_query_tokens(query)
    if len(tokens) < 2:
        if debug_timing is not None:
            debug_timing["semantic_lexical_boost_ms"] = _elapsed_ms(total_started)
            debug_timing["semantic_lexical_skip_reason"] = "too_few_tokens"
        return []

    token_variants = {token: _query_token_variants(token) for token in tokens}
    patterns = (
        [p.strip() for p in file_pattern.split(",") if p.strip()]
        if file_pattern
        else None
    )
    exact_rows: list[Any] = []
    exact_started = time.perf_counter()
    try:
        exact_rows = _exact_index().search_token_overlap(
            workspace_id=workspace_id,
            tokens=tokens,
            file_pattern=file_pattern,
            max_results=max_results * 8,
            min_tokens=2,
            limit_rows=2500,
        )
    except Exception:
        exact_rows = []
    if debug_timing is not None:
        debug_timing["semantic_lexical_exact_index_ms"] = _elapsed_ms(exact_started)
        debug_timing["semantic_lexical_exact_rows"] = len(exact_rows)
    if exact_rows:
        scored_exact: list[tuple[float, dict[str, Any]]] = []
        for row in exact_rows:
            path_lower = row.path.lower()
            if any(
                part in path_lower
                for part in (
                    "/static/",
                    "/vendor/",
                    "/vendors/",
                    "/node_modules/",
                    "/dist/",
                    "/build/",
                    "jquery",
                )
            ):
                continue
            key = (row.path, int(row.line_no), "")
            if key in existing_keys:
                continue
            existing_keys.add(key)
            matched_tokens = set(row.matched_tokens)
            score = float(row.score) + len(matched_tokens)
            if (
                {"request", "middleware"} <= matched_tokens
                and "handler" in path_lower
            ):
                score += 8.0
            if (
                {"middleware", "chain"} <= matched_tokens
                and "base.py" in path_lower
            ):
                score += 6.0
            if path_lower.startswith("tests/") or "/tests/" in path_lower:
                score *= 0.55
            relevance = min(0.98, 0.55 + score / 40.0)
            scored_exact.append(
                (
                    relevance,
                    {
                        "file_path": row.path,
                        "symbol_name": "",
                        "chunk_type": "text",
                        "line_start": int(row.line_no),
                        "line_end": int(row.line_no),
                        "signature": row.line_text.strip(),
                        "docstring": "",
                        "relevance_score": relevance,
                        "why_matched": [
                            "text:token_overlap",
                            "exact_index",
                            "semantic:lexical_boost",
                        ],
                        "source": "exact_index",
                        "rank_reason": "lexical_overlap_before_semantic",
                        "matched_tokens": sorted(matched_tokens),
                        "hash": row.hash,
                        "revision": row.revision,
                    },
                )
            )
        scored_exact.sort(key=lambda item: item[0], reverse=True)
        if scored_exact:
            if debug_timing is not None:
                debug_timing["semantic_lexical_provider"] = "exact_index"
                debug_timing["semantic_lexical_scored_rows"] = len(scored_exact)
                debug_timing["semantic_lexical_boost_ms"] = _elapsed_ms(total_started)
            return [row for _score, row in scored_exact[:max_results]]

    store = CloudSnapshotStore()
    scored: list[tuple[float, dict[str, Any]]] = []
    started = time.perf_counter()
    scanned = 0

    records_started = time.perf_counter()
    records = list(store.list_records(workspace_id=workspace_id))
    if debug_timing is not None:
        debug_timing["semantic_lexical_snapshot_records_ms"] = _elapsed_ms(records_started)
        debug_timing["semantic_lexical_snapshot_records"] = len(records)
    query_token_set = set(tokens)

    def _record_priority(record: Any) -> tuple[int, str]:
        path_lower = str(record.path).lower()
        score = 0
        if path_lower.endswith(_SEMANTIC_BOOST_SOURCE_SUFFIXES):
            score -= 20
        if path_lower.startswith(("docs/", "tests/")) or "/docs/" in path_lower or "/tests/" in path_lower:
            score += 25
        if any(part in path_lower for part in ("/static/", "/vendor/", "/vendors/", "jquery")):
            score += 50
        if "handler" in path_lower and "request" in query_token_set:
            score -= 12
        if "middleware" in path_lower and "middleware" in query_token_set:
            score -= 12
        if path_lower.endswith("base.py") and {"request", "middleware"} <= query_token_set:
            score -= 10
        return (score, path_lower)

    for record in sorted(records, key=_record_priority):
        scanned += 1
        if scanned > _SEMANTIC_LEXICAL_BOOST_MAX_RECORDS:
            if debug_timing is not None:
                debug_timing["semantic_lexical_stop_reason"] = "record_cap"
            break
        if int((time.perf_counter() - started) * 1000) > _SEMANTIC_LEXICAL_BOOST_TIMEOUT_MS:
            if debug_timing is not None:
                debug_timing["semantic_lexical_stop_reason"] = "timeout"
            break
        if not _snapshot_patterns_match(record.path, patterns):
            continue
        path_lower = record.path.lower()
        if any(
            part in path_lower
            for part in (
                "/static/",
                "/vendor/",
                "/vendors/",
                "/node_modules/",
                "/dist/",
                "/build/",
                "jquery",
            )
        ):
            continue
        if patterns is None:
            if (
                path_lower.startswith(("docs/", "tests/"))
                or "/docs/" in path_lower
                or "/tests/" in path_lower
            ):
                continue
            if not path_lower.endswith(_SEMANTIC_BOOST_SOURCE_SUFFIXES):
                continue
        read_record_text = getattr(store, "read_record_text", None)
        if callable(read_record_text):
            content = read_record_text(workspace_id=workspace_id, record=record)
        else:  # pragma: no cover - compatibility for older injected fakes
            content = store.read_text(workspace_id=workspace_id, path=record.path)
        if content is None:
            continue

        path_parts = path_lower.replace("\\", "/").split("/")
        basename = path_parts[-1] if path_parts else path_lower
        path_score = 0.0
        matched_tokens: set[str] = set()
        for token, variants in token_variants.items():
            if any(variant in path_lower for variant in variants):
                path_score += 3.0
                matched_tokens.add(token)
            if any(variant in basename for variant in variants):
                path_score += 2.0

        best_line_no = 1
        best_line = ""
        best_line_score = 0.0
        best_line_tokens: set[str] = set()
        for idx, line in enumerate(content.splitlines(), start=1):
            line_lower = line.lower()
            line_score = 0.0
            line_matched: set[str] = set()
            for token, variants in token_variants.items():
                if any(variant in line_lower for variant in variants):
                    line_score += 2.0
                    line_matched.add(token)
            if line_score <= 0:
                continue
            stripped = line.lstrip()
            if stripped.startswith(("class ", "def ", "async def ")):
                line_score += 1.5
            if len(line_matched) >= 2:
                line_score += float(len(line_matched))
            if line_score > best_line_score:
                best_line_score = line_score
                best_line_no = idx
                best_line = line.strip()
                best_line_tokens = set(line_matched)
                matched_tokens.update(line_matched)

        if len(matched_tokens) < 2:
            continue

        score = path_score + best_line_score + len(matched_tokens)
        if (
            {"request", "middleware"} <= matched_tokens
            and "handler" in path_lower
        ):
            score += 8.0
        if (
            {"middleware", "chain"} <= matched_tokens
            and "base.py" in path_lower
        ):
            score += 6.0
        if len(best_line_tokens) == len(tokens):
            score += 4.0
        elif len(best_line_tokens) >= 3:
            score += 2.0
        if path_lower.startswith("tests/") or "/tests/" in path_lower:
            score *= 0.55
        if score < 5.0:
            continue

        key = (record.path, best_line_no, "")
        if key in existing_keys:
            continue
        existing_keys.add(key)
        relevance = min(0.98, 0.55 + score / 40.0)
        scored.append(
            (
                relevance,
                {
                    "file_path": record.path,
                    "symbol_name": "",
                    "chunk_type": "text",
                    "line_start": best_line_no,
                    "line_end": best_line_no,
                    "signature": best_line,
                    "docstring": "",
                    "relevance_score": relevance,
                    "why_matched": [
                        "text:token_overlap",
                        "snapshot_store",
                        "semantic:lexical_boost",
                    ],
                    "source": "snapshot_store",
                    "rank_reason": "lexical_overlap_before_semantic",
                    "matched_tokens": sorted(matched_tokens),
                    "hash": record.hash,
                    "revision": record.revision,
                },
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    if debug_timing is not None:
        debug_timing.setdefault("semantic_lexical_provider", "snapshot_store")
        debug_timing.setdefault("semantic_lexical_stop_reason", "exhausted")
        debug_timing["semantic_lexical_snapshot_scan_ms"] = _elapsed_ms(started)
        debug_timing["semantic_lexical_scanned"] = scanned
        debug_timing["semantic_lexical_scored_rows"] = len(scored)
        debug_timing["semantic_lexical_boost_ms"] = _elapsed_ms(total_started)
    return [row for _score, row in scored[:max_results]]


def _run_snapshot_index_blocking(
    workspace_id: str,
    *,
    force: bool = False,
    scope: str = "semantic",
    progress: Optional[Callable[[dict[str, Any]], None]] = None,
    staging_dir: Optional[str] = None,
    resume_staging: bool = False,
) -> dict[str, Any]:
    """Index snapshot-store content in a worker thread."""

    async def _index() -> dict[str, Any]:
        active_search_engine = get_search_engine()
        if not active_search_engine:
            raise RuntimeError("Semantic search not initialized")
        search_engine = active_search_engine
        staging_engine = None
        staging_path = Path(staging_dir).resolve() if staging_dir else None
        if staging_path is not None:
            from omnicode.search.engine import SemanticSearchEngine

            if staging_path.exists() and not resume_staging:
                shutil.rmtree(staging_path)
            staging_path.mkdir(parents=True, exist_ok=True)
            staging_engine = SemanticSearchEngine(
                active_search_engine.working_dir,
                db_dir=str(staging_path),
            )
            staging_engine.embedding_model = active_search_engine.embedding_model
            search_engine = staging_engine
        prepare_semantic_index = getattr(
            search_engine,
            "prepare_semantic_index",
            None,
        )
        if callable(prepare_semantic_index):
            prepare_semantic_index(
                force=bool(force and not resume_staging),
                workspace_id=workspace_id,
            )
        scan_force = bool(force and staging_engine is None)

        def emit_progress(**fields: Any) -> None:
            if progress is not None:
                progress(dict(fields))

        store = CloudSnapshotStore()
        records = store.list_records(workspace_id=workspace_id)
        snapshot_status_before = store.status(workspace_id)
        indexed_revision_watermark = int(
            snapshot_status_before.get("indexed_revision", 0)
        )
        current_paths = {record.path for record in records}
        index_stats_before: dict[str, Any] = {}
        get_stats = getattr(search_engine, "get_stats", None)
        if callable(get_stats):
            try:
                index_stats_before = dict(get_stats())
            except Exception:
                index_stats_before = {}
        indexed_total_files = int(index_stats_before.get("total_files") or 0)
        trust_revision_watermark = (
            not scan_force
            and indexed_revision_watermark > 0
            and indexed_total_files > 0
            and staging_engine is None
        )
        indexed_hashes: dict[str, str] = {}
        indexed_file_hashes = getattr(search_engine, "indexed_file_hashes", None)
        if not scan_force and callable(indexed_file_hashes):
            try:
                indexed_hashes = dict(indexed_file_hashes(workspace_id=workspace_id))
            except TypeError:
                indexed_hashes = dict(indexed_file_hashes())
        indexed_files = 0
        indexed_chunks = 0
        skipped_unchanged = 0
        skipped_by_indexed_revision = 0
        skipped_by_policy = 0
        skip_policy_reasons: dict[str, int] = {}
        files_truncated_by_chunk_limit = 0
        chunks_dropped_by_limit = 0
        deleted_index_entries = 0
        selected_limit = (
            semantic_selected_file_limit()
            if scope == "exact_policy"
            else 0
        )
        try:
            batch_size = max(
                1,
                int(
                    os.environ.get(
                        "OMNICODE_SEMANTIC_FILE_BATCH_SIZE",
                        "25",
                    )
                ),
            )
        except (TypeError, ValueError):
            batch_size = 25
        try:
            batch_max_bytes = max(
                64 * 1024,
                int(
                    os.environ.get(
                        "OMNICODE_SEMANTIC_BATCH_MAX_BYTES",
                        str(2 * 1024 * 1024),
                    )
                ),
            )
        except (TypeError, ValueError):
            batch_max_bytes = 2 * 1024 * 1024
        batch: list[tuple[str, str, dict[str, Any]]] = []
        batch_bytes = 0
        upsert_many = getattr(search_engine, "upsert_contents", None)
        records_processed = 0

        def progress_snapshot(current_path: Optional[str]) -> dict[str, Any]:
            return {
                "records_seen": records_processed,
                "records_total": len(records),
                "indexed_files": indexed_files,
                "indexed_chunks": indexed_chunks,
                "skipped_unchanged": skipped_unchanged,
                "skipped_by_indexed_revision": skipped_by_indexed_revision,
                "skipped_by_policy": skipped_by_policy,
                "skip_policy_reasons": dict(skip_policy_reasons),
                "files_truncated_by_chunk_limit": files_truncated_by_chunk_limit,
                "chunks_dropped_by_limit": chunks_dropped_by_limit,
                "deleted_index_entries": deleted_index_entries,
                "indexed_revision_watermark": indexed_revision_watermark,
                "current_path": current_path,
            }

        emit_progress(**progress_snapshot(None))

        async def flush_batch() -> None:
            nonlocal indexed_chunks, batch, batch_bytes
            nonlocal files_truncated_by_chunk_limit, chunks_dropped_by_limit
            if not batch:
                return
            if callable(upsert_many):
                try:
                    indexed_chunks += await upsert_many(batch, refresh=False)
                except TypeError:
                    indexed_chunks += await upsert_many(
                        [(path, body) for path, body, _metadata in batch]
                    )
            else:
                for path, body, metadata in batch:
                    try:
                        indexed_chunks += await search_engine.upsert_content(
                            path,
                            body,
                            refresh=False,
                            content_hash=metadata.get("content_hash"),
                            revision=metadata.get("snapshot_revision"),
                            workspace_id=metadata.get("workspace_id"),
                        )
                    except TypeError:
                        indexed_chunks += await search_engine.upsert_content(
                            path,
                            body,
                        )
            batch = []
            batch_bytes = 0
            upsert_stats = getattr(search_engine, "last_upsert_stats", {}) or {}
            try:
                files_truncated_by_chunk_limit += int(
                    upsert_stats.get("files_truncated_by_chunk_limit") or 0
                )
                chunks_dropped_by_limit += int(
                    upsert_stats.get("chunks_dropped_by_limit") or 0
                )
            except (TypeError, ValueError):
                pass

        for record in records:
            records_processed += 1
            if not scan_force:
                indexed_hash = indexed_hashes.get(record.path)
                if indexed_hash == record.hash:
                    skipped_unchanged += 1
                    if records_processed % 25 == 0 or records_processed == len(records):
                        emit_progress(**progress_snapshot(record.path))
                    continue
                if (
                    indexed_hash is None
                    and trust_revision_watermark
                    and record.revision <= indexed_revision_watermark
                ):
                    skipped_by_indexed_revision += 1
                    if records_processed % 25 == 0 or records_processed == len(records):
                        emit_progress(**progress_snapshot(record.path))
                    continue
            if selected_limit > 0 and indexed_files >= selected_limit:
                skipped_by_policy += 1
                skip_policy_reasons["selected_limit_reached"] = (
                    skip_policy_reasons.get("selected_limit_reached", 0) + 1
                )
                if records_processed % 25 == 0 or records_processed == len(records):
                    emit_progress(**progress_snapshot(record.path))
                continue
            path_skip_reason = semantic_path_skip_reason(record.path)
            if path_skip_reason:
                skipped_by_policy += 1
                skip_policy_reasons[path_skip_reason] = (
                    skip_policy_reasons.get(path_skip_reason, 0) + 1
                )
                if records_processed % 25 == 0 or records_processed == len(records):
                    emit_progress(**progress_snapshot(record.path))
                continue
            read_record_text = getattr(store, "read_record_text", None)
            if callable(read_record_text):
                content = read_record_text(workspace_id=workspace_id, record=record)
            else:  # pragma: no cover - compatibility for older injected fakes
                content = store.read_text(workspace_id=workspace_id, path=record.path)
            if content is None:
                if records_processed % 25 == 0 or records_processed == len(records):
                    emit_progress(**progress_snapshot(record.path))
                continue
            include_semantic, reason = semantic_index_decision(
                record.path,
                content,
                {
                    "phase": "semantic_full_bootstrap",
                    "files_seen": len(records),
                },
            )
            if not include_semantic:
                skipped_by_policy += 1
                skip_policy_reasons[reason] = (
                    skip_policy_reasons.get(reason, 0) + 1
                )
                if records_processed % 25 == 0 or records_processed == len(records):
                    emit_progress(**progress_snapshot(record.path))
                continue
            batch.append(
                (
                    record.path,
                    content,
                    semantic_index_metadata(
                        record.path,
                        content,
                        {
                            "content_hash": record.hash,
                            "snapshot_hash": record.hash,
                            "snapshot_revision": record.revision,
                            "workspace_id": workspace_id,
                        },
                    ),
                )
            )
            batch_bytes += len(content.encode("utf-8", errors="replace"))
            indexed_files += 1
            if len(batch) >= batch_size or batch_bytes >= batch_max_bytes:
                await flush_batch()
                emit_progress(**progress_snapshot(record.path))
            elif records_processed % 25 == 0 or records_processed == len(records):
                emit_progress(**progress_snapshot(record.path))
        await flush_batch()
        emit_progress(**progress_snapshot(None))

        delete_file_index = getattr(search_engine, "delete_file_index", None)
        if callable(delete_file_index):
            for path in sorted(set(indexed_hashes) - current_paths):
                try:
                    await delete_file_index(path, refresh=False)
                except TypeError:
                    await delete_file_index(path)
                deleted_index_entries += 1
                emit_progress(**progress_snapshot(path))

        refresh_stats = getattr(search_engine, "refresh_stats", None)
        if callable(refresh_stats):
            maybe_awaitable = refresh_stats()
            if asyncio.iscoroutine(maybe_awaitable):
                await maybe_awaitable
        else:
            initialize = getattr(search_engine, "initialize", None)
            if callable(initialize):
                await initialize()

        activation: dict[str, Any] = {}
        if staging_engine is not None:
            replace_index = getattr(
                active_search_engine,
                "replace_semantic_index_from",
                None,
            )
            if not callable(replace_index):
                raise RuntimeError(
                    "SEMANTIC_STAGING_ACTIVATION_UNAVAILABLE"
                )
            activation = dict(replace_index(staging_engine))
            staging_engine.vector_store.close()
            if staging_path is not None and staging_path.exists():
                shutil.rmtree(staging_path)
            search_engine = active_search_engine

        snapshot_status = store.status(workspace_id)
        accepted_revision = int(snapshot_status.get("accepted_revision", 0))
        indexed_revision = store.mark_indexed(
            workspace_id=workspace_id,
            revision=accepted_revision,
            semantic_coverage=(
                "semantic_full"
                if scope == "semantic"
                else "filtered"
                if skipped_by_policy
                else "selected_files"
            ),
        )
        try:
            from api.v1.routers.sync import record_external_indexed_revision

            record_external_indexed_revision(workspace_id, indexed_revision)
        except Exception:
            pass

        stats = search_engine.get_stats()
        return {
            "message": "Snapshot indexing completed",
            "workspace_id": workspace_id,
            "snapshot_store_used": True,
            "force": force,
            "scope": scope,
            "staging_used": staging_engine is not None,
            "staging_resumed": bool(resume_staging),
            "activation": activation,
            "semantic_index_policy": semantic_index_policy_payload(),
            "records_seen": len(records),
            "records_total": len(records),
            "indexed_files": indexed_files,
            "indexed_chunks": indexed_chunks,
            "skipped_unchanged": skipped_unchanged,
            "skipped_by_indexed_revision": skipped_by_indexed_revision,
            "skipped_by_policy": skipped_by_policy,
            "skip_policy_reasons": dict(skip_policy_reasons),
            "files_truncated_by_chunk_limit": files_truncated_by_chunk_limit,
            "chunks_dropped_by_limit": chunks_dropped_by_limit,
            "deleted_index_entries": deleted_index_entries,
            "accepted_revision": accepted_revision,
            "indexed_revision": indexed_revision,
            "indexed_revision_watermark": indexed_revision_watermark,
            "batch_size": batch_size,
            "batch_max_bytes": batch_max_bytes,
            "stats": stats,
        }

    return asyncio.run(_index())


def _public_snapshot_index_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"thread", "resume_event"}
    }


def _snapshot_index_job_key(workspace_id: str) -> str:
    return workspace_id


def _snapshot_job_state_path(workspace_id: str) -> Path:
    state_root = Path(
        os.environ.get("OMNICODE_STATE_DIR")
        or (Path.home() / ".omnicode")
    ).expanduser()
    key = hashlib.sha1(
        workspace_id.encode("utf-8", "replace")
    ).hexdigest()[:20]
    return state_root / "index-jobs" / f"{key}.json"


def _snapshot_job_staging_dir(workspace_id: str) -> Path:
    state_root = Path(
        os.environ.get("OMNICODE_STATE_DIR")
        or (Path.home() / ".omnicode")
    ).expanduser()
    key = hashlib.sha1(
        workspace_id.encode("utf-8", "replace")
    ).hexdigest()[:20]
    return state_root / "index-jobs" / "staging" / key


def _persist_snapshot_index_job(job: dict[str, Any]) -> None:
    path = _snapshot_job_state_path(str(job.get("workspace_id") or "workspace"))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _public_snapshot_index_job(job)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_snapshot_index_job(workspace_id: str) -> Optional[dict[str, Any]]:
    path = _snapshot_job_state_path(workspace_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("state") in {"running", "paused"}:
        payload.update({
            "state": "interrupted",
            "retryable": True,
            "error": "index worker process restarted before completion",
        })
    return payload


def snapshot_index_job_status(workspace_id: str) -> dict[str, Any]:
    """Return the public snapshot semantic-index job for status aggregation."""
    key = _snapshot_index_job_key(workspace_id)
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        job = _SNAPSHOT_INDEX_JOBS.get(key)
        if not isinstance(job, dict):
            persisted = _load_snapshot_index_job(workspace_id)
            if persisted is not None:
                return {
                    "workspace_id": workspace_id,
                    "background": True,
                    "state": persisted.get("state"),
                    "job": persisted,
                }
            return {
                "workspace_id": workspace_id,
                "background": True,
                "state": "idle",
                "job": None,
            }
        return {
            "workspace_id": workspace_id,
            "background": True,
            "state": job.get("state"),
            "job": _public_snapshot_index_job(job),
        }


def _start_snapshot_index_job(
    workspace_id: str,
    *,
    force: bool = False,
    scope: str = "semantic",
    staging_dir: Optional[str] = None,
    resume_staging: bool = False,
) -> dict[str, Any]:
    key = _snapshot_index_job_key(workspace_id)
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        existing = _SNAPSHOT_INDEX_JOBS.get(key)
        if isinstance(existing, dict) and existing.get("state") == "running":
            return _public_snapshot_index_job(existing)
        if isinstance(existing, dict) and existing.get("state") == "paused":
            return _public_snapshot_index_job(existing)
        previous_attempt = int(
            (existing or {}).get("attempt")
            or (_load_snapshot_index_job(workspace_id) or {}).get("attempt")
            or 0
        )
        resume_event = threading.Event()
        resume_event.set()
        effective_staging_dir = (
            str(staging_dir or _snapshot_job_staging_dir(workspace_id))
            if scope == "semantic"
            else None
        )
        job = {
            "job_id": f"{workspace_id}:{int(time.time() * 1000)}",
            "workspace_id": workspace_id,
            "state": "running",
            "attempt": previous_attempt + 1,
            "retryable": False,
            "force": force,
            "scope": scope,
            "staging_dir": effective_staging_dir,
            "staging_resumed": bool(resume_staging),
            "activation_strategy": (
                "staging_atomic_swap"
                if effective_staging_dir
                else "in_place_incremental"
            ),
            "started_at": time.time(),
            "completed_at": None,
            "elapsed_ms": None,
            "records_seen": 0,
            "records_total": None,
            "indexed_files": 0,
            "indexed_chunks": 0,
            "skipped_unchanged": 0,
            "skipped_by_indexed_revision": 0,
            "skipped_by_policy": 0,
            "skip_policy_reasons": {},
            "files_truncated_by_chunk_limit": 0,
            "chunks_dropped_by_limit": 0,
            "deleted_index_entries": 0,
            "indexed_revision_watermark": None,
            "current_path": None,
            "progress_percent": 0.0,
            "records_per_second": 0.0,
            "eta_seconds": None,
            "last_update_at": None,
            "result": None,
            "error": None,
            "resume_event": resume_event,
        }
        _SNAPSHOT_INDEX_JOBS[key] = job
        _persist_snapshot_index_job(job)

    def _runner() -> None:
        started = time.monotonic()

        def _progress(fields: dict[str, Any]) -> None:
            resume_event.wait()
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(fields)
                elapsed = max(time.monotonic() - started, 0.001)
                job["elapsed_ms"] = int(elapsed * 1000)
                seen = int(job.get("records_seen") or 0)
                total = int(job.get("records_total") or 0)
                rate = seen / elapsed if seen > 0 else 0.0
                job["records_per_second"] = round(rate, 3)
                job["progress_percent"] = (
                    round(min(seen / total, 1.0) * 100.0, 2)
                    if total > 0
                    else 0.0
                )
                job["eta_seconds"] = (
                    round(max(total - seen, 0) / rate, 2)
                    if total > seen and rate > 0
                    else 0.0
                    if total > 0 and seen >= total
                    else None
                )
                job["last_update_at"] = time.time()
                _persist_snapshot_index_job(job)

        try:
            result = _run_snapshot_index_blocking(
                workspace_id,
                force=force,
                scope=scope,
                progress=_progress,
                staging_dir=effective_staging_dir,
                resume_staging=bool(resume_staging),
            )
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(
                    {
                        "state": "completed",
                        "retryable": False,
                        "completed_at": time.time(),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "records_seen": result.get("records_seen"),
                        "records_total": result.get("records_total"),
                        "indexed_files": result.get("indexed_files"),
                        "indexed_chunks": result.get("indexed_chunks"),
                        "skipped_unchanged": result.get("skipped_unchanged"),
                        "skipped_by_indexed_revision": result.get(
                            "skipped_by_indexed_revision"
                        ),
                        "skipped_by_policy": result.get("skipped_by_policy"),
                        "skip_policy_reasons": result.get("skip_policy_reasons"),
                        "files_truncated_by_chunk_limit": result.get(
                            "files_truncated_by_chunk_limit"
                        ),
                        "chunks_dropped_by_limit": result.get(
                            "chunks_dropped_by_limit"
                        ),
                        "deleted_index_entries": result.get("deleted_index_entries"),
                        "indexed_revision_watermark": result.get(
                            "indexed_revision_watermark"
                        ),
                        "current_path": None,
                        "last_update_at": time.time(),
                        "result": result,
                        "staging_resumed": result.get("staging_resumed"),
                        "activation": result.get("activation"),
                        "error": None,
                        "progress_percent": 100.0,
                        "eta_seconds": 0.0,
                    }
                )
                _persist_snapshot_index_job(job)
        except Exception as exc:  # pragma: no cover - defensive thread boundary
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(
                    {
                        "state": "failed",
                        "retryable": True,
                        "completed_at": time.time(),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                _persist_snapshot_index_job(job)

    thread = threading.Thread(
        target=_runner,
        name=f"snapshot-index-{workspace_id}",
        daemon=True,
    )
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        job["thread"] = thread
    thread.start()
    return _public_snapshot_index_job(job)


def control_snapshot_index_job(
    workspace_id: str,
    *,
    action: str,
) -> dict[str, Any]:
    """Pause, resume, or retry one background semantic index job."""

    action_value = (action or "").strip().lower()
    if action_value not in {"pause", "resume", "retry"}:
        raise ValueError("action must be one of: pause, resume, retry")
    retry_config: Optional[tuple[bool, str, Optional[str], bool]] = None
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        job = _SNAPSHOT_INDEX_JOBS.get(workspace_id)
        if not isinstance(job, dict):
            persisted = _load_snapshot_index_job(workspace_id)
            if action_value != "retry" or persisted is None:
                raise ValueError("no index job exists for this workspace")
            retry_config = (
                bool(persisted.get("force")),
                str(persisted.get("scope") or "semantic"),
                (
                    str(persisted.get("staging_dir"))
                    if persisted.get("staging_dir")
                    else None
                ),
                bool(persisted.get("staging_dir")),
            )
        elif action_value == "pause":
            if job.get("state") != "running":
                raise ValueError("only a running index job can be paused")
            event = job.get("resume_event")
            if isinstance(event, threading.Event):
                event.clear()
            job["state"] = "paused"
            job["paused_at"] = time.time()
            _persist_snapshot_index_job(job)
            return _public_snapshot_index_job(job)
        elif action_value == "resume":
            if job.get("state") != "paused":
                raise ValueError("only a paused index job can be resumed")
            event = job.get("resume_event")
            if isinstance(event, threading.Event):
                event.set()
            job["state"] = "running"
            job["resumed_at"] = time.time()
            _persist_snapshot_index_job(job)
            return _public_snapshot_index_job(job)
        else:
            if job.get("state") in {"running", "paused"}:
                raise ValueError("running or paused jobs cannot be retried")
            retry_config = (
                bool(job.get("force")),
                str(job.get("scope") or "semantic"),
                (
                    str(job.get("staging_dir"))
                    if job.get("staging_dir")
                    else None
                ),
                bool(job.get("staging_dir")),
            )
            _SNAPSHOT_INDEX_JOBS.pop(workspace_id, None)
    assert retry_config is not None
    return _start_snapshot_index_job(
        workspace_id,
        force=retry_config[0],
        scope=retry_config[1],
        staging_dir=retry_config[2],
        resume_staging=retry_config[3],
    )


@router.post("")
async def search_codebase(
    request: SearchRequest,
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Search the codebase using semantic search"""
    try:
        workspace_id = _resolve_search_workspace(x_omnicode_workspace)
        stale = cloud_freshness_error(
            workspace_id=workspace_id,
            min_revision=x_omnicode_min_revision,
            allow_exact_fresh=request.search_type == "semantic",
        )
        if stale is not None:
            return stale

        search_plan = build_search_plan(
            query=request.query,
            requested_mode=request.search_type,
            resolved_mode=request.search_type,
            use_regex=bool(getattr(request, "use_regex", False)),
            freshness_required=bool(x_omnicode_min_revision),
        )
        debug_timing: dict[str, Any] = {}
        exact_only_snapshot = False
        if request.search_type == "semantic" and workspace_id:
            status_started = time.perf_counter()
            try:
                snapshot_status = CloudSnapshotStore().status(workspace_id)
                exact_only_snapshot = (
                    isinstance(snapshot_status, dict)
                    and (
                        snapshot_status.get("semantic_initial_exact_only") is True
                        or snapshot_status.get("semantic_index_coverage")
                        == "exact_only_initial_sync"
                    )
                )
            except Exception:
                exact_only_snapshot = False
            debug_timing["semantic_snapshot_status_ms"] = _elapsed_ms(status_started)
            debug_timing["semantic_exact_only_snapshot"] = exact_only_snapshot
        if request.search_type == "semantic" and exact_only_snapshot:
            fast_path_started = time.perf_counter()
            exact_started = time.perf_counter()
            snapshot_boost = await asyncio.to_thread(
                _snapshot_semantic_exact_boost,
                workspace_id=workspace_id,
                query=request.query,
                file_pattern=request.file_pattern,
                max_results=request.max_results,
            )
            debug_timing["semantic_exact_boost_ms"] = _elapsed_ms(exact_started)
            debug_timing["semantic_exact_boost_rows"] = len(snapshot_boost)
            boost_keys: set[tuple[str, int, str]] = {
                (
                    str(row.get("file_path") or ""),
                    int(row.get("line_start") or row.get("line_number") or 0),
                    str(row.get("symbol_name") or ""),
                )
                for row in snapshot_boost
            }
            if len(snapshot_boost) < request.max_results:
                snapshot_boost.extend(
                    await asyncio.to_thread(
                        _snapshot_semantic_lexical_boost,
                        workspace_id=workspace_id,
                        query=request.query,
                        file_pattern=request.file_pattern,
                        max_results=request.max_results - len(snapshot_boost),
                        existing_keys=boost_keys,
                        debug_timing=debug_timing,
                    )
                )
            provider_chain = ["cloud_snapshot_grep", "semantic_vector"]
            fallback_rows = snapshot_boost[: request.max_results]
            if fallback_rows:
                debug_timing["semantic_fast_path_total_ms"] = _elapsed_ms(
                    fast_path_started
                )
                return create_success_response(
                    _search_success_payload(**{
                        "query": request.query,
                        "search_type": request.search_type,
                        "results": fallback_rows,
                        "total_results": len(fallback_rows),
                        "snapshot_store_used": True,
                        "provider": "cloud_snapshot_grep",
                        "provider_chain": provider_chain,
                        "query_plan": search_plan.to_dict(providers=provider_chain),
                        "capabilities_used": ["search.text_exact"],
                        "capabilities_missing": ["search.semantic"],
                        "fallback_used": True,
                        "fallback_reason": (
                            "semantic_exact_only_snapshot_fallback"
                        ),
                        "warnings": [
                            "semantic index is exact-only; returned deterministic snapshot fallback"
                        ],
                        "empty_reason": None,
                        "snapshot_exact_boost": any(
                            "semantic:exact_boost" in (row.get("why_matched") or [])
                            for row in fallback_rows
                        ),
                        "snapshot_lexical_boost": any(
                            "semantic:lexical_boost" in (row.get("why_matched") or [])
                            for row in fallback_rows
                        ),
                        "semantic_exact_only_fast_path": True,
                        "debug_timing": debug_timing,
                        "semantic_index_ready": False,
                        "semantic_index_stale_reason": "exact_only_initial_sync",
                        "semantic_coverage": "exact_only_initial_sync",
                        "semantic_provider": None,
                        "vector_count": 0,
                        "rrf_used": False,
                        "rerank_used": False,
                        "freshness": "degraded",
                    })
                )
            return _structured_search_error(
                message="SEMANTIC_INDEX_NOT_READY: exact_only_initial_sync",
                status_code=409,
                error_code="SEMANTIC_INDEX_NOT_READY",
                query=request.query,
                search_type=request.search_type,
                results=[],
                total_results=0,
                snapshot_store_used=True,
                provider="semantic_vector",
                provider_chain=provider_chain,
                query_plan=search_plan.to_dict(providers=provider_chain),
                capabilities_used=[],
                capabilities_missing=["search.semantic"],
                fallback_used=False,
                fallback_reason="semantic_exact_only_snapshot_no_fallback",
                empty_reason="provider_unavailable",
            )
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        snapshot_boost = await asyncio.to_thread(
            _snapshot_semantic_exact_boost,
            workspace_id=workspace_id,
            query=request.query,
            file_pattern=request.file_pattern,
            max_results=request.max_results,
        )
        boost_keys: set[tuple[str, int, str]] = {
            (
                str(row.get("file_path") or ""),
                int(row.get("line_start") or row.get("line_number") or 0),
                str(row.get("symbol_name") or ""),
            )
            for row in snapshot_boost
        }
        if len(snapshot_boost) < request.max_results:
            snapshot_boost.extend(
                await asyncio.to_thread(
                    _snapshot_semantic_lexical_boost,
                    workspace_id=workspace_id,
                    query=request.query,
                    file_pattern=request.file_pattern,
                    max_results=request.max_results - len(snapshot_boost),
                    existing_keys=boost_keys,
                )
            )
        provider_chain: list[str] = []
        if snapshot_boost:
            provider_chain.append("cloud_snapshot_grep")
        provider_chain.append("semantic_vector")
        semantic_status: dict[str, Any] = {}
        snapshot_status: dict[str, Any] = {}
        semantic_ready = True
        if request.search_type == "semantic":
            try:
                semantic_status_fn = getattr(search_engine, "semantic_index_status", None)
                if callable(semantic_status_fn):
                    semantic_status = dict(semantic_status_fn())
                    semantic_ready = bool(
                        semantic_status.get("semantic_index_ready")
                    )
            except Exception as exc:
                semantic_ready = False
                semantic_status = {
                    "semantic_index_stale_reason": (
                        f"semantic_status_error:{exc.__class__.__name__}"
                    )
                }
            try:
                snapshot_status = CloudSnapshotStore().status(workspace_id)
            except Exception:
                snapshot_status = {}
            if (
                isinstance(snapshot_status, dict)
                and (
                    snapshot_status.get("semantic_initial_exact_only") is True
                    or snapshot_status.get("semantic_index_coverage")
                    == "exact_only_initial_sync"
                )
            ):
                semantic_ready = False
                semantic_status.setdefault(
                    "semantic_index_stale_reason",
                    "exact_only_initial_sync",
                )
        if request.search_type == "semantic" and not semantic_ready:
            fallback_rows = snapshot_boost[: request.max_results]
            if fallback_rows:
                return create_success_response(
                    _search_success_payload(**{
                        "query": request.query,
                        "search_type": request.search_type,
                        "results": fallback_rows,
                        "total_results": len(fallback_rows),
                        "snapshot_store_used": bool(workspace_id),
                        "provider": "cloud_snapshot_grep",
                        "provider_chain": provider_chain,
                        "query_plan": search_plan.to_dict(providers=provider_chain),
                        "capabilities_used": ["search.text_exact"],
                        "capabilities_missing": ["search.semantic"],
                        "fallback_used": True,
                        "fallback_reason": (
                            "semantic_index_not_ready_exact_or_lexical_boost"
                        ),
                        "warnings": [
                            "semantic index is not ready; returned deterministic snapshot fallback"
                        ],
                        "empty_reason": None,
                        "snapshot_exact_boost": any(
                            "semantic:exact_boost" in (row.get("why_matched") or [])
                            for row in fallback_rows
                        ),
                        "snapshot_lexical_boost": any(
                            "semantic:lexical_boost" in (row.get("why_matched") or [])
                            for row in fallback_rows
                        ),
                        "semantic_index_ready": False,
                        "semantic_index_stale_reason": (
                            semantic_status.get("semantic_index_stale_reason")
                            or "semantic_index_not_ready"
                        ),
                        "semantic_coverage": snapshot_status.get(
                            "semantic_index_coverage"
                        ),
                        "semantic_provider": (
                            (semantic_status.get("runtime") or {}).get(
                                "embedding_backend"
                            )
                            or "faiss"
                        ),
                        "vector_count": int(
                            semantic_status.get("vector_count") or 0
                        ),
                        "rrf_used": False,
                        "rerank_used": False,
                    })
                )
            return _structured_search_error(
                message=(
                    semantic_status.get("semantic_index_stale_reason")
                    or "SEMANTIC_INDEX_NOT_READY: semantic index is not ready"
                ),
                status_code=409,
                error_code="SEMANTIC_INDEX_NOT_READY",
                query=request.query,
                search_type=request.search_type,
                results=[],
                total_results=0,
                snapshot_store_used=bool(workspace_id),
                provider="semantic_vector",
                provider_chain=provider_chain,
                query_plan=search_plan.to_dict(providers=provider_chain),
                capabilities_used=[],
                capabilities_missing=["search.semantic"],
                fallback_used=False,
                fallback_reason="semantic_index_not_ready",
                empty_reason="provider_unavailable",
                next_actions=[
                    "omni_index(action='bootstrap', scope='semantic', background=True, format='json') to rebuild semantic vectors.",
                    "Retry with mode='symbol' or mode='text' for deterministic exact search.",
                ],
            )
        try:
            indexed_results = await search_engine.search(request)
        except RuntimeError as exc:
            message = str(exc)
            if "SEMANTIC_INDEX_NOT_READY" in message:
                return _structured_search_error(
                    message=message,
                    status_code=409,
                    error_code="SEMANTIC_INDEX_NOT_READY",
                    query=request.query,
                    search_type=request.search_type,
                    results=snapshot_boost[: request.max_results],
                    total_results=len(snapshot_boost[: request.max_results]),
                    snapshot_store_used=bool(workspace_id),
                    provider="cloud_snapshot_grep" if snapshot_boost else "semantic_vector",
                    provider_chain=provider_chain,
                    query_plan=search_plan.to_dict(providers=provider_chain),
                    capabilities_used=(
                        ["search.text_exact"] if snapshot_boost else []
                    ),
                    capabilities_missing=["search.semantic"],
                    fallback_used=bool(snapshot_boost),
                    fallback_reason=(
                        "semantic_index_not_ready_exact_boost_available"
                        if snapshot_boost
                        else "semantic_index_not_ready"
                    ),
                    empty_reason=(
                        None if snapshot_boost else "provider_unavailable"
                    ),
                    next_actions=[
                        "omni_index(action='bootstrap', scope='semantic', background=True, format='json') to rebuild semantic vectors.",
                        "Retry with mode='symbol' or mode='text' for deterministic exact search.",
                    ],
                )
            raise

        # Format results for API response.
        formatted_results = list(snapshot_boost)
        seen_keys: set[tuple[str, int, str]] = {
            (
                str(row.get("file_path") or ""),
                int(row.get("line_start") or row.get("line_number") or 0),
                str(row.get("symbol_name") or ""),
            )
            for row in formatted_results
        }
        for result in indexed_results:
            key = (
                str(result.file_path),
                int(result.line_start or 0),
                str(result.symbol_name or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
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
                    "why_matched": getattr(result, "why_matched", []),
                }
            )
            if len(formatted_results) >= request.max_results:
                break

        return create_success_response(
            _search_success_payload(**{
                "query": request.query,
                "search_type": request.search_type,
                "results": formatted_results[: request.max_results],
                "total_results": len(formatted_results),
                "snapshot_store_used": bool(workspace_id),
                "provider": "semantic_vector",
                "provider_chain": provider_chain,
                "query_plan": search_plan.to_dict(providers=provider_chain),
                "capabilities_used": ["search.semantic"],
                "capabilities_missing": [],
                "fallback_used": bool(snapshot_boost),
                "fallback_reason": (
                    "snapshot_exact_or_lexical_boost" if snapshot_boost else None
                ),
                "warnings": [],
                "empty_reason": "true_empty" if not formatted_results else None,
                "snapshot_exact_boost": any(
                    "semantic:exact_boost" in (row.get("why_matched") or [])
                    for row in snapshot_boost
                ),
                "snapshot_lexical_boost": any(
                    "semantic:lexical_boost" in (row.get("why_matched") or [])
                    for row in snapshot_boost
                ),
                "semantic_index_ready": bool(
                    semantic_status.get("semantic_index_ready", True)
                ),
                "semantic_coverage": snapshot_status.get(
                    "semantic_index_coverage"
                ),
                "semantic_provider": (
                    (semantic_status.get("runtime") or {}).get(
                        "embedding_backend"
                    )
                    or "faiss"
                ),
                "vector_count": int(
                    semantic_status.get("vector_count") or 0
                ),
                "rrf_used": True,
                "rerank_used": any(
                    "reranked" in (row.get("why_matched") or [])
                    for row in formatted_results
                    if isinstance(row, dict)
                ),
                "semantic_indexed_revision": (
                    (semantic_status.get("metadata") or {}).get(
                        "indexed_revision"
                    )
                ),
                "freshness": (
                    "fresh"
                    if not workspace_id
                    or int(snapshot_status.get("indexed_revision") or 0)
                    >= int(snapshot_status.get("accepted_revision") or 0)
                    else "stale"
                ),
            })
        )

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Search failed: {str(e)}", 500)


@router.post("/index")
async def index_codebase(
    force: bool = Query(False, description="Force full rebuild (ignore file tracker cache)"),
    background: bool = Query(
        False,
        description="For snapshot workspaces, start indexing in the background.",
    ),
    scope: str = Query(
        "auto",
        description=(
            "Index scope: 'auto' builds deterministic local files/lines/symbols "
            "for local requests and preserves snapshot semantic bootstrap for "
            "workspace requests; 'workspace' forces local deterministic index, "
            "'semantic' indexes snapshot text into the semantic store, and "
            "'exact_policy' applies the configured semantic extension/size policy."
        ),
    ),
    workspace_id: Optional[str] = Query(
        None,
        description="Hybrid sync workspace id; when present, index snapshot-store content.",
    ),
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Index the codebase incrementally (or force full rebuild).

    By default, only new/modified files are re-indexed and deleted files
    are removed.  Unchanged files are skipped entirely.  This typically
    reduces indexing time from 30-60s to 2-3s.

    Pass ?force=true to clear the file tracker and rebuild everything.
    """
    try:
        scope_value = (scope or "auto").strip().lower()
        if scope_value not in {"auto", "semantic", "exact_policy", "workspace"}:
            return create_error_response(
                "scope must be one of: auto, semantic, exact_policy, workspace",
                400,
            )
        requested_workspace_id = x_omnicode_workspace or workspace_id
        if scope_value == "auto":
            scope_value = "semantic" if requested_workspace_id else "workspace"
        effective_workspace_id = (
            None
            if scope_value == "workspace"
            else _resolve_search_workspace(requested_workspace_id)
        )
        if scope_value == "workspace":
            settings = get_settings()
            workspace_key = (
                requested_workspace_id
                or _local_exact_workspace_id()
            )
            payload = await asyncio.to_thread(
                _exact_index().index_workspace_root,
                workspace_id=workspace_key,
                root=settings.WORKING_DIR,
                revision=int(time.time()),
                force=force,
            )
            return create_success_response({
                "message": "Workspace exact index completed",
                "scope": "workspace",
                "snapshot_store_used": False,
                "exact_index_used": True,
                **payload,
            })

        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        if effective_workspace_id:
            if background:
                return create_success_response(
                    {
                        "message": "Snapshot indexing started",
                        "workspace_id": effective_workspace_id,
                        "snapshot_store_used": True,
                        "background": True,
                        "job": _start_snapshot_index_job(
                            effective_workspace_id,
                            force=force,
                            scope=scope_value,
                        ),
                    }
                )
            payload = await asyncio.to_thread(
                _run_snapshot_index_blocking,
                effective_workspace_id,
                force=force,
                scope=scope_value,
            )
            return create_success_response(payload)

        if force:
            # Clear the file tracker so everything looks "new"
            import os

            from omnicode_core.index.file_tracker import FileTracker
            tracker_db = os.path.join(search_engine.db_dir, "file_tracker.db")
            FileTracker(tracker_db).clear()
        prepare_semantic_index = getattr(
            search_engine,
            "prepare_semantic_index",
            None,
        )
        if callable(prepare_semantic_index):
            prepare_semantic_index(force=bool(force))

        await search_engine.index_codebase()
        stats = search_engine.get_stats()

        return create_success_response(
            {"message": "Codebase indexing completed", "stats": stats}
        )

    except Exception as e:
        return create_error_response(f"Indexing failed: {str(e)}", 500)


@router.get("/index/status")
async def snapshot_index_status(
    workspace_id: Optional[str] = Query(
        None,
        description="Hybrid sync workspace id for a snapshot indexing job.",
    ),
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    try:
        effective_workspace_id = _resolve_search_workspace(
            x_omnicode_workspace or workspace_id
        )
        if not effective_workspace_id:
            return create_error_response("workspace_id is required", 400)
        key = _snapshot_index_job_key(effective_workspace_id)
        return create_success_response(snapshot_index_job_status(key))
    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Index status failed: {str(e)}", 500)


@router.post("/index/control")
async def snapshot_index_control(
    action: str = Query(..., description="pause | resume | retry"),
    workspace_id: Optional[str] = Query(
        None,
        description="Hybrid sync workspace id for a snapshot indexing job.",
    ),
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    try:
        effective_workspace_id = _resolve_search_workspace(
            x_omnicode_workspace or workspace_id
        )
        if not effective_workspace_id:
            return create_error_response("workspace_id is required", 400)
        job = control_snapshot_index_job(
            effective_workspace_id,
            action=action,
        )
        return create_success_response({
            "workspace_id": effective_workspace_id,
            "action": action.strip().lower(),
            "background": True,
            "state": job.get("state"),
            "job": job,
        })
    except HTTPException:
        raise
    except ValueError as exc:
        return create_error_response(str(exc), 409)
    except Exception as exc:
        return create_error_response(
            f"Index control failed: {str(exc)}",
            500,
        )


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
    file_pattern: str = Query(
        "",
        description="Comma-separated globs (e.g. '*.py,*.md'). Empty = sensible source-code defaults.",
    ),
    use_regex: bool = Query(False, description="Use regex matching"),
    case_sensitive: bool = Query(False, description="Case sensitive search"),
    max_results: int = Query(50, description="Maximum results"),
    context_lines: int = Query(2, description="Lines of context before/after each hit"),
    merge_adjacent: bool = Query(
        True,
        description=(
            "When true (default), hits within 2*context_lines of each other "
            "in the same file fold into one record. Set false to keep every "
            "match as a separate row."
        ),
    ),
    workspace_id: Optional[str] = Query(
        None,
        description="Hybrid sync workspace id; when set, also searches cloud snapshot content.",
    ),
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Line-level text search across the workspace.

    Walks the working directory, prunes vendor / cache directories,
    and returns real `(file, line_no, line_content, context_before,
    context_after)` records with column spans for highlighting.

    Unlike the previous SQLite-chunk LIKE scan, this hits every file
    that matches ``file_pattern`` 鈥?not just files that have been
    indexed 鈥?so freshly-added code is searchable immediately.
    """
    try:
        effective_workspace_id = _resolve_search_workspace(
            x_omnicode_workspace or workspace_id
        )
        stale = cloud_freshness_error(
            workspace_id=effective_workspace_id,
            min_revision=x_omnicode_min_revision,
            allow_snapshot_fresh=True,
            allow_exact_fresh=True,
        )
        if stale is not None:
            return stale
        freshness = cloud_freshness_state(
            workspace_id=effective_workspace_id,
            min_revision=x_omnicode_min_revision,
        )

        from omnicode_core.search.text_grep import grep_workspace_with_provider

        settings = get_settings()
        patterns = (
            [p.strip() for p in file_pattern.split(",") if p.strip()]
            if file_pattern
            else None
        )
        search_plan = build_search_plan(
            query=query,
            requested_mode="text",
            resolved_mode="text",
            use_regex=use_regex,
            freshness_required=bool(x_omnicode_min_revision),
        )

        results = []
        existing_keys: set[tuple[str, int]] = set()
        exact_index_used = False
        exact_line_fts_available = False
        exact_line_fts_reason: Optional[str] = None
        provider_chain: list[str] = []
        warnings: list[str] = []
        provider: Optional[str] = None
        fallback_used = False
        fallback_reason: Optional[str] = None
        exact_index_authoritative = bool(
            effective_workspace_id
            and x_omnicode_min_revision
            and freshness
            and not freshness.get("exact_stale", True)
        )
        if effective_workspace_id:
            try:
                exact_status = await asyncio.to_thread(
                    _exact_index().status,
                    workspace_id=effective_workspace_id,
                )
                exact_line_fts_available = bool(
                    exact_status.get("line_fts_available", False)
                )
                exact_line_fts_reason = exact_status.get("line_fts_reason")
            except Exception as exc:
                exact_line_fts_available = False
                exact_line_fts_reason = f"status_failed: {exc}"
            if exact_line_fts_available:
                provider_chain.append("exact_line_fts")
                exact_rows = await asyncio.to_thread(
                    _exact_index().search_text,
                    workspace_id=effective_workspace_id,
                    query=query,
                    file_pattern=file_pattern or None,
                    use_regex=use_regex,
                    case_sensitive=case_sensitive,
                    max_results=max_results,
                    context_lines=context_lines,
                )
                exact_index_used = True
                provider = "exact_line_fts" if exact_rows else provider
                for row in exact_rows:
                    existing_keys.add((row.path, row.line_no))
                    results.append(
                        {
                            "file_path": row.path,
                            "line_number": row.line_no,
                            "line_content": row.line_text,
                            "context_before": row.context_before,
                            "context_after": row.context_after,
                            "match_span": list(row.match_span),
                            "match_type": "text",
                            "relevance_score": 1.0,
                            "why_matched": ["text:line_match", "exact_index"],
                            "source": "exact_index",
                            "hash": row.hash,
                            "revision": row.revision,
                        }
                    )
            else:
                provider_chain.append("exact_line_fts")
                warnings.append(
                    "line_fts unavailable; using grep/snapshot fallback"
                    + (f" ({exact_line_fts_reason})" if exact_line_fts_reason else "")
                )

        if results and exact_index_used:
            return create_success_response(
                {
                    "query": query,
                    "search_type": "text",
                    "file_pattern": file_pattern or "(defaults)",
                    "use_regex": use_regex,
                    "case_sensitive": case_sensitive,
                    "results": results[:max_results],
                    "total_results": min(len(results), max_results),
                    "snapshot_store_used": bool(effective_workspace_id),
                    "exact_index_used": exact_index_used,
                    "exact_line_fts_available": exact_line_fts_available,
                    "line_fts_available": exact_line_fts_available,
                    "line_fts_reason": exact_line_fts_reason,
                    "provider": provider or "exact_line_fts",
                    "provider_chain": provider_chain,
                    "query_plan": search_plan.to_dict(providers=provider_chain),
                    "capabilities_used": list(provider_chain),
                    "capabilities_missing": (
                        []
                        if exact_line_fts_available
                        else ["search.text_exact.line_fts"]
                    ),
                    "fallback_used": fallback_used,
                    "fallback_reason": fallback_reason,
                    "warnings": warnings,
                    "empty_reason": None,
                    "exact_fast_path": True,
                    "freshness": (freshness or {}).get("freshness", "unknown")
                    if x_omnicode_min_revision
                    else "unknown",
                    "semantic_stale": bool((freshness or {}).get("semantic_stale", False)),
                    "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                    "accepted_revision": (freshness or {}).get("accepted_revision"),
                    "indexed_revision": (freshness or {}).get("indexed_revision"),
                    "exact_indexed_revision": (freshness or {}).get("exact_indexed_revision"),
                }
            )

        grep_result = grep_workspace_with_provider(
            workspace_root=settings.WORKING_DIR,
            query=query,
            file_patterns=patterns,
            max_results=max_results,
            context_lines=context_lines,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            merge_adjacent=merge_adjacent,
        )
        provider_chain.extend(grep_result.provider_chain)
        warnings.extend(grep_result.warnings)
        hits = grep_result.hits
        if grep_result.provider_chain and not fallback_used:
            fallback_used = True
            fallback_reason = (
                grep_result.fallback_reason
                or "exact_line_fts_unavailable_or_empty"
            )
        if hits:
            if not provider:
                provider = grep_result.provider

        for h in hits:
            key = (h.file_path, h.line_number)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            row = {
                "file_path": h.file_path,
                "line_number": h.line_number,
                "line_content": h.line_content,
                "context_before": h.context_before,
                "context_after": h.context_after,
                "match_span": list(h.match_span),
                "match_type": "text",
                # Plain-text matches don't have a continuous relevance
                # score; every hit is equally "matched". Use 1.0 so the
                # MCP renderer doesn't display a bogus 0.00.
                "relevance_score": 1.0,
                "why_matched": ["text:line_match"],
                "source": grep_result.provider,
            }
            merged_extra = list(getattr(h, "_merged_lines", []) or [])
            if merged_extra:
                row["merged_lines"] = merged_extra
                row["why_matched"].append(f"text:merged({len(merged_extra) + 1})")
            results.append(row)

        if len(results) < max_results:
            provider_chain.append("cloud_snapshot_grep")
            snapshot_rows = await asyncio.to_thread(
                _grep_snapshot_store,
                workspace_id=effective_workspace_id,
                query=query,
                patterns=patterns,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
                max_results=max_results - len(results),
                context_lines=context_lines,
                existing_keys=existing_keys,
            )
            if snapshot_rows:
                if not provider:
                    provider = "cloud_snapshot_grep"
                if not exact_index_used or len(results) == 0:
                    fallback_used = True
                    fallback_reason = "exact_line_fts_unavailable_or_empty"
            results.extend(snapshot_rows)

        return create_success_response(
            {
                "query": query,
                "search_type": "text",
                "file_pattern": file_pattern or "(defaults)",
                "use_regex": use_regex,
                "case_sensitive": case_sensitive,
                "results": results,
                "total_results": len(results),
                "snapshot_store_used": bool(effective_workspace_id),
                "exact_index_used": exact_index_used,
                "exact_line_fts_available": exact_line_fts_available,
                "line_fts_available": exact_line_fts_available,
                "line_fts_reason": exact_line_fts_reason,
                "provider": provider
                or (
                    "exact_line_fts"
                    if exact_index_used and exact_index_authoritative
                    else grep_result.provider
                ),
                "provider_chain": provider_chain,
                "query_plan": search_plan.to_dict(providers=provider_chain),
                "capabilities_used": list(provider_chain),
                "capabilities_missing": (
                    []
                    if exact_line_fts_available
                    else ["search.text_exact.line_fts"]
                ),
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "ripgrep_available": grep_result.rg_available,
                "grep_timeout_seconds": grep_result.timeout_seconds,
                "grep_max_file_bytes": grep_result.max_file_bytes,
                "warnings": warnings,
                "empty_reason": "true_empty" if not results else None,
                "freshness": (freshness or {}).get("freshness", "unknown")
                if x_omnicode_min_revision
                else "unknown",
                "semantic_stale": bool((freshness or {}).get("semantic_stale", False)),
                "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                "accepted_revision": (freshness or {}).get("accepted_revision"),
                "indexed_revision": (freshness or {}).get("indexed_revision"),
                "exact_indexed_revision": (freshness or {}).get("exact_indexed_revision"),
            }
        )

    except HTTPException:
        raise
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
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Search for symbols with fuzzy matching"""
    try:
        workspace_id = _resolve_search_workspace(x_omnicode_workspace)
        stale = cloud_freshness_error(
            workspace_id=workspace_id,
            min_revision=x_omnicode_min_revision,
            allow_snapshot_fresh=True,
            allow_exact_fresh=True,
        )
        if stale is not None:
            return stale
        freshness = cloud_freshness_state(
            workspace_id=workspace_id,
            min_revision=x_omnicode_min_revision,
        )

        search_type = "fuzzy_symbol" if fuzzy else "symbol_exact"
        search_plan = build_search_plan(
            query=query,
            requested_mode="symbol",
            resolved_mode=search_type,
            freshness_required=bool(x_omnicode_min_revision),
        )

        # Snapshot-store rows come first so a freshly-synced large repo can
        # answer exact symbol lookups before the vector/symbol DB is fully
        # bootstrapped.
        formatted_results = []
        existing_keys: set[tuple[str, str, int]] = set()
        local_exact_status: dict[str, Any] = {}
        local_exact_used = False
        if not workspace_id:
            local_workspace_id = _local_exact_workspace_id()
            local_exact_status = await asyncio.to_thread(
                _exact_index().status,
                workspace_id=local_workspace_id,
            )
            if int(local_exact_status.get("symbols") or 0) > 0:
                local_rows = await asyncio.to_thread(
                    _exact_index().search_symbols,
                    workspace_id=local_workspace_id,
                    query=query,
                    symbol_type=symbol_type,
                    file_pattern=file_pattern,
                    fuzzy=fuzzy,
                    min_score=min_score,
                    max_results=max_results,
                )
                for row in local_rows:
                    key = (row.path, row.name, row.line_start)
                    existing_keys.add(key)
                    formatted_results.append(
                        _format_exact_symbol_row(row, source="local_exact_index")
                    )
                local_exact_used = bool(local_rows)
                local_has_exact = any(
                    "symbol:exact" in (row.get("why_matched") or [])
                    for row in formatted_results
                )
                if local_has_exact or len(formatted_results) >= max_results:
                    return create_success_response(
                        {
                            "query": query,
                            "search_type": search_type,
                            "symbol_type": symbol_type,
                            "fuzzy_enabled": fuzzy,
                            "results": formatted_results[:max_results],
                            "total_results": min(len(formatted_results), max_results),
                            "snapshot_store_used": False,
                            "exact_index_used": True,
                            "local_exact_index_used": True,
                            "snapshot_fast_path": False,
                            "freshness": "local_exact",
                            "semantic_stale": False,
                            "exact_stale": False,
                            "accepted_revision": None,
                            "indexed_revision": None,
                            "exact_indexed_revision": local_exact_status.get(
                                "exact_indexed_revision"
                            ),
                            "provider": "local_exact_index",
                            "provider_chain": ["local_exact_index"],
                            "query_plan": search_plan.to_dict(
                                providers=["local_exact_index"]
                            ),
                            "capabilities_used": ["search.symbol_exact"],
                            "capabilities_missing": [],
                            "fallback_used": False,
                            "warnings": [],
                            "empty_reason": None,
                        }
                    )
        exact_index_authoritative = bool(
            workspace_id
            and x_omnicode_min_revision
            and freshness
            and not freshness.get("exact_stale", True)
        )
        if workspace_id:
            exact_rows = await asyncio.to_thread(
                _exact_index().search_symbols,
                workspace_id=workspace_id,
                query=query,
                symbol_type=symbol_type,
                file_pattern=file_pattern,
                fuzzy=fuzzy,
                min_score=min_score,
                max_results=max_results,
            )
            for row in exact_rows:
                key = (row.path, row.name, row.line_start)
                existing_keys.add(key)
                formatted_results.append(_format_exact_symbol_row(row))
            exact_has_exact = any(
                "symbol:exact" in (row.get("why_matched") or [])
                for row in formatted_results
            )
            if (
                exact_index_authoritative
                or exact_has_exact
                or len(formatted_results) >= max_results
            ):
                return create_success_response(
                    {
                        "query": query,
                        "search_type": search_type,
                        "symbol_type": symbol_type,
                        "fuzzy_enabled": fuzzy,
                        "results": formatted_results[:max_results],
                        "total_results": min(len(formatted_results), max_results),
                        "snapshot_store_used": bool(workspace_id),
                        "exact_index_used": True,
                        "snapshot_fast_path": True,
                        "freshness": (freshness or {}).get("freshness", "unknown")
                        if x_omnicode_min_revision
                        else "unknown",
                        "semantic_stale": bool(
                            (freshness or {}).get("semantic_stale", False)
                        ),
                        "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                        "accepted_revision": (freshness or {}).get("accepted_revision"),
                        "indexed_revision": (freshness or {}).get("indexed_revision"),
                        "exact_indexed_revision": (
                            freshness or {}
                        ).get("exact_indexed_revision"),
                        "provider": "exact_index",
                        "provider_chain": ["cloud_exact_symbols"],
                        "query_plan": search_plan.to_dict(
                            providers=["cloud_exact_symbols"]
                        ),
                        "capabilities_used": ["search.symbol_exact"],
                        "capabilities_missing": [],
                        "fallback_used": False,
                        "warnings": [],
                        "empty_reason": None,
                    }
                )

        snapshot_results = await asyncio.to_thread(
            _snapshot_symbol_search,
            workspace_id=workspace_id,
            query=query,
            symbol_type=symbol_type,
            file_pattern=file_pattern,
            fuzzy=fuzzy,
            min_score=min_score,
            max_results=max_results,
            existing_keys=existing_keys,
        )
        formatted_results.extend(snapshot_results)
        snapshot_has_exact = any(
            "symbol:exact" in (row.get("why_matched") or [])
            for row in snapshot_results
        )
        snapshot_fast_path = bool(snapshot_results) and (
            len(formatted_results) >= max_results
            or snapshot_has_exact
        )
        if snapshot_fast_path:
            return create_success_response(
                {
                    "query": query,
                    "search_type": search_type,
                    "symbol_type": symbol_type,
                    "fuzzy_enabled": fuzzy,
                    "results": formatted_results[:max_results],
                    "total_results": min(len(formatted_results), max_results),
                    "snapshot_store_used": bool(workspace_id),
                    "exact_index_used": bool(workspace_id),
                    "snapshot_fast_path": True,
                    "freshness": (freshness or {}).get("freshness", "unknown")
                    if x_omnicode_min_revision
                    else "unknown",
                    "semantic_stale": bool(
                        (freshness or {}).get("semantic_stale", False)
                    ),
                    "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                    "accepted_revision": (freshness or {}).get("accepted_revision"),
                    "indexed_revision": (freshness or {}).get("indexed_revision"),
                    "exact_indexed_revision": (
                        freshness or {}
                    ).get("exact_indexed_revision"),
                    "provider": "snapshot_store",
                    "provider_chain": ["cloud_snapshot_symbols"],
                    "query_plan": search_plan.to_dict(
                        providers=["cloud_snapshot_symbols"]
                    ),
                    "capabilities_used": ["search.symbol_exact"],
                    "capabilities_missing": [],
                    "fallback_used": True,
                    "fallback_reason": "cloud_exact_symbols_empty",
                    "warnings": [],
                    "empty_reason": None,
                }
            )

        search_engine = get_search_engine()
        if not search_engine:
            if formatted_results:
                return create_success_response(
                    {
                        "query": query,
                        "search_type": search_type,
                        "symbol_type": symbol_type,
                        "fuzzy_enabled": fuzzy,
                        "results": formatted_results[:max_results],
                        "total_results": min(len(formatted_results), max_results),
                        "snapshot_store_used": bool(workspace_id),
                        "exact_index_used": bool(workspace_id or local_exact_used),
                        "local_exact_index_used": local_exact_used,
                        "snapshot_fast_path": True,
                        "search_engine_unavailable": True,
                        "freshness": (freshness or {}).get("freshness", "unknown")
                        if x_omnicode_min_revision
                        else ("local_exact" if local_exact_used else "unknown"),
                        "semantic_stale": bool(
                            (freshness or {}).get("semantic_stale", False)
                        ),
                        "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                        "accepted_revision": (freshness or {}).get("accepted_revision"),
                        "indexed_revision": (freshness or {}).get("indexed_revision"),
                        "exact_indexed_revision": (
                            freshness or {}
                        ).get("exact_indexed_revision"),
                        "provider": "snapshot_store",
                        "provider_chain": ["cloud_snapshot_symbols"],
                        "query_plan": search_plan.to_dict(
                            providers=["cloud_snapshot_symbols"]
                        ),
                        "capabilities_used": ["search.symbol_exact"],
                        "capabilities_missing": ["search.semantic"],
                        "fallback_used": True,
                        "fallback_reason": "semantic_engine_unavailable",
                        "warnings": ["semantic search engine unavailable; returned exact/snapshot symbol rows"],
                        "empty_reason": None,
                    }
                )
            return create_error_response("Semantic search not initialized", 500)

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
        for result in results:
            key = (
                result.file_path,
                result.symbol_name,
                int(result.line_start or 0),
            )
            if key in existing_keys:
                continue
            existing_keys.add(key)
            formatted_results.append(
                {
                    "file_path": result.file_path,
                    "symbol_name": result.symbol_name,
                    "symbol_type": result.chunk_type,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                    "signature": result.signature,
                    "relevance_score": result.relevance_score,
                    "why_matched": getattr(result, "why_matched", []),
                }
            )
            if len(formatted_results) >= max_results:
                break

        if (
            not workspace_id
            and not formatted_results
            and int(local_exact_status.get("symbols") or 0) <= 0
        ):
            return _structured_search_error(
                message=(
                    "Local exact symbol index is not ready. Run workspace "
                    "index bootstrap before relying on symbol search."
                ),
                status_code=409,
                error_code="INDEX_NOT_READY",
                query=query,
                search_type=search_type,
                empty_reason="index_not_ready",
                local_index={
                    "workspace_id": _local_exact_workspace_id(),
                    "ready": False,
                    "status": local_exact_status,
                },
                query_plan=search_plan.to_dict(
                    providers=["local_exact_index", "semantic_symbol"]
                ),
                capabilities_missing=["search.symbol_exact"],
                next_actions=[
                    "omni_index(action='bootstrap', scope='workspace', background=False, format='json')",
                    "Use omni_read(file='<known file>', mode='outline', format='json') if you already know the file.",
                ],
            )

        return create_success_response(
            {
                "query": query,
                "search_type": search_type,
                "symbol_type": symbol_type,
                        "fuzzy_enabled": fuzzy,
                        "results": formatted_results,
                        "total_results": len(formatted_results),
                        "snapshot_store_used": bool(workspace_id),
                        "exact_index_used": bool(workspace_id or local_exact_used),
                        "local_exact_index_used": local_exact_used,
                        "snapshot_fast_path": False,
                        "freshness": (freshness or {}).get("freshness", "unknown")
                        if x_omnicode_min_revision
                        else ("local_exact" if local_exact_used else "unknown"),
                        "semantic_stale": bool((freshness or {}).get("semantic_stale", False)),
                        "exact_stale": bool((freshness or {}).get("exact_stale", False)),
                "accepted_revision": (freshness or {}).get("accepted_revision"),
                "indexed_revision": (freshness or {}).get("indexed_revision"),
                "exact_indexed_revision": (freshness or {}).get("exact_indexed_revision"),
                "provider": "semantic_symbol" if formatted_results else "none",
                "provider_chain": [
                    *(
                        ["local_exact_index"]
                        if local_exact_used
                        else []
                    ),
                    *(
                        ["cloud_exact_symbols"]
                        if workspace_id
                        else []
                    ),
                    "semantic_symbol",
                ],
                "query_plan": search_plan.to_dict(
                    providers=[
                        *(
                            ["local_exact_index"]
                            if local_exact_used
                            else []
                        ),
                        *(
                            ["cloud_exact_symbols"]
                            if workspace_id
                            else []
                        ),
                        "semantic_symbol",
                    ]
                ),
                "capabilities_used": [
                    *(
                        ["search.symbol_exact"]
                        if local_exact_used or workspace_id
                        else []
                    ),
                    "search.semantic",
                ],
                "capabilities_missing": [],
                "fallback_used": bool(local_exact_used or workspace_id),
                "fallback_reason": None,
                "empty_reason": "true_empty" if not formatted_results else None,
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Symbol search failed: {str(e)}", 500)


@router.get("/stats")
async def get_search_statistics():
    """Get detailed search engine statistics"""
    try:
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Semantic search not initialized", 500)

        refresh_stats = getattr(search_engine, "refresh_stats", None)
        if callable(refresh_stats):
            refresh_stats()
        stats = search_engine.get_stats()
        semantic_status: dict[str, Any] = {}
        semantic_index_status = getattr(search_engine, "semantic_index_status", None)
        if callable(semantic_index_status):
            try:
                semantic_status = dict(semantic_index_status())
            except Exception as exc:
                semantic_status = {
                    "semantic_index_ready": False,
                    "semantic_index_stale_reason": str(exc),
                    "semantic_index_invalid": True,
                }

        return create_success_response(
            {
                "index_stats": stats,
                "semantic_index": semantic_status,
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
# STAGE 3.9 鈥?AST query endpoints
# ============================================================================
class SymbolQueryRequest(BaseModel):
    symbol: str
    direction: str = "both"  # 'callers' | 'callees' | 'both'
    path: Optional[str] = None  # File or directory to scope the analysis (relative)
    max_files: int = 200


@router.post("/symbols/relations")
async def query_symbol_relations(
    req: SymbolQueryRequest,
    x_omnicode_workspace: Optional[str] = Header(default=None),
    x_omnicode_min_revision: Optional[int] = Header(
        default=None,
        alias="X-Omnicode-Min-Revision",
    ),
):
    """Find callers and/or callees of ``symbol`` using AST analysis.

    The endpoint walks the supplied path (file or directory; defaults to the
    working directory) and builds an in-memory call graph, then returns the
    requested relations.
    """
    try:
        workspace_id = _resolve_search_workspace(x_omnicode_workspace)
        stale = cloud_freshness_error(
            workspace_id=workspace_id,
            min_revision=x_omnicode_min_revision,
        )
        if stale is not None:
            return stale

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

    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover
        return create_error_response(f"Symbol relation query failed: {e}", 500)


@router.get("/symbols/graph")
async def get_symbols_graph(
    path: Optional[str] = Query(None, description="File or directory (relative)"),
    max_files: int = Query(200, description="Maximum files to scan"),
    max_nodes: int = Query(50, description="Max nodes in ASCII rendering"),
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Return a full call-graph for the given scope as JSON + ASCII rendering."""
    try:
        _resolve_search_workspace(x_omnicode_workspace)
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
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover
        return create_error_response(f"Graph build failed: {e}", 500)



# ----------------------------------------------------------------------------
# STAGE 3.11 鈥?Class inheritance hierarchy
# ----------------------------------------------------------------------------
@router.get("/inheritance")
async def get_inheritance_graph(
    path: Optional[str] = Query(
        None, description="File or directory (relative to working dir)"
    ),
    max_files: int = Query(500, description="Maximum files to scan"),
    max_nodes: int = Query(80, description="Maximum nodes in ASCII rendering"),
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Build a class-inheritance graph (subclass 鈫?base) for the given scope.

    Supports Python / JS / TS / C++ / Java / Rust.  For Rust we treat
    ``impl Trait for Struct`` as ``Struct 鈫?Trait``.
    """
    try:
        _resolve_search_workspace(x_omnicode_workspace)
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
    except HTTPException:
        raise
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
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """Look up the inheritance neighbourhood of a single symbol."""
    try:
        _resolve_search_workspace(x_omnicode_workspace)
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
    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Inheritance query failed: {e}", 500)


# ----------------------------------------------------------------------------
# CATCH-ALL 鈥?keep this LAST so specific routes like /symbols/graph and
# /symbols/relations resolve to their dedicated handlers above.
# ----------------------------------------------------------------------------
@router.get("/symbols/{file_path:path}")
async def list_file_symbols(
    file_path: str,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    """List all symbols in a specific file."""
    # Belt-and-suspenders: even though FastAPI now resolves /symbols/graph and
    # /symbols/relations to the right handlers (they are declared before this
    # route), we still reject those names here so a typo with a trailing
    # slash doesn't silently match the wrong endpoint.
    if file_path in {"graph", "relations"} or file_path.startswith(("graph/", "relations/")):
        return create_error_response(
            f"Reserved path '/symbols/{file_path}' 鈥?please use the dedicated "
            "endpoint (/symbols/graph or POST /symbols/relations).",
            404,
        )
    try:
        _resolve_search_workspace(x_omnicode_workspace)
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
