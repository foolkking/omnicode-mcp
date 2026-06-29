"""Hybrid workspace sync endpoints.

This is the cloud-side protocol surface used by local MCP clients before
cloud-backed search/context/impact routing is enabled. Step 6 keeps accepted
state in memory; the snapshot store is introduced in the next architecture
step.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core import get_search_engine
from core.config import get_settings
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.graph_index import WorkspaceGraphIndex
from omnicode_core.workspace.local import LocalWorkspace, WorkspacePathError
from omnicode_core.workspace.readiness import (
    build_index_readiness_contract,
    contract_summary,
)
from omnicode_core.workspace.registry import get_workspace_registry
from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from omnicode_core.workspace.semantic_index_policy import (
    merge_semantic_coverages,
    semantic_coverage_for_batch,
    semantic_index_decision,
    semantic_index_metadata,
    semantic_index_policy_payload,
)
from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    SnapshotStoreError,
)

router = APIRouter(prefix="/sync", tags=["sync"])
_STATUS_CACHE_TTL_SECONDS = float(
    os.environ.get("OMNICODE_SYNC_STATUS_CACHE_TTL_SECONDS", "10.0") or "10.0"
)
_STATUS_PROBE_TIMEOUT_SECONDS = float(
    os.environ.get("OMNICODE_SYNC_STATUS_PROBE_TIMEOUT_SECONDS", "1.0")
    or "1.0"
)


class SyncFileIn(BaseModel):
    path: str
    hash: str
    size: int = Field(ge=0)
    mtime_ms: int = Field(ge=0)
    encoding: str = "utf-8"
    content: str


class SyncDeleteIn(BaseModel):
    path: str


class SyncBatchIn(BaseModel):
    client_id: str = ""
    base_revision: int = Field(default=0, ge=0)
    client_revision: int = Field(default=0, ge=0)
    files: List[SyncFileIn] = Field(default_factory=list)
    deletes: List[SyncDeleteIn] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SyncBarrierIn(BaseModel):
    min_revision: int = Field(ge=0)
    paths: List[str] = Field(default_factory=list)
    wait_ms: int = Field(default=0, ge=0)


@dataclass
class _SyncedFile:
    path: str
    hash: str
    size: int
    mtime_ms: int
    encoding: str
    content: str
    accepted_revision: int


@dataclass
class _IndexJob:
    workspace_id: str
    revision: int
    changed_files: list[tuple[str, str] | tuple[str, str, dict[str, Any]]]
    deleted_paths: list[str]
    graph_changed_files: list[
        tuple[str, str] | tuple[str, str, dict[str, Any]]
    ] = field(default_factory=list)
    semantic_coverage: str = "unknown"
    graph_coverage_complete: bool = False


@dataclass
class _CoalescedIndexJob:
    workspace_id: str
    revision: int = 0
    changed_files: Dict[
        str,
        tuple[str, str, dict[str, Any]],
    ] = field(default_factory=dict)
    graph_changed_files: Dict[
        str,
        tuple[str, str, dict[str, Any]],
    ] = field(default_factory=dict)
    changed_file_bytes: Dict[str, int] = field(default_factory=dict)
    changed_bytes: int = 0
    deleted_paths: set[str] = field(default_factory=set)
    semantic_coverages: set[str] = field(default_factory=set)
    graph_coverage_complete: bool = False
    job_count: int = 0


@dataclass
class _SyncWorkspaceState:
    workspace_id: str
    accepted_revision: int = 0
    indexed_revision: int = 0
    files: Dict[str, _SyncedFile] = field(default_factory=dict)
    deleted_paths: set[str] = field(default_factory=set)
    indexing: bool = False
    last_index_error: Optional[str] = None
    last_index_elapsed_ms: Optional[int] = None
    last_index_revision: int = 0
    index_queue_depth: int = 0
    index_worker_running: bool = False
    index_jobs_enqueued: int = 0
    index_jobs_completed: int = 0
    current_index_revision: int = 0
    current_index_files: int = 0
    current_index_bytes: int = 0
    current_index_deletes: int = 0
    current_index_job_count: int = 0
    current_index_started_at: Optional[float] = None
    last_batch_elapsed_ms: Optional[int] = None
    last_batch_files: int = 0
    last_batch_deletes: int = 0
    last_semantic_files_enqueued: int = 0
    last_semantic_files_skipped: int = 0
    last_semantic_skip_reasons: Dict[str, int] = field(default_factory=dict)
    semantic_index_coverage: str = "unknown"
    semantic_initial_exact_only: bool = False
    last_sync_metadata: Dict[str, Any] = field(default_factory=dict)
    state_loaded: bool = False
    file_count: int = 0
    delete_count: int = 0


_LOCK = threading.RLock()
_SYNC_STATES: Dict[str, _SyncWorkspaceState] = {}
_SNAPSHOT_STORE = CloudSnapshotStore()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTO_FTS_LINE_LIMIT = int(
    os.environ.get("OMNICODE_EXACT_LINE_FTS_MAX_LINES", "50000") or "50000"
)
_INDEX_QUEUE: Optional[asyncio.Queue[_IndexJob]] = None
_INDEX_WORKER_TASK: Optional[asyncio.Task[None]] = None
_INDEX_LOOP: Optional[asyncio.AbstractEventLoop] = None
_GRAPH_INDEX_INSTANCE: Optional[WorkspaceGraphIndex] = None
_GRAPH_INDEX_STORE_ID: Optional[int] = None
_STATUS_CACHE: dict[str, tuple[tuple[Any, ...], float, dict[str, Any]]] = {}
_STATUS_CACHE_LOCKS: dict[str, asyncio.Lock] = {}
_STATUS_CACHE_LOCKS_GUARD = threading.RLock()


def _exact_index() -> SnapshotExactIndex:
    return SnapshotExactIndex(store=_SNAPSHOT_STORE)


def _graph_index() -> WorkspaceGraphIndex:
    global _GRAPH_INDEX_INSTANCE, _GRAPH_INDEX_STORE_ID
    store_id = id(_SNAPSHOT_STORE)
    if (
        _GRAPH_INDEX_INSTANCE is None
        or _GRAPH_INDEX_STORE_ID != store_id
    ):
        _GRAPH_INDEX_INSTANCE = WorkspaceGraphIndex(store=_SNAPSHOT_STORE)
        _GRAPH_INDEX_STORE_ID = store_id
    return _GRAPH_INDEX_INSTANCE


def _sync_graph_rebuild_mode() -> str:
    raw = (os.environ.get("OMNICODE_SYNC_GRAPH_REBUILD_MODE") or "small").strip().lower()
    if raw in {"always", "on", "true", "1"}:
        return "always"
    if raw in {"off", "none", "false", "0", "never"}:
        return "off"
    return "small"


def _sync_graph_rebuild_max_files() -> int:
    raw = (os.environ.get("OMNICODE_SYNC_GRAPH_REBUILD_MAX_FILES") or "500").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 500


def _should_sync_rebuild_graph(
    *,
    workspace_id: str,
    graph_status: dict[str, Any],
) -> bool:
    if not (
        not graph_status.get("coverage_complete")
        or graph_status.get("needs_rebuild")
    ):
        return False
    mode = _sync_graph_rebuild_mode()
    if mode == "off":
        return False
    if mode == "always":
        return True
    try:
        snapshot_status = _SNAPSHOT_STORE.status(workspace_id)
        file_count = int(snapshot_status.get("file_count") or 0)
    except Exception:
        file_count = 0
    return file_count <= _sync_graph_rebuild_max_files()


def _initial_sync_graph_coverage_complete(
    metadata: dict[str, Any],
    *,
    snapshot_file_count: int,
) -> bool:
    phase = str(metadata.get("phase") or "").strip().lower()
    if phase != "initial_sync":
        return False
    if bool(metadata.get("truncated") or metadata.get("initial_sync_truncated")):
        return False
    expected = 0
    for key in ("files_pushed", "files_seen"):
        try:
            expected = max(expected, int(metadata.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return bool(expected > 0 and int(snapshot_file_count) >= expected)


def _status_cache_lock(workspace_id: str) -> asyncio.Lock:
    with _STATUS_CACHE_LOCKS_GUARD:
        lock = _STATUS_CACHE_LOCKS.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            _STATUS_CACHE_LOCKS[workspace_id] = lock
        return lock


def _status_cache_key(state: _SyncWorkspaceState) -> tuple[Any, ...]:
    return (
        state.accepted_revision,
        state.indexed_revision,
        state.indexing,
        state.index_queue_depth,
        state.index_worker_running,
        state.index_jobs_enqueued,
        state.index_jobs_completed,
        state.current_index_revision,
        state.current_index_files,
        state.current_index_deletes,
        state.last_index_revision,
        state.last_index_error,
        state.last_batch_elapsed_ms,
        state.last_batch_files,
        state.last_batch_deletes,
        state.last_semantic_files_enqueued,
        state.last_semantic_files_skipped,
        tuple(sorted(state.last_semantic_skip_reasons.items())),
        state.semantic_index_coverage,
        state.semantic_initial_exact_only,
        state.file_count,
        state.delete_count,
    )


def _status_cache_get(
    workspace_id: str,
    cache_key: tuple[Any, ...],
) -> Optional[dict[str, Any]]:
    if _STATUS_CACHE_TTL_SECONDS <= 0:
        return None
    item = _STATUS_CACHE.get(workspace_id)
    if item is None:
        return None
    key, created_at, payload = item
    if key != cache_key:
        return None
    if time.monotonic() - created_at > _STATUS_CACHE_TTL_SECONDS:
        return None
    cached = dict(payload)
    cached["status_cache_hit"] = True
    return cached


def _status_cache_put(
    workspace_id: str,
    cache_key: tuple[Any, ...],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if _STATUS_CACHE_TTL_SECONDS <= 0:
        payload["status_cache_hit"] = False
        return payload
    cached_payload = dict(payload)
    cached_payload["status_cache_hit"] = False
    _STATUS_CACHE[workspace_id] = (
        cache_key,
        time.monotonic(),
        dict(cached_payload),
    )
    return cached_payload


def _status_cache_invalidate(workspace_id: str) -> None:
    _STATUS_CACHE.pop(workspace_id, None)


def _line_fts_mode() -> str:
    raw = os.environ.get("OMNICODE_EXACT_LINE_FTS", "").strip().lower()
    if raw in {"0", "false", "no", "off", "disabled"}:
        return "off"
    if raw in {"1", "true", "yes", "on", "force", "forced"}:
        return "on"
    return "auto"


def _content_line_count(content: str) -> int:
    if not content:
        return 0
    return content.count("\n") + 1


def _sync_exact_fts_decision(
    *,
    workspace_id: str,
    files: list[dict[str, Any]],
) -> tuple[Optional[bool], Optional[str]]:
    """Decide whether this sync batch should populate SQLite FTS rows.

    Symbol and plain line indexes remain authoritative for large repositories.
    FTS5 is useful for small/medium workspaces, but keeping it enabled during
    a large initial sync can dominate /sync/batch latency and bloat the DB.
    """
    mode = _line_fts_mode()
    if mode == "off":
        return False, "disabled_by_env"
    if mode == "on":
        return None, None

    batch_lines = sum(_content_line_count(str(item.get("content") or "")) for item in files)
    try:
        exact_status = _exact_index().status(workspace_id=workspace_id)
    except Exception:
        exact_status = {}
    existing_reason = str(exact_status.get("line_fts_reason") or "")
    existing_lines = int(exact_status.get("lines") or 0)
    if existing_reason == "disabled_for_large_workspace":
        return False, "disabled_for_large_workspace"
    if _AUTO_FTS_LINE_LIMIT > 0 and existing_lines + batch_lines > _AUTO_FTS_LINE_LIMIT:
        return False, "disabled_for_large_workspace"
    return None, None


def _error(message: str, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return payload


def _resolve_workspace(workspace_header: Optional[str]) -> LocalWorkspace:
    if not workspace_header or not workspace_header.strip():
        raise HTTPException(
            status_code=400,
            detail="X-Omnicode-Workspace header is required for /sync",
        )

    settings = get_settings()
    registry = get_workspace_registry()
    workspace_id = workspace_header.strip()
    try:
        resolved = resolve_workspace_request(
            workspace_id,
            working_dir=settings.WORKING_DIR,
            registry=registry,
        )
    except WorkspaceResolutionError as exc:
        if exc.status_code == 404:
            try:
                registry.add(
                    name=workspace_id,
                    path=settings.WORKING_DIR,
                    set_active=False,
                    workspace_id=workspace_id,
                )
                resolved = resolve_workspace_request(
                    workspace_id,
                    working_dir=settings.WORKING_DIR,
                    registry=registry,
                )
            except Exception as register_exc:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"workspace_id not registered and auto-registration "
                        f"failed for {workspace_id}: {register_exc}"
                    ),
                ) from register_exc
        else:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return LocalWorkspace(
            root=Path(resolved.working_dir),
            workspace_id=resolved.workspace_id or workspace_id,
        )
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _state_for(workspace_id: str) -> _SyncWorkspaceState:
    with _LOCK:
        state = _SYNC_STATES.get(workspace_id)
        if state is None:
            state = _SyncWorkspaceState(workspace_id=workspace_id)
            _SYNC_STATES[workspace_id] = state
        return state


def _content_hash(content: str, encoding: str) -> str:
    return hashlib.sha256(content.encode(encoding, errors="replace")).hexdigest()


def _expected_hash(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value.lower()


def _validate_file_payload(item: SyncFileIn) -> None:
    expected = _expected_hash(item.hash)
    if not _SHA256_RE.match(expected):
        raise ValueError(
            f"invalid hash for {item.path}: expected sha256:<64 lowercase hex chars>"
        )
    actual = _content_hash(item.content, item.encoding)
    if expected and expected != actual:
        raise ValueError(
            f"hash mismatch for {item.path}: expected sha256:{expected}, got sha256:{actual}"
        )
    encoded_size = len(item.content.encode(item.encoding, errors="replace"))
    if item.size != encoded_size:
        raise ValueError(
            f"size mismatch for {item.path}: expected {item.size}, got {encoded_size}"
        )


def _apply_snapshot_status(
    state: _SyncWorkspaceState,
    snapshot_status: dict[str, Any],
) -> None:
    accepted = int(snapshot_status.get("accepted_revision", 0))
    indexed = int(snapshot_status.get("indexed_revision", 0))
    state.accepted_revision = max(state.accepted_revision, accepted)
    state.indexed_revision = max(state.indexed_revision, indexed)
    state.file_count = int(snapshot_status.get("file_count", state.file_count))
    state.delete_count = int(snapshot_status.get("delete_count", state.delete_count))
    state.state_loaded = True


async def _ensure_state_loaded(state: _SyncWorkspaceState) -> None:
    if state.state_loaded:
        return
    snapshot_status = await asyncio.to_thread(_SNAPSHOT_STORE.status, state.workspace_id)
    with _LOCK:
        _apply_snapshot_status(state, snapshot_status)


def _snapshot_status_from_state(state: _SyncWorkspaceState) -> dict[str, Any]:
    return {
        "latest_revision": state.accepted_revision,
        "accepted_revision": state.accepted_revision,
        "indexed_revision": state.indexed_revision,
        "file_count": state.file_count,
        "delete_count": state.delete_count,
    }


def _merge_snapshot_status_with_state(
    state: _SyncWorkspaceState,
    snapshot_status: dict[str, Any],
) -> dict[str, Any]:
    """Return a monotonic snapshot/status view for one response.

    The bounded disk probe and the background index worker run concurrently.
    A probe can read ``status.json`` immediately before ``mark_indexed`` while
    the worker commits the newer revision to process state immediately after.
    Without this merge a single payload can claim indexing is complete at the
    top level while exposing an older ``snapshot_store.indexed_revision``.

    Process state only advances after the snapshot store accepts or indexes a
    revision, so taking the maximum is monotonic and does not manufacture a
    revision. File/delete counts also use the newest known in-process values.
    """
    merged = dict(snapshot_status)
    accepted = max(
        int(merged.get("accepted_revision", 0) or 0),
        int(merged.get("latest_revision", 0) or 0),
        state.accepted_revision,
    )
    indexed = max(
        int(merged.get("indexed_revision", 0) or 0),
        state.indexed_revision,
    )
    merged["latest_revision"] = accepted
    merged["accepted_revision"] = accepted
    merged["indexed_revision"] = min(indexed, accepted)
    merged["file_count"] = max(
        int(merged.get("file_count", 0) or 0),
        state.file_count,
    )
    merged["delete_count"] = max(
        int(merged.get("delete_count", 0) or 0),
        state.delete_count,
    )
    return merged


def record_external_indexed_revision(workspace_id: str, revision: int) -> None:
    """Update process-local sync status after an explicit index bootstrap."""
    with _LOCK:
        state = _state_for(workspace_id)
        state.indexed_revision = max(state.indexed_revision, revision)
        state.last_index_revision = max(state.last_index_revision, revision)
        state.indexing = (
            state.index_queue_depth > 0
            or state.indexed_revision < state.accepted_revision
        )
        state.last_index_error = None
        _status_cache_invalidate(workspace_id)


async def _recover_stalled_index_update(state: _SyncWorkspaceState) -> None:
    """Best-effort recovery when the async index worker stopped with work queued.

    Uvicorn keeps one event loop alive, so the normal worker path should handle
    indexing. Some embedded/test clients recreate request loops, which can leave
    queued jobs without a running worker. Status is the right repair point: it is
    already the freshness authority, and a one-shot recovery is better than
    reporting stale forever.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - status is async in production
        loop = None
    worker_task_alive = bool(
        _INDEX_WORKER_TASK is not None
        and not _INDEX_WORKER_TASK.done()
        and _INDEX_LOOP is not None
        and not _INDEX_LOOP.is_closed()
    )
    worker_task_active_here = bool(worker_task_alive and _INDEX_LOOP is loop)
    if worker_task_active_here:
        for _ in range(10):
            with _LOCK:
                current = _state_for(state.workspace_id)
                if current.indexed_revision >= current.accepted_revision:
                    return
                still_busy = current.index_worker_running or current.index_queue_depth > 0
            if not still_busy:
                break
            await asyncio.sleep(0.01)
        worker_task_alive = bool(
            _INDEX_WORKER_TASK is not None
            and not _INDEX_WORKER_TASK.done()
            and _INDEX_LOOP is not None
            and not _INDEX_LOOP.is_closed()
        )

    with _LOCK:
        current = _state_for(state.workspace_id)
        if current.indexed_revision >= current.accepted_revision:
            return
        if current.index_worker_running and worker_task_alive:
            return
        if current.index_queue_depth > 0 and worker_task_alive:
            return
        target_revision = current.accepted_revision
        pending_rows = [
            row
            for row in current.files.values()
            if row.accepted_revision > current.indexed_revision
        ]
        deleted_paths = sorted(current.deleted_paths)
        metadata = dict(current.last_sync_metadata)
        pending_jobs = max(current.index_queue_depth, 1)
        current.index_worker_running = True
        current.indexing = True
        current.current_index_revision = target_revision
        current.current_index_files = len(pending_rows)
        current.current_index_bytes = sum(
            _index_content_bytes(row.content) for row in pending_rows
        )
        current.current_index_deletes = len(deleted_paths)
        current.current_index_job_count = pending_jobs
        current.current_index_started_at = time.monotonic()

    index_files: list[tuple[str, str, dict[str, Any]]] = []
    graph_files: list[tuple[str, str, dict[str, Any]]] = []
    semantic_files_skipped = 0
    semantic_skip_reasons: dict[str, int] = {}
    for row in pending_rows:
        graph_files.append(
            (
                row.path,
                row.content,
                {
                    "content_hash": row.hash,
                    "snapshot_hash": row.hash,
                    "snapshot_revision": target_revision,
                    "workspace_id": state.workspace_id,
                },
            )
        )
        include_semantic, reason = semantic_index_decision(
            row.path,
            row.content,
            metadata,
        )
        if not include_semantic:
            semantic_files_skipped += 1
            semantic_skip_reasons[reason] = semantic_skip_reasons.get(reason, 0) + 1
            continue
        index_files.append(
            (
                row.path,
                row.content,
                semantic_index_metadata(
                    row.path,
                    row.content,
                    {
                        "content_hash": row.hash,
                        "snapshot_hash": row.hash,
                        "snapshot_revision": target_revision,
                        "workspace_id": state.workspace_id,
                    },
                ),
            )
        )
    semantic_coverage = semantic_coverage_for_batch(
        files_enqueued=len(index_files),
        files_skipped=semantic_files_skipped,
        skip_reasons=semantic_skip_reasons,
        deletes=len(deleted_paths),
    )

    started = time.monotonic()
    try:
        indexed_revision = await asyncio.to_thread(
            _invoke_index_update_blocking,
            state.workspace_id,
            target_revision,
            index_files,
            deleted_paths,
            semantic_coverage,
            graph_files,
            graph_coverage_complete=_initial_sync_graph_coverage_complete(
                metadata,
                snapshot_file_count=current.file_count,
            ),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with _LOCK:
            current = _state_for(state.workspace_id)
            current.indexed_revision = max(
                current.indexed_revision,
                indexed_revision,
            )
            current.index_queue_depth = 0
            current.index_worker_running = False
            current.indexing = current.indexed_revision < current.accepted_revision
            current.last_index_error = None
            current.last_index_elapsed_ms = elapsed_ms
            current.last_index_revision = current.indexed_revision
            current.index_jobs_completed += pending_jobs
            current.current_index_revision = 0
            current.current_index_files = 0
            current.current_index_bytes = 0
            current.current_index_deletes = 0
            current.current_index_job_count = 0
            current.current_index_started_at = None
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with _LOCK:
            current = _state_for(state.workspace_id)
            current.index_worker_running = False
            current.indexing = current.index_queue_depth > 0
            current.last_index_error = str(exc)
            current.last_index_elapsed_ms = elapsed_ms
            current.current_index_revision = 0
            current.current_index_files = 0
            current.current_index_bytes = 0
            current.current_index_deletes = 0
            current.current_index_job_count = 0
            current.current_index_started_at = None


def _status_payload(
    state: _SyncWorkspaceState,
    snapshot_status: dict,
    exact_status: Optional[dict[str, Any]] = None,
    semantic_status: Optional[dict[str, Any]] = None,
    graph_status: Optional[dict[str, Any]] = None,
) -> dict:
    semantic_bootstrap_status: dict[str, Any] = {
        "workspace_id": state.workspace_id,
        "background": True,
        "state": "unknown",
        "job": None,
        "error": None,
    }
    try:
        from api.v1.routers.search import snapshot_index_job_status

        semantic_bootstrap_status = snapshot_index_job_status(state.workspace_id)
    except Exception as exc:  # noqa: BLE001
        semantic_bootstrap_status["error"] = f"{exc.__class__.__name__}: {exc}"
    semantic_bootstrap_job = semantic_bootstrap_status.get("job")
    semantic_bootstrap_running = bool(
        isinstance(semantic_bootstrap_job, dict)
        and semantic_bootstrap_job.get("state") == "running"
    )
    pending_files = max(state.accepted_revision - state.indexed_revision, 0)
    exact_status = exact_status or {}
    snapshot_file_count = int(snapshot_status.get("file_count", state.file_count) or 0)
    snapshot_delete_count = int(
        snapshot_status.get("delete_count", state.delete_count) or 0
    )
    exact_indexed_revision = int(exact_status.get("exact_indexed_revision") or 0)
    snapshot_semantic_coverage = str(
        snapshot_status.get("semantic_index_coverage") or "unknown"
    )
    state_semantic_coverage = (state.semantic_index_coverage or "unknown").strip()
    semantic_index_coverage = (
        state_semantic_coverage
        if state_semantic_coverage and state_semantic_coverage != "unknown"
        else snapshot_semantic_coverage
    )
    semantic_initial_exact_only = bool(
        state.semantic_initial_exact_only
        or snapshot_status.get("semantic_initial_exact_only", False)
        or semantic_index_coverage == "exact_only_initial_sync"
    )
    index_worker_busy = bool(
        state.index_worker_running
        or state.index_queue_depth > 0
        or state.indexing
        or pending_files > 0
        or semantic_bootstrap_running
    )
    graph_status = graph_status or {}
    graph_index_ready = bool(graph_status.get("ready", False))
    readiness_contract = build_index_readiness_contract(
        workspace_id=state.workspace_id,
        accepted_revision=state.accepted_revision,
        semantic_indexed_revision=state.indexed_revision,
        exact_indexed_revision=exact_indexed_revision,
        snapshot_files=snapshot_file_count,
        snapshot_deletes=snapshot_delete_count,
        exact_files=exact_status.get("files") or 0,
        exact_symbols=exact_status.get("symbols") or 0,
        exact_lines=exact_status.get("lines") or 0,
        exact_line_fts_available=bool(
            exact_status.get("line_fts_available", False)
        ),
        semantic_index_coverage=semantic_index_coverage,
        semantic_initial_exact_only=semantic_initial_exact_only,
        index_worker_busy=index_worker_busy,
        last_index_error=state.last_index_error,
        graph_index_ready=graph_index_ready,
    )
    readiness_summary = contract_summary(readiness_contract)
    exact_index_ready = readiness_summary["exact_index_ready"]
    exact_pending_revisions = readiness_summary["exact_pending_revisions"]
    semantic_index_ready = readiness_summary["semantic_index_ready"]
    search_degraded = readiness_summary["search_degraded"]
    semantic_status = semantic_status or {}
    semantic_runtime_ready = bool(semantic_status.get("semantic_index_ready"))
    semantic_runtime_payload = {
        "ready": semantic_runtime_ready,
        "embedding_available": bool(semantic_status.get("embedding_available", False)),
        "model": semantic_status.get("semantic_index_model"),
        "dimension": semantic_status.get("semantic_index_dimension"),
        "faiss_dimension": semantic_status.get("faiss_dimension"),
        "chunker_version": semantic_status.get("chunker_version"),
        "workspace_id": semantic_status.get("workspace_id"),
        "vector_count": int(semantic_status.get("vector_count") or 0),
        "stale": bool(semantic_status.get("semantic_index_stale", False)),
        "invalid": bool(semantic_status.get("semantic_index_invalid", False)),
        "stale_reason": semantic_status.get("semantic_index_stale_reason"),
        "metadata": semantic_status.get("metadata") or {},
    }
    current_index_elapsed_ms = None
    if state.current_index_started_at is not None:
        current_index_elapsed_ms = int(
            (time.monotonic() - state.current_index_started_at) * 1000
        )
    return {
        "ok": True,
        "workspace_id": state.workspace_id,
        "accepted_revision": state.accepted_revision,
        "indexed_revision": state.indexed_revision,
        "indexing": state.indexing or state.indexed_revision < state.accepted_revision,
        "exact_indexed_revision": exact_indexed_revision,
        "exact_index_ready": exact_index_ready,
        "exact_pending_revisions": exact_pending_revisions,
        "exact_index": {
            "files": int(exact_status.get("files") or 0),
            "symbols": int(exact_status.get("symbols") or 0),
            "lines": int(exact_status.get("lines") or 0),
            "line_fts_available": bool(exact_status.get("line_fts_available", False)),
            "line_fts_mode": _line_fts_mode(),
            "line_fts_auto_line_limit": _AUTO_FTS_LINE_LIMIT,
            "line_fts_reason": exact_status.get("line_fts_reason"),
            "schema_version": exact_status.get("schema_version"),
            "index_kind": exact_status.get("index_kind"),
        },
        "semantic_index_ready": semantic_index_ready,
        "semantic_index_coverage": semantic_index_coverage,
        "semantic_initial_exact_only": semantic_initial_exact_only,
        # Runtime readiness is intentionally separate from query safety:
        # a FAISS index can be compatible with the configured embedding model
        # while the current workspace coverage is still exact-only/partial.
        "semantic_runtime_ready": semantic_runtime_ready,
        "semantic_runtime": semantic_runtime_payload,
        "semantic_bootstrap_running": semantic_bootstrap_running,
        "semantic_bootstrap_job": semantic_bootstrap_status,
        "semantic_index_stale": semantic_runtime_payload["stale"],
        "semantic_index_invalid": semantic_runtime_payload["invalid"],
        "semantic_index_stale_reason": semantic_runtime_payload["stale_reason"],
        "graph_index_ready": graph_index_ready,
        "graph_indexed_revision": int(
            graph_status.get("graph_indexed_revision") or 0
        ),
        "graph_index": {
            "ready": graph_index_ready,
            "current": bool(graph_status.get("current", False)),
            "indexed_revision": int(
                graph_status.get("graph_indexed_revision") or 0
            ),
            "pending_revisions": int(
                graph_status.get("pending_revisions") or 0
            ),
            "files": int(graph_status.get("files") or 0),
            "supported_files": int(
                graph_status.get("supported_files") or 0
            ),
            "unsupported_files": int(
                graph_status.get("unsupported_files") or 0
            ),
            "parse_error_files": int(
                graph_status.get("parse_error_files") or 0
            ),
            "edges": int(graph_status.get("edges") or 0),
            "definitions": int(graph_status.get("definitions") or 0),
            "callers": int(graph_status.get("callers") or 0),
            "callees": int(graph_status.get("callees") or 0),
            "languages": list(graph_status.get("languages") or []),
            "last_error": graph_status.get("last_error"),
            "needs_rebuild": bool(graph_status.get("needs_rebuild", False)),
            "coverage_complete": bool(
                graph_status.get("coverage_complete", False)
            ),
            "schema_version": graph_status.get("schema_version"),
            "index_kind": graph_status.get("index_kind"),
        },
        "snapshot_ready": readiness_summary["snapshot_ready"],
        "index_worker_busy": index_worker_busy,
        "search_degraded": search_degraded,
        "recommended_query_mode": readiness_summary["recommended_query_mode"],
        "query_mode_reason": readiness_summary["query_mode_reason"],
        "supported_query_modes": readiness_summary["supported_query_modes"],
        "exact_query_safe": readiness_summary["exact_query_safe"],
        "strict_semantic_safe": readiness_summary["strict_semantic_safe"],
        "semantic_query_safe": readiness_summary["semantic_query_safe"],
        "index_readiness_contract": readiness_contract,
        "semantic_pending_revisions": pending_files,
        "pending_files": pending_files,
        "last_index_error": state.last_index_error,
        "last_index_elapsed_ms": state.last_index_elapsed_ms,
        "last_index_revision": state.last_index_revision,
        "index_queue_depth": state.index_queue_depth,
        "index_worker_running": state.index_worker_running,
        "index_jobs_enqueued": state.index_jobs_enqueued,
        "index_jobs_completed": state.index_jobs_completed,
        "last_semantic_files_enqueued": state.last_semantic_files_enqueued,
        "last_semantic_files_skipped": state.last_semantic_files_skipped,
        "last_semantic_skip_reasons": dict(state.last_semantic_skip_reasons),
        "semantic_index_policy": semantic_index_policy_payload(),
        "current_index_revision": state.current_index_revision,
        "current_index_files": state.current_index_files,
        "current_index_bytes": state.current_index_bytes,
        "current_index_deletes": state.current_index_deletes,
        "current_index_job_count": state.current_index_job_count,
        "current_index_elapsed_ms": current_index_elapsed_ms,
        "last_batch_elapsed_ms": state.last_batch_elapsed_ms,
        "last_batch_files": state.last_batch_files,
        "last_batch_deletes": state.last_batch_deletes,
        "last_sync_metadata": dict(state.last_sync_metadata),
        "indexed_files": int(snapshot_status.get("file_count", 0)),
        "indexed_chunks": int(snapshot_status.get("file_count", 0)),
        "snapshot_store": {
            "latest_revision": int(snapshot_status.get("latest_revision", 0)),
            "accepted_revision": int(snapshot_status.get("accepted_revision", 0)),
            "indexed_revision": int(snapshot_status.get("indexed_revision", 0)),
            "files": int(snapshot_status.get("file_count", 0)),
            "deletes": int(snapshot_status.get("delete_count", 0)),
        },
    }


def _index_queue_maxsize() -> int:
    import os

    raw = (os.environ.get("OMNICODE_SYNC_INDEX_QUEUE_MAXSIZE") or "").strip()
    if not raw:
        return 1024
    try:
        value = int(raw)
    except ValueError:
        return 1024
    return max(1, value)


def _index_chunk_max_files() -> int:
    import os

    raw = (os.environ.get("OMNICODE_SYNC_INDEX_CHUNK_FILES") or "").strip()
    if not raw:
        return 25
    try:
        value = int(raw)
    except ValueError:
        return 25
    return max(1, value)


def _index_chunk_max_bytes() -> int:
    import os

    raw = (os.environ.get("OMNICODE_SYNC_INDEX_CHUNK_BYTES") or "").strip()
    if not raw:
        return 250_000
    try:
        value = int(raw)
    except ValueError:
        return 250_000
    return max(1, value)


def _index_content_bytes(content: str) -> int:
    return len(content.encode("utf-8", errors="replace"))


def _is_semantic_index_compat_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "semantic_index_incompatible",
            "semantic_index_not_ready",
            "embedding_dimension_mismatch",
            "metadata_missing",
            "dimension mismatch",
        )
    )


def _index_file_parts(
    item: tuple[str, str] | tuple[str, str, dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    try:
        path, content, metadata = item
    except ValueError:
        path, content = item
        metadata = {}
    return path, content, dict(metadata) if isinstance(metadata, dict) else {}


def _invoke_index_update_blocking(
    workspace_id: str,
    revision: int,
    changed_files: list[tuple[str, str] | tuple[str, str, dict[str, Any]]],
    deleted_paths: list[str],
    semantic_coverage: str,
    graph_changed_files: list[
        tuple[str, str] | tuple[str, str, dict[str, Any]]
    ],
    graph_coverage_complete: bool = False,
) -> int:
    """Preserve the legacy worker-call shape used by tests and integrations."""
    function = _run_index_update_blocking
    signature = inspect.signature(function).parameters
    if "graph_changed_files" in signature:
        kwargs: dict[str, Any] = {"graph_changed_files": graph_changed_files}
        if "graph_coverage_complete" in signature:
            kwargs["graph_coverage_complete"] = graph_coverage_complete
        return function(
            workspace_id,
            revision,
            changed_files,
            deleted_paths,
            semantic_coverage,
            **kwargs,
        )
    return function(
        workspace_id,
        revision,
        changed_files,
        deleted_paths,
        semantic_coverage,
    )


def _ensure_index_worker() -> asyncio.Queue[_IndexJob]:
    """Return the process-local index queue and start one worker per loop."""
    global _INDEX_LOOP, _INDEX_QUEUE, _INDEX_WORKER_TASK

    loop = asyncio.get_running_loop()
    if _INDEX_QUEUE is None or _INDEX_LOOP is not loop:
        _INDEX_LOOP = loop
        _INDEX_QUEUE = asyncio.Queue(maxsize=_index_queue_maxsize())
        _INDEX_WORKER_TASK = None
    if _INDEX_WORKER_TASK is None or _INDEX_WORKER_TASK.done():
        _INDEX_WORKER_TASK = loop.create_task(_index_worker(_INDEX_QUEUE))
    return _INDEX_QUEUE


def _coalesce_index_jobs(jobs: list[_IndexJob]) -> list[_CoalescedIndexJob]:
    max_files = _index_chunk_max_files()
    max_bytes = _index_chunk_max_bytes()
    active_groups: Dict[str, _CoalescedIndexJob] = {}
    completed_groups: list[_CoalescedIndexJob] = []
    for job in jobs:
        graph_items = job.graph_changed_files or job.changed_files
        job_bytes = sum(
            _index_content_bytes(content)
            for _path, content, _metadata in (
                _index_file_parts(item) for item in graph_items
            )
        )
        group = active_groups.get(job.workspace_id)
        if group is None:
            group = _CoalescedIndexJob(workspace_id=job.workspace_id)
            active_groups[job.workspace_id] = group
        if (
            group.job_count > 0
            and (
                len(group.graph_changed_files) + len(graph_items) > max_files
                or group.changed_bytes + job_bytes > max_bytes
            )
        ):
            completed_groups.append(group)
            group = _CoalescedIndexJob(workspace_id=job.workspace_id)
            active_groups[job.workspace_id] = group
        group.revision = max(group.revision, job.revision)
        group.job_count += 1
        group.graph_coverage_complete = (
            group.graph_coverage_complete or job.graph_coverage_complete
        )
        for path in job.deleted_paths:
            group.changed_files.pop(path, None)
            group.graph_changed_files.pop(path, None)
            removed_bytes = group.changed_file_bytes.pop(path, 0)
            group.changed_bytes = max(0, group.changed_bytes - removed_bytes)
            group.deleted_paths.add(path)
        for item in job.changed_files:
            path, content, metadata = _index_file_parts(item)
            group.deleted_paths.discard(path)
            content_bytes = _index_content_bytes(content)
            previous_bytes = group.changed_file_bytes.get(path, 0)
            group.changed_bytes = max(0, group.changed_bytes - previous_bytes)
            group.changed_bytes += content_bytes
            group.changed_file_bytes[path] = content_bytes
            group.changed_files[path] = (path, content, metadata)
        for item in graph_items:
            path, content, metadata = _index_file_parts(item)
            group.deleted_paths.discard(path)
            content_bytes = _index_content_bytes(content)
            previous_bytes = group.changed_file_bytes.get(path, 0)
            group.changed_bytes = max(0, group.changed_bytes - previous_bytes)
            group.changed_bytes += content_bytes
            group.changed_file_bytes[path] = content_bytes
            group.graph_changed_files[path] = (path, content, metadata)
        group.semantic_coverages.add(job.semantic_coverage)
    completed_groups.extend(
        group for group in active_groups.values() if group.job_count > 0
    )
    return completed_groups


async def _enqueue_index_update(
    *,
    workspace_id: str,
    revision: int,
    changed_files: list[tuple[str, str] | tuple[str, str, dict[str, Any]]],
    deleted_paths: list[str],
    graph_changed_files: Optional[
        list[tuple[str, str] | tuple[str, str, dict[str, Any]]]
    ] = None,
    semantic_coverage: str = "unknown",
    graph_coverage_complete: bool = False,
) -> int:
    queue = _ensure_index_worker()
    job = _IndexJob(
        workspace_id=workspace_id,
        revision=revision,
        changed_files=changed_files,
        graph_changed_files=list(graph_changed_files or changed_files),
        deleted_paths=deleted_paths,
        semantic_coverage=semantic_coverage,
        graph_coverage_complete=graph_coverage_complete,
    )
    with _LOCK:
        state = _state_for(workspace_id)
        state.index_queue_depth += 1
        state.index_jobs_enqueued += 1
        state.indexing = True
        depth = state.index_queue_depth
    await queue.put(job)
    return depth


async def _index_worker(queue: asyncio.Queue[_IndexJob]) -> None:
    while True:
        first = await queue.get()
        jobs = [first]
        while True:
            try:
                jobs.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        for group in _coalesce_index_jobs(jobs):
            with _LOCK:
                state = _state_for(group.workspace_id)
                state.index_queue_depth = max(
                    state.index_queue_depth - group.job_count,
                    0,
                )
                state.index_worker_running = True
                state.indexing = True
                state.current_index_revision = group.revision
                state.current_index_files = len(group.graph_changed_files)
                state.current_index_bytes = group.changed_bytes
                state.current_index_deletes = len(group.deleted_paths)
                state.current_index_job_count = group.job_count
                state.current_index_started_at = time.monotonic()

            started = time.monotonic()
            try:
                indexed_revision = await asyncio.to_thread(
                    _invoke_index_update_blocking,
                    group.workspace_id,
                    group.revision,
                    list(group.changed_files.values()),
                    sorted(group.deleted_paths),
                    merge_semantic_coverages(group.semantic_coverages),
                    list(group.graph_changed_files.values()),
                    graph_coverage_complete=group.graph_coverage_complete,
                )
                merged_semantic_coverage = merge_semantic_coverages(
                    group.semantic_coverages
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                with _LOCK:
                    state = _state_for(group.workspace_id)
                    state.indexed_revision = indexed_revision
                    if merged_semantic_coverage not in {
                        "",
                        "unknown",
                        "unchanged",
                    }:
                        state.semantic_index_coverage = merged_semantic_coverage
                    if merged_semantic_coverage == "exact_only_initial_sync":
                        state.semantic_initial_exact_only = True
                    elif merged_semantic_coverage in {
                        "semantic_full",
                        "selected_files",
                        "filtered",
                    }:
                        state.semantic_initial_exact_only = False
                    state.index_worker_running = False
                    state.indexing = (
                        state.index_queue_depth > 0
                        or state.indexed_revision < state.accepted_revision
                    )
                    state.last_index_error = None
                    state.last_index_elapsed_ms = elapsed_ms
                    state.last_index_revision = indexed_revision
                    state.index_jobs_completed += group.job_count
                    state.current_index_revision = 0
                    state.current_index_files = 0
                    state.current_index_bytes = 0
                    state.current_index_deletes = 0
                    state.current_index_job_count = 0
                    state.current_index_started_at = None
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                with _LOCK:
                    state = _state_for(group.workspace_id)
                    state.index_worker_running = False
                    state.indexing = state.index_queue_depth > 0
                    state.last_index_error = str(exc)
                    state.last_index_elapsed_ms = elapsed_ms
                    state.current_index_revision = 0
                    state.current_index_files = 0
                    state.current_index_bytes = 0
                    state.current_index_deletes = 0
                    state.current_index_job_count = 0
                    state.current_index_started_at = None

        for _job in jobs:
            queue.task_done()


def _run_index_update_blocking(
    workspace_id: str,
    revision: int,
    changed_files: list[tuple[str, str] | tuple[str, str, dict[str, Any]]],
    deleted_paths: list[str],
    semantic_coverage: str = "unknown",
    graph_changed_files: Optional[
        list[tuple[str, str] | tuple[str, str, dict[str, Any]]]
    ] = None,
    graph_coverage_complete: bool = False,
) -> int:
    graph_index = _graph_index()
    try:
        graph_items = graph_changed_files
        if graph_items is None:
            graph_items = changed_files
        graph_status = graph_index.status(
            workspace_id=workspace_id,
            accepted_revision=revision,
        )
        if _should_sync_rebuild_graph(
            workspace_id=workspace_id,
            graph_status=graph_status,
        ):
            graph_index.index_snapshot_store(
                workspace_id=workspace_id,
                revision=revision,
                force=True,
            )
        graph_index.update_batch(
            workspace_id=workspace_id,
            changed_files=[
                {
                    "path": path,
                    "content": content,
                    "hash": metadata.get("content_hash")
                    or metadata.get("snapshot_hash")
                    or "",
                }
                for path, content, metadata in (
                    _index_file_parts(item) for item in graph_items
                )
            ],
            deleted_paths=deleted_paths,
            revision=revision,
            coverage_complete=True if graph_coverage_complete else None,
        )
    except Exception as exc:
        try:
            graph_index.record_error(
                workspace_id=workspace_id,
                error=f"{exc.__class__.__name__}: {exc}",
            )
        except Exception:
            pass

    async def _update_index() -> None:
        nonlocal semantic_coverage
        if changed_files or deleted_paths:
            engine = get_search_engine()
            if engine is None:
                return
            semantic_available = getattr(engine, "semantic_available", None)
            if callable(semantic_available) and not bool(semantic_available()):
                semantic_coverage = "exact_only_initial_sync"
                refresh_stats = getattr(engine, "refresh_stats", None)
                if callable(refresh_stats):
                    maybe_awaitable = refresh_stats()
                    if asyncio.iscoroutine(maybe_awaitable):
                        await maybe_awaitable
                return
            try:
                if changed_files:
                    upsert_many = getattr(engine, "upsert_contents", None)
                    if callable(upsert_many):
                        try:
                            await upsert_many(changed_files, refresh=False)
                        except TypeError:
                            await upsert_many(changed_files)
                    else:
                        for item in changed_files:
                            try:
                                path, content, metadata = item
                            except ValueError:
                                path, content = item
                                metadata = {}
                            try:
                                await engine.upsert_content(
                                    path,
                                    content,
                                    refresh=False,
                                    content_hash=metadata.get("content_hash"),
                                    revision=metadata.get("snapshot_revision"),
                                    workspace_id=metadata.get("workspace_id"),
                                )
                            except TypeError:
                                try:
                                    await engine.upsert_content(
                                        path,
                                        content,
                                        refresh=False,
                                    )
                                except TypeError:
                                    await engine.upsert_content(path, content)
                for path in deleted_paths:
                    try:
                        await engine.delete_file_index(path, refresh=False)
                    except TypeError:
                        await engine.delete_file_index(path)
                if changed_files or deleted_paths:
                    refresh_stats = getattr(engine, "refresh_stats", None)
                    if callable(refresh_stats):
                        maybe_awaitable = refresh_stats()
                        if asyncio.iscoroutine(maybe_awaitable):
                            await maybe_awaitable
                    else:
                        initialize = getattr(engine, "initialize", None)
                        if callable(initialize):
                            await initialize()
            except Exception as exc:
                if not _is_semantic_index_compat_error(exc):
                    raise
                semantic_coverage = "exact_only_semantic_incompatible"
                refresh_stats = getattr(engine, "refresh_stats", None)
                if callable(refresh_stats):
                    maybe_awaitable = refresh_stats()
                    if asyncio.iscoroutine(maybe_awaitable):
                        await maybe_awaitable
                return

    asyncio.run(_update_index())
    return int(
        _SNAPSHOT_STORE.mark_indexed(
            workspace_id=workspace_id,
            revision=revision,
            semantic_coverage=semantic_coverage,
        )
    )


@router.post("/batch")
async def push_sync_batch(
    body: SyncBatchIn,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    started = time.monotonic()
    workspace = _resolve_workspace(x_omnicode_workspace)
    if not body.files and not body.deletes:
        return _error("sync batch is empty", workspace_id=workspace.workspace_id)

    normalized_files: list[tuple[str, SyncFileIn]] = []
    normalized_deletes: list[str] = []
    try:
        for item in body.files:
            path = workspace.normalize_rel(item.path)
            _validate_file_payload(item)
            normalized_files.append((path, item))
        for delete_item in body.deletes:
            normalized_deletes.append(workspace.normalize_rel(delete_item.path))
    except WorkspacePathError as exc:
        return _error(
            f"Invalid sync path: {exc}",
            workspace_id=workspace.workspace_id,
            accepted_revision=_state_for(workspace.workspace_id).accepted_revision,
        )
    except ValueError as exc:
        return _error(
            str(exc),
            workspace_id=workspace.workspace_id,
            accepted_revision=_state_for(workspace.workspace_id).accepted_revision,
        )

    state = _state_for(workspace.workspace_id)
    await _ensure_state_loaded(state)
    existing_hashes = await asyncio.to_thread(
        _SNAPSHOT_STORE.file_hashes,
        workspace.workspace_id,
    )
    changed_files: list[tuple[str, SyncFileIn]] = []
    skipped_unchanged: list[dict[str, str]] = []
    for path, item in normalized_files:
        if existing_hashes.get(path) == item.hash:
            skipped_unchanged.append({"path": path, "hash": item.hash})
        else:
            changed_files.append((path, item))

    with _LOCK:
        state = _state_for(workspace.workspace_id)
        has_effective_changes = bool(changed_files or normalized_deletes)
        accepted = (
            max(state.accepted_revision + 1, body.client_revision)
            if has_effective_changes
            else state.accepted_revision
        )

    batch_result = None
    exact_indexed_revision = 0
    if has_effective_changes:
        store_files = [
            {
                "path": path,
                "hash": item.hash,
                "size": item.size,
                "mtime_ms": item.mtime_ms,
                "encoding": item.encoding,
                "content": item.content,
            }
            for path, item in changed_files
        ]
        try:
            batch_result = await asyncio.to_thread(
                _SNAPSHOT_STORE.apply_batch,
                workspace_id=workspace.workspace_id,
                files=store_files,
                deletes=normalized_deletes,
                revision=accepted,
            )
        except SnapshotStoreError as exc:
            return _error(
                str(exc),
                workspace_id=workspace.workspace_id,
                accepted_revision=_state_for(workspace.workspace_id).accepted_revision,
            )
        try:
            populate_fts, fts_disabled_reason = _sync_exact_fts_decision(
                workspace_id=workspace.workspace_id,
                files=store_files,
            )
            exact_indexed_revision = await asyncio.to_thread(
                _exact_index().update_batch,
                workspace_id=workspace.workspace_id,
                changed_files=store_files,
                deleted_paths=normalized_deletes,
                revision=accepted,
                populate_fts=populate_fts,
                fts_disabled_reason=fts_disabled_reason,
            )
        except Exception as exc:
            return _error(
                f"exact index update failed: {exc}",
                workspace_id=workspace.workspace_id,
                accepted_revision=_state_for(workspace.workspace_id).accepted_revision,
            )

    with _LOCK:
        state = _state_for(workspace.workspace_id)
        _status_cache_invalidate(workspace.workspace_id)
        for path, item in changed_files:
            state.files[path] = _SyncedFile(
                path=path,
                hash=item.hash,
                size=item.size,
                mtime_ms=item.mtime_ms,
                encoding=item.encoding,
                content=item.content,
                accepted_revision=accepted,
            )
            state.deleted_paths.discard(path)
        for path in normalized_deletes:
            state.files.pop(path, None)
            state.deleted_paths.add(path)
        state.accepted_revision = accepted
        if batch_result is not None:
            state.indexed_revision = max(
                state.indexed_revision,
                batch_result.indexed_revision,
            )
            state.file_count = batch_result.file_count
            state.delete_count = batch_result.delete_count
            state.state_loaded = True
        if has_effective_changes:
            state.indexing = True
            state.last_index_error = None
        state.last_batch_elapsed_ms = int((time.monotonic() - started) * 1000)
        state.last_batch_files = len(changed_files)
        state.last_batch_deletes = len(normalized_deletes)
        state.last_sync_metadata = dict(body.metadata or {})
        indexed_revision = state.indexed_revision

    if not has_effective_changes:
        try:
            exact_status = await asyncio.to_thread(
                _exact_index().status,
                workspace_id=workspace.workspace_id,
            )
            exact_indexed_revision = int(
                exact_status.get("exact_indexed_revision") or 0
            )
        except Exception:
            exact_indexed_revision = 0

    semantic_files_enqueued = 0
    semantic_files_skipped = 0
    semantic_skip_reasons: dict[str, int] = {}
    queued_depth = 0
    semantic_coverage = "unchanged"
    if has_effective_changes:
        index_files = []
        graph_files = []
        for path, item in changed_files:
            graph_files.append(
                (
                    path,
                    item.content,
                    {
                        "content_hash": item.hash,
                        "snapshot_hash": item.hash,
                        "snapshot_revision": accepted,
                        "workspace_id": workspace.workspace_id,
                    },
                )
            )
            include_semantic, reason = semantic_index_decision(
                path,
                item.content,
                body.metadata or {},
            )
            if not include_semantic:
                semantic_files_skipped += 1
                semantic_skip_reasons[reason] = semantic_skip_reasons.get(reason, 0) + 1
                continue
            semantic_files_enqueued += 1
            index_files.append(
                (
                    path,
                    item.content,
                    semantic_index_metadata(
                        path,
                        item.content,
                        {
                            "content_hash": item.hash,
                            "snapshot_hash": item.hash,
                            "snapshot_revision": accepted,
                            "workspace_id": workspace.workspace_id,
                        },
                    ),
                )
            )
        semantic_coverage = semantic_coverage_for_batch(
            files_enqueued=semantic_files_enqueued,
            files_skipped=semantic_files_skipped,
            skip_reasons=semantic_skip_reasons,
            deletes=len(normalized_deletes),
        )
        queued_depth = await _enqueue_index_update(
            workspace_id=workspace.workspace_id,
            revision=accepted,
            changed_files=index_files,
            deleted_paths=normalized_deletes,
            graph_changed_files=graph_files,
            semantic_coverage=semantic_coverage,
            graph_coverage_complete=_initial_sync_graph_coverage_complete(
                body.metadata or {},
                snapshot_file_count=(
                    batch_result.file_count
                    if batch_result is not None
                    else _state_for(workspace.workspace_id).file_count
                ),
            ),
        )

    with _LOCK:
        state = _state_for(workspace.workspace_id)
        state.last_semantic_files_enqueued = semantic_files_enqueued
        state.last_semantic_files_skipped = semantic_files_skipped
        state.last_semantic_skip_reasons = dict(semantic_skip_reasons)
        return {
            "ok": True,
            "workspace_id": workspace.workspace_id,
            "accepted_revision": state.accepted_revision,
            "indexed_revision": indexed_revision,
            "exact_indexed_revision": exact_indexed_revision,
            "indexing": state.indexing,
            "index_queue_depth": state.index_queue_depth,
            "index_worker_running": state.index_worker_running,
            "queued_index_jobs": queued_depth,
            "last_batch_elapsed_ms": state.last_batch_elapsed_ms,
            "last_sync_metadata": dict(state.last_sync_metadata),
            "files_accepted": len(changed_files),
            "deletes_accepted": len(normalized_deletes),
            "skipped_unchanged": len(skipped_unchanged),
            "semantic_files_enqueued": state.last_semantic_files_enqueued,
            "semantic_files_skipped": state.last_semantic_files_skipped,
            "semantic_skip_reasons": dict(state.last_semantic_skip_reasons),
            "accepted_files": [
                {"path": path, "hash": item.hash, "revision": accepted}
                for path, item in changed_files
            ],
            "skipped_paths": skipped_unchanged,
        }


@router.get("/status")
async def sync_status(
    x_omnicode_workspace: Optional[str] = Header(default=None),
    workspace_id: Optional[str] = None,
):
    workspace = _resolve_workspace(x_omnicode_workspace or workspace_id)
    state = _state_for(workspace.workspace_id)
    await _ensure_state_loaded(state)
    await _recover_stalled_index_update(state)
    with _LOCK:
        state = _state_for(workspace.workspace_id)
        cache_key = _status_cache_key(state)
    cached = _status_cache_get(workspace.workspace_id, cache_key)
    if cached is not None:
        return cached

    async with _status_cache_lock(workspace.workspace_id):
        with _LOCK:
            state = _state_for(workspace.workspace_id)
            cache_key = _status_cache_key(state)
        cached = _status_cache_get(workspace.workspace_id, cache_key)
        if cached is not None:
            return cached
        with _LOCK:
            accepted_revision_hint = int(
                _state_for(workspace.workspace_id).accepted_revision
            )

        def _semantic_status_probe() -> dict[str, Any]:
            search_engine = get_search_engine()
            semantic_index_status = getattr(
                search_engine,
                "semantic_index_status",
                None,
            )
            if callable(semantic_index_status):
                return dict(semantic_index_status())
            return {}

        async def _bounded_probe(fn, /, *args, **kwargs):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(fn, *args, **kwargs),
                    timeout=max(_STATUS_PROBE_TIMEOUT_SECONDS, 0.01),
                )
            except asyncio.TimeoutError:
                return TimeoutError(
                    f"status probe exceeded "
                    f"{_STATUS_PROBE_TIMEOUT_SECONDS:.3f}s"
                )
            except Exception as exc:  # noqa: BLE001
                return exc

        exact_index = _exact_index()
        exact_status_probe = getattr(exact_index, "try_status", None)
        if callable(exact_status_probe):
            exact_probe = _bounded_probe(
                exact_status_probe,
                workspace_id=workspace.workspace_id,
                lock_timeout_ms=75,
            )
        else:
            exact_probe = _bounded_probe(
                exact_index.status,
                workspace_id=workspace.workspace_id,
            )

        probes = await asyncio.gather(
            exact_probe,
            _bounded_probe(
                _SNAPSHOT_STORE.status,
                workspace.workspace_id,
            ),
            _bounded_probe(_semantic_status_probe),
            _bounded_probe(
                _graph_index().status,
                workspace_id=workspace.workspace_id,
                accepted_revision=accepted_revision_hint,
            ),
            return_exceptions=True,
        )
        exact_result, snapshot_result, semantic_result, graph_result = probes
        probe_warnings: list[str] = []
        for name, result in (
            ("exact", exact_result),
            ("snapshot", snapshot_result),
            ("semantic", semantic_result),
            ("graph", graph_result),
        ):
            if isinstance(result, BaseException):
                probe_warnings.append(
                    f"{name}_status_probe_degraded:"
                    f"{result.__class__.__name__}"
                )
        exact_status = (
            exact_result if isinstance(exact_result, dict) else {}
        )
        if isinstance(snapshot_result, dict):
            snapshot_status = snapshot_result
        else:
            with _LOCK:
                snapshot_status = _snapshot_status_from_state(
                    _state_for(workspace.workspace_id)
                )
        if isinstance(semantic_result, dict):
            semantic_status = semantic_result
        else:
            semantic_status = {
                "semantic_index_ready": False,
                "semantic_index_stale": False,
                "semantic_index_invalid": True,
                "semantic_index_stale_reason": str(semantic_result),
                "embedding_available": False,
            }
        if isinstance(graph_result, dict):
            graph_status = graph_result
        else:
            graph_status = {
                "ready": False,
                "current": False,
                "last_error": str(graph_result),
            }
        with _LOCK:
            state = _state_for(workspace.workspace_id)
            _apply_snapshot_status(state, snapshot_status)
            snapshot_status = _merge_snapshot_status_with_state(
                state,
                snapshot_status,
            )
            cache_key = _status_cache_key(state)
            payload = _status_payload(
                state,
                snapshot_status,
                exact_status,
                semantic_status,
                graph_status,
            )
            payload["status_probe_degraded"] = bool(probe_warnings)
            payload["status_probe_warnings"] = probe_warnings
            payload["status_probe_timeout_seconds"] = (
                _STATUS_PROBE_TIMEOUT_SECONDS
            )
        return _status_cache_put(workspace.workspace_id, cache_key, payload)


@router.post("/barrier")
async def sync_barrier(
    body: SyncBarrierIn,
    x_omnicode_workspace: Optional[str] = Header(default=None),
):
    workspace = _resolve_workspace(x_omnicode_workspace)
    try:
        paths = [workspace.normalize_rel(p) for p in body.paths]
    except WorkspacePathError as exc:
        return _error(
            f"Invalid barrier path: {exc}",
            workspace_id=workspace.workspace_id,
            next_actions=["Run omni_status() to inspect sync state."],
        )

    state = _state_for(workspace.workspace_id)
    await _ensure_state_loaded(state)
    with _LOCK:
        state = _state_for(workspace.workspace_id)
        ready = state.indexed_revision >= body.min_revision
        if ready:
            return {
                "ok": True,
                "ready": True,
                "stale": False,
                "workspace_id": workspace.workspace_id,
                "accepted_revision": state.accepted_revision,
                "indexed_revision": state.indexed_revision,
                "paths": paths,
            }
        return {
            "ok": False,
            "error": f"Cloud index is stale for workspace {workspace.workspace_id}",
            "ready": False,
            "stale": True,
            "workspace_id": workspace.workspace_id,
            "local_revision": body.min_revision,
            "accepted_revision": state.accepted_revision,
            "indexed_revision": state.indexed_revision,
            "paths": paths,
            "next_actions": [
                "Wait for indexing to finish or retry the same tool call.",
                "Run omni_status() to inspect sync state.",
            ],
        }


__all__ = ["router"]
