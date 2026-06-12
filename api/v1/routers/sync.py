"""Hybrid workspace sync endpoints.

This is the cloud-side protocol surface used by local MCP clients before
cloud-backed search/context/impact routing is enabled. Step 6 keeps accepted
state in memory; the snapshot store is introduced in the next architecture
step.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from omnicode_core.workspace.local import LocalWorkspace, WorkspacePathError
from omnicode_core.workspace.registry import get_workspace_registry
from omnicode_core.workspace.request import (
    WorkspaceResolutionError,
    resolve_workspace_request,
)
from omnicode_core.workspace.readiness import (
    build_index_readiness_contract,
    contract_summary,
)
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.semantic_index_policy import (
    merge_semantic_coverages,
    semantic_coverage_for_batch,
    semantic_index_decision,
    semantic_index_policy_payload,
)
from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    SnapshotStoreError,
)

router = APIRouter(prefix="/sync", tags=["sync"])


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
    semantic_coverage: str = "unknown"


@dataclass
class _CoalescedIndexJob:
    workspace_id: str
    revision: int = 0
    changed_files: Dict[
        str,
        tuple[str, str, dict[str, Any]],
    ] = field(default_factory=dict)
    changed_file_bytes: Dict[str, int] = field(default_factory=dict)
    changed_bytes: int = 0
    deleted_paths: set[str] = field(default_factory=set)
    semantic_coverages: set[str] = field(default_factory=set)
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
    last_sync_metadata: Dict[str, Any] = field(default_factory=dict)
    state_loaded: bool = False
    file_count: int = 0
    delete_count: int = 0


_LOCK = threading.RLock()
_SYNC_STATES: Dict[str, _SyncWorkspaceState] = {}
_SNAPSHOT_STORE = CloudSnapshotStore()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INDEX_QUEUE: Optional[asyncio.Queue[_IndexJob]] = None
_INDEX_WORKER_TASK: Optional[asyncio.Task[None]] = None
_INDEX_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _exact_index() -> SnapshotExactIndex:
    return SnapshotExactIndex(store=_SNAPSHOT_STORE)


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


def _status_payload(
    state: _SyncWorkspaceState,
    snapshot_status: dict,
    exact_status: Optional[dict[str, Any]] = None,
) -> dict:
    pending_files = max(state.accepted_revision - state.indexed_revision, 0)
    exact_status = exact_status or {}
    snapshot_file_count = int(snapshot_status.get("file_count", state.file_count) or 0)
    snapshot_delete_count = int(
        snapshot_status.get("delete_count", state.delete_count) or 0
    )
    exact_indexed_revision = int(exact_status.get("exact_indexed_revision") or 0)
    semantic_initial_exact_only = bool(
        snapshot_status.get("semantic_initial_exact_only", False)
    )
    semantic_index_coverage = str(
        snapshot_status.get("semantic_index_coverage") or "unknown"
    )
    index_worker_busy = bool(
        state.index_worker_running
        or state.index_queue_depth > 0
        or state.indexing
        or pending_files > 0
    )
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
        graph_index_ready=False,
    )
    readiness_summary = contract_summary(readiness_contract)
    exact_index_ready = readiness_summary["exact_index_ready"]
    exact_pending_revisions = readiness_summary["exact_pending_revisions"]
    semantic_index_ready = readiness_summary["semantic_index_ready"]
    search_degraded = readiness_summary["search_degraded"]
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
        },
        "semantic_index_ready": semantic_index_ready,
        "semantic_index_coverage": semantic_index_coverage,
        "semantic_initial_exact_only": semantic_initial_exact_only,
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


def _index_file_parts(
    item: tuple[str, str] | tuple[str, str, dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    try:
        path, content, metadata = item
    except ValueError:
        path, content = item
        metadata = {}
    return path, content, dict(metadata) if isinstance(metadata, dict) else {}


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
        job_bytes = sum(
            _index_content_bytes(content)
            for _path, content, _metadata in (
                _index_file_parts(item) for item in job.changed_files
            )
        )
        group = active_groups.get(job.workspace_id)
        if group is None:
            group = _CoalescedIndexJob(workspace_id=job.workspace_id)
            active_groups[job.workspace_id] = group
        if (
            group.job_count > 0
            and (
                len(group.changed_files) + len(job.changed_files) > max_files
                or group.changed_bytes + job_bytes > max_bytes
            )
        ):
            completed_groups.append(group)
            group = _CoalescedIndexJob(workspace_id=job.workspace_id)
            active_groups[job.workspace_id] = group
        group.revision = max(group.revision, job.revision)
        group.job_count += 1
        for path in job.deleted_paths:
            group.changed_files.pop(path, None)
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
    semantic_coverage: str = "unknown",
) -> int:
    queue = _ensure_index_worker()
    job = _IndexJob(
        workspace_id=workspace_id,
        revision=revision,
        changed_files=changed_files,
        deleted_paths=deleted_paths,
        semantic_coverage=semantic_coverage,
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
                state.current_index_files = len(group.changed_files)
                state.current_index_bytes = group.changed_bytes
                state.current_index_deletes = len(group.deleted_paths)
                state.current_index_job_count = group.job_count
                state.current_index_started_at = time.monotonic()

            started = time.monotonic()
            try:
                indexed_revision = await asyncio.to_thread(
                    _run_index_update_blocking,
                    group.workspace_id,
                    group.revision,
                    list(group.changed_files.values()),
                    sorted(group.deleted_paths),
                    merge_semantic_coverages(group.semantic_coverages),
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                with _LOCK:
                    state = _state_for(group.workspace_id)
                    state.indexed_revision = indexed_revision
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
) -> int:
    async def _update_index() -> None:
        if changed_files or deleted_paths:
            engine = get_search_engine()
            if engine is None:
                return
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
            exact_indexed_revision = await asyncio.to_thread(
                _exact_index().update_batch,
                workspace_id=workspace.workspace_id,
                changed_files=store_files,
                deleted_paths=normalized_deletes,
                revision=accepted,
            )
        except Exception as exc:
            return _error(
                f"exact index update failed: {exc}",
                workspace_id=workspace.workspace_id,
                accepted_revision=_state_for(workspace.workspace_id).accepted_revision,
            )

    with _LOCK:
        state = _state_for(workspace.workspace_id)
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
        for path, item in changed_files:
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
                    {
                        "content_hash": item.hash,
                        "snapshot_hash": item.hash,
                        "snapshot_revision": accepted,
                        "workspace_id": workspace.workspace_id,
                    },
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
            semantic_coverage=semantic_coverage,
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
    with _LOCK:
        state = _state_for(workspace.workspace_id)
    try:
        exact_status = await asyncio.to_thread(
            _exact_index().status,
            workspace_id=workspace.workspace_id,
        )
    except Exception:
        exact_status = {}
    try:
        snapshot_status = await asyncio.to_thread(
            _SNAPSHOT_STORE.status,
            workspace.workspace_id,
        )
    except Exception:
        with _LOCK:
            snapshot_status = _snapshot_status_from_state(_state_for(workspace.workspace_id))
    with _LOCK:
        state = _state_for(workspace.workspace_id)
        return _status_payload(state, snapshot_status, exact_status)


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
