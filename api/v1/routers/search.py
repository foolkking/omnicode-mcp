"""
Search and indexing endpoints
Provides semantic search, text search, symbol search, and index management
"""

import asyncio
import fnmatch
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from api.v1.routers.freshness import cloud_freshness_error, cloud_freshness_state
from core import get_ast_parser, get_search_engine
from core.config import get_settings
from omnicode.ast_engine.graph import CallGraphBuilder
from omnicode.search.models import SearchRequest
from omnicode_core.workspace.registry import get_workspace_registry
from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.semantic_index_policy import (
    semantic_index_decision,
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
) -> list[dict[str, Any]]:
    """Boost source files with strong lexical overlap for natural queries."""
    if not workspace_id or max_results <= 0:
        return []
    tokens = _semantic_query_tokens(query)
    if len(tokens) < 2:
        return []

    token_variants = {token: _query_token_variants(token) for token in tokens}
    patterns = (
        [p.strip() for p in file_pattern.split(",") if p.strip()]
        if file_pattern
        else None
    )
    store = CloudSnapshotStore()
    scored: list[tuple[float, dict[str, Any]]] = []

    for record in store.list_records(workspace_id=workspace_id):
        if not _snapshot_patterns_match(record.path, patterns):
            continue
        path_lower = record.path.lower()
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
    return [row for _score, row in scored[:max_results]]


def _run_snapshot_index_blocking(
    workspace_id: str,
    *,
    force: bool = False,
    scope: str = "semantic",
    progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Index snapshot-store content in a worker thread."""

    async def _index() -> dict[str, Any]:
        search_engine = get_search_engine()
        if not search_engine:
            raise RuntimeError("Semantic search not initialized")

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
            not force
            and indexed_revision_watermark > 0
            and indexed_total_files > 0
        )
        indexed_hashes: dict[str, str] = {}
        indexed_file_hashes = getattr(search_engine, "indexed_file_hashes", None)
        if not force and callable(indexed_file_hashes):
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
        deleted_index_entries = 0
        batch_size = 50
        batch: list[tuple[str, str, dict[str, Any]]] = []
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
                "deleted_index_entries": deleted_index_entries,
                "indexed_revision_watermark": indexed_revision_watermark,
                "current_path": current_path,
            }

        emit_progress(**progress_snapshot(None))

        async def flush_batch() -> None:
            nonlocal indexed_chunks, batch
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

        for record in records:
            records_processed += 1
            if not force:
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
            read_record_text = getattr(store, "read_record_text", None)
            if callable(read_record_text):
                content = read_record_text(workspace_id=workspace_id, record=record)
            else:  # pragma: no cover - compatibility for older injected fakes
                content = store.read_text(workspace_id=workspace_id, path=record.path)
            if content is None:
                if records_processed % 25 == 0 or records_processed == len(records):
                    emit_progress(**progress_snapshot(record.path))
                continue
            if scope != "semantic":
                include_semantic, reason = semantic_index_decision(
                    record.path,
                    content,
                    {
                        "phase": "snapshot_bootstrap",
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
                    {
                        "content_hash": record.hash,
                        "snapshot_hash": record.hash,
                        "snapshot_revision": record.revision,
                        "workspace_id": workspace_id,
                    },
                )
            )
            indexed_files += 1
            if len(batch) >= batch_size:
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
            "semantic_index_policy": semantic_index_policy_payload(),
            "records_seen": len(records),
            "records_total": len(records),
            "indexed_files": indexed_files,
            "indexed_chunks": indexed_chunks,
            "skipped_unchanged": skipped_unchanged,
            "skipped_by_indexed_revision": skipped_by_indexed_revision,
            "skipped_by_policy": skipped_by_policy,
            "skip_policy_reasons": dict(skip_policy_reasons),
            "deleted_index_entries": deleted_index_entries,
            "accepted_revision": accepted_revision,
            "indexed_revision": indexed_revision,
            "indexed_revision_watermark": indexed_revision_watermark,
            "batch_size": batch_size,
            "stats": stats,
        }

    return asyncio.run(_index())


def _public_snapshot_index_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"thread"}
    }


def _snapshot_index_job_key(workspace_id: str) -> str:
    return workspace_id


def _start_snapshot_index_job(
    workspace_id: str,
    *,
    force: bool = False,
    scope: str = "semantic",
) -> dict[str, Any]:
    key = _snapshot_index_job_key(workspace_id)
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        existing = _SNAPSHOT_INDEX_JOBS.get(key)
        if isinstance(existing, dict) and existing.get("state") == "running":
            return _public_snapshot_index_job(existing)
        job = {
            "job_id": f"{workspace_id}:{int(time.time() * 1000)}",
            "workspace_id": workspace_id,
            "state": "running",
            "force": force,
            "scope": scope,
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
            "deleted_index_entries": 0,
            "indexed_revision_watermark": None,
            "current_path": None,
            "last_update_at": None,
            "result": None,
            "error": None,
        }
        _SNAPSHOT_INDEX_JOBS[key] = job

    def _runner() -> None:
        started = time.monotonic()

        def _progress(fields: dict[str, Any]) -> None:
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(fields)
                job["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                job["last_update_at"] = time.time()

        try:
            result = _run_snapshot_index_blocking(
                workspace_id,
                force=force,
                scope=scope,
                progress=_progress,
            )
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(
                    {
                        "state": "completed",
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
                        "deleted_index_entries": result.get("deleted_index_entries"),
                        "indexed_revision_watermark": result.get(
                            "indexed_revision_watermark"
                        ),
                        "current_path": None,
                        "last_update_at": time.time(),
                        "result": result,
                        "error": None,
                    }
                )
        except Exception as exc:  # pragma: no cover - defensive thread boundary
            with _SNAPSHOT_INDEX_JOBS_LOCK:
                job.update(
                    {
                        "state": "failed",
                        "completed_at": time.time(),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )

    thread = threading.Thread(
        target=_runner,
        name=f"snapshot-index-{workspace_id}",
        daemon=True,
    )
    with _SNAPSHOT_INDEX_JOBS_LOCK:
        job["thread"] = thread
    thread.start()
    return _public_snapshot_index_job(job)


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
        )
        if stale is not None:
            return stale

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
        indexed_results = await search_engine.search(request)

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
            {
                "query": request.query,
                "search_type": request.search_type,
                "results": formatted_results[: request.max_results],
                "total_results": len(formatted_results),
                "snapshot_store_used": bool(workspace_id),
                "snapshot_exact_boost": any(
                    "semantic:exact_boost" in (row.get("why_matched") or [])
                    for row in snapshot_boost
                ),
                "snapshot_lexical_boost": any(
                    "semantic:lexical_boost" in (row.get("why_matched") or [])
                    for row in snapshot_boost
                ),
            }
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
        "semantic",
        description=(
            "Snapshot workspace semantic bootstrap scope: 'semantic' indexes all "
            "snapshot text into the semantic store; 'exact_policy' applies the "
            "configured semantic extension/size policy."
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
        scope_value = (scope or "semantic").strip().lower()
        if scope_value not in {"semantic", "exact_policy"}:
            return create_error_response(
                "scope must be one of: semantic, exact_policy",
                400,
            )
        effective_workspace_id = _resolve_search_workspace(
            x_omnicode_workspace or workspace_id
        )
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
        with _SNAPSHOT_INDEX_JOBS_LOCK:
            job = _SNAPSHOT_INDEX_JOBS.get(key)
            if not isinstance(job, dict):
                return create_success_response(
                    {
                        "workspace_id": effective_workspace_id,
                        "background": True,
                        "state": "idle",
                        "job": None,
                    }
                )
            return create_success_response(
                {
                    "workspace_id": effective_workspace_id,
                    "background": True,
                    "state": job.get("state"),
                    "job": _public_snapshot_index_job(job),
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Index status failed: {str(e)}", 500)


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
    that matches ``file_pattern`` — not just files that have been
    indexed — so freshly-added code is searchable immediately.
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

        from omnicode_core.search.text_grep import grep_workspace

        settings = get_settings()
        patterns = (
            [p.strip() for p in file_pattern.split(",") if p.strip()]
            if file_pattern
            else None
        )

        results = []
        existing_keys: set[tuple[str, int]] = set()
        exact_index_used = False
        exact_line_fts_available = False
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
            except Exception:
                exact_line_fts_available = False
            if exact_line_fts_available:
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

        if (exact_index_authoritative and exact_index_used) or len(results) >= max_results:
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

        hits = grep_workspace(
            workspace_root=settings.WORKING_DIR,
            query=query,
            file_patterns=patterns,
            max_results=max_results,
            context_lines=context_lines,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            merge_adjacent=merge_adjacent,
        )

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
            }
            merged_extra = list(getattr(h, "_merged_lines", []) or [])
            if merged_extra:
                row["merged_lines"] = merged_extra
                row["why_matched"].append(f"text:merged({len(merged_extra) + 1})")
            results.append(row)

        if len(results) < max_results:
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

        # Snapshot-store rows come first so a freshly-synced large repo can
        # answer exact symbol lookups before the vector/symbol DB is fully
        # bootstrapped.
        formatted_results = []
        existing_keys: set[tuple[str, str, int]] = set()
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
                formatted_results.append(
                    {
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
                )
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
                        "exact_index_used": bool(workspace_id),
                        "snapshot_fast_path": True,
                        "search_engine_unavailable": True,
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

        return create_success_response(
            {
                "query": query,
                "search_type": search_type,
                "symbol_type": symbol_type,
                "fuzzy_enabled": fuzzy,
                "results": formatted_results,
                "total_results": len(formatted_results),
                "snapshot_store_used": bool(workspace_id),
                "exact_index_used": bool(workspace_id),
                "snapshot_fast_path": False,
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
# STAGE 3.11 — Class inheritance hierarchy
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
    """Build a class-inheritance graph (subclass → base) for the given scope.

    Supports Python / JS / TS / C++ / Java / Rust.  For Rust we treat
    ``impl Trait for Struct`` as ``Struct → Trait``.
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
# CATCH-ALL — keep this LAST so specific routes like /symbols/graph and
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
            f"Reserved path '/symbols/{file_path}' — please use the dedicated "
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
