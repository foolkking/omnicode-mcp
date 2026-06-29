"""Stateless HTTP client for the OmniCode local-agent (Wave 2, W2-2).

Pure synchronous code on top of ``httpx`` so the watcher loop can call
into it from a thread executor without juggling event loops, AND the
unit tests can drive it against a fake transport.

Responsibilities:

* Read files off the local disk and POST them as JSON to ``/sync/batch``.
* Send deletes through ``/sync/batch`` as delete entries.
* Carry the bearer token (or X-API-Key) on every request.
* Retry transient failures with exponential backoff.

What it deliberately does NOT do:
* Apply patches on the local side. That stays in PatchManager — agent
  is sync-only for the index payload, not a patch loop.
* Long-poll the server for inbound changes. Pull mode is parked in
  Wave 2 W2-? for after we figure out conflict semantics.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

import httpx

from omnicode_core.workspace.local import LocalWorkspace
from omnicode_core.workspace.manifest import LocalManifest
from omnicode_core.workspace.sync_queue import SyncQueue

logger = logging.getLogger(__name__)

# Files we never push — common build/junk dirs and binary blobs we
# can't usefully chunk anyway. The watcher applies its own filter on
# top of this so it doesn't burn syscalls in the first place.
_DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".data/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "dist/",
    "build/",
)

_BINARY_EXTS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z",
        ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
        ".faiss", ".db", ".sqlite", ".sqlite3",
    }
)


@dataclass
class AgentResult:
    """Aggregate outcome of an agent push cycle."""

    pushed: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed_ms: int = 0
    accepted_revision: Optional[int] = None
    indexed_revision: Optional[int] = None
    sync_protocol: Optional[str] = None
    files_seen: Optional[int] = None
    initial_sync_truncated: Optional[bool] = None
    initial_sync_cap: Optional[int] = None

    def merge(self, other: "AgentResult") -> "AgentResult":
        self.pushed += other.pushed
        self.deleted += other.deleted
        self.skipped += other.skipped
        self.errors.extend(other.errors)
        self.elapsed_ms += other.elapsed_ms
        if other.accepted_revision is not None:
            self.accepted_revision = other.accepted_revision
        if other.indexed_revision is not None:
            self.indexed_revision = other.indexed_revision
        if other.sync_protocol is not None:
            self.sync_protocol = other.sync_protocol
        if other.files_seen is not None:
            self.files_seen = other.files_seen
        if other.initial_sync_truncated is not None:
            self.initial_sync_truncated = other.initial_sync_truncated
        if other.initial_sync_cap is not None:
            self.initial_sync_cap = other.initial_sync_cap
        return self

    def to_dict(self) -> dict:
        payload = {
            "pushed": self.pushed,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "elapsed_ms": self.elapsed_ms,
        }
        if self.accepted_revision is not None:
            payload["accepted_revision"] = self.accepted_revision
        if self.indexed_revision is not None:
            payload["indexed_revision"] = self.indexed_revision
        if self.sync_protocol is not None:
            payload["sync_protocol"] = self.sync_protocol
        if self.files_seen is not None:
            payload["files_seen"] = self.files_seen
        if self.initial_sync_truncated is not None:
            payload["initial_sync_truncated"] = self.initial_sync_truncated
        if self.initial_sync_cap is not None:
            payload["initial_sync_cap"] = self.initial_sync_cap
        return payload


def _is_binary_path(rel: str) -> bool:
    return Path(rel).suffix.lower() in _BINARY_EXTS


def _is_excluded(rel: str, extra: Sequence[str] = ()) -> bool:
    rel_norm = rel.replace("\\", "/")
    candidates = list(_DEFAULT_EXCLUDES) + list(extra)
    return any(
        rel_norm == pat.rstrip("/")
        or rel_norm.startswith(pat)
        for pat in candidates
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


class AgentClient:
    """HTTP client that talks to the remote ``/sync/...`` endpoints."""

    def __init__(
        self,
        remote: str,
        token: Optional[str] = None,
        workspace: Optional[Path] = None,
        workspace_id: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_file_bytes: int = 1_000_000,
        batch_max_files: int = 500,
        batch_max_bytes: int = 8_000_000,
        excludes: Sequence[str] = (),
        manifest_path: Optional[Path] = None,
        record_manifest: bool = True,
    ) -> None:
        if not remote:
            raise ValueError("remote is required")
        self._remote = remote.rstrip("/")
        self._token = token or ""
        self._workspace = Path(workspace).resolve() if workspace else Path.cwd()
        self._workspace_id = (workspace_id or "").strip()
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_file_bytes = max_file_bytes
        self._batch_max_files = max(1, int(batch_max_files or 1))
        self._batch_max_bytes = max(1, int(batch_max_bytes or 1))
        self._excludes = tuple(excludes)
        self._manifest_path = Path(manifest_path) if manifest_path else None
        self._record_manifest = bool(record_manifest)
        self._client = client or httpx.Client(
            base_url=self._remote, timeout=timeout
        )
        self._sync_revision: Optional[int] = None

    # ------------------------------------------------------------ helpers
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token:
            headers["X-API-Key"] = self._token
        if self._workspace_id:
            headers["X-Omnicode-Workspace"] = self._workspace_id
        return headers

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._workspace)).replace(
                "\\", "/"
            )
        except ValueError:
            # Path was supplied relative to cwd already — best effort.
            return str(path).replace("\\", "/")

    def _read_text(self, rel: str) -> Optional[str]:
        full = self._workspace / rel
        if not full.is_file():
            return None
        try:
            if full.stat().st_size > self._max_file_bytes:
                logger.debug("agent: skipping oversized %s", rel)
                return None
        except OSError:
            return None
        try:
            return full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("agent: failed to read %s — %s", rel, exc)
            return None

    def _post(self, path: str, json: dict) -> httpx.Response:
        last_exc: Optional[Exception] = None
        backoff = 0.5
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._client.post(path, json=json, headers=self._headers())
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    logger.info(
                        "agent: %s %s failed (%s); retry %d/%d in %.1fs",
                        "POST",
                        path,
                        exc,
                        attempt,
                        self._max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
        # All attempts failed
        raise last_exc  # type: ignore[misc]

    def _sync_status_revision(self) -> int:
        if self._sync_revision is not None:
            return self._sync_revision
        try:
            r = self._client.get("/sync/status", headers=self._headers())
            if r.status_code == 200:
                payload = r.json()
                body = payload.get("result", payload)
                if isinstance(body, dict):
                    self._sync_revision = int(body.get("accepted_revision") or 0)
                    return self._sync_revision
        except Exception:
            pass
        self._sync_revision = 0
        return self._sync_revision

    def _sync_file_payload(self, rel: str, text: str) -> dict[str, Any]:
        full = self._workspace / rel
        mtime_ms = 0
        try:
            mtime_ms = int(full.stat().st_mtime * 1000)
        except OSError:
            pass
        raw = text.encode("utf-8", errors="replace")
        return {
            "path": rel,
            "hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "mtime_ms": mtime_ms,
            "encoding": "utf-8",
            "content": text,
        }

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load_manifest(self) -> Optional[LocalManifest]:
        if not self._record_manifest or not self._workspace_id:
            return None
        try:
            workspace = LocalWorkspace(
                root=self._workspace,
                workspace_id=self._workspace_id,
            )
            return LocalManifest.load(
                workspace=workspace,
                path=self._manifest_path,
                max_file_bytes=self._max_file_bytes,
                ignore_paths=tuple(_DEFAULT_EXCLUDES) + self._excludes,
            )
        except Exception as exc:
            logger.warning("agent: local manifest unavailable: %s", exc)
            return None

    def _record_manifest_ack(
        self,
        *,
        files: list[dict[str, Any]],
        deletes: list[str],
        result: AgentResult,
    ) -> None:
        if result.errors or result.accepted_revision is None:
            return
        manifest = self._load_manifest()
        if manifest is None:
            return
        accepted = int(result.accepted_revision)
        indexed = int(result.indexed_revision or accepted)
        now = self._utc_now()
        changed_paths: set[str] = set()

        for payload in files:
            try:
                rel = manifest.workspace.normalize_rel(str(payload.get("path") or ""))
            except Exception:
                continue
            changed_paths.add(rel)
            manifest.files[rel] = {
                "hash": payload.get("hash"),
                "size": int(payload.get("size") or 0),
                "mtime_ms": int(payload.get("mtime_ms") or 0),
                "last_uploaded_revision": accepted,
                "last_seen_at": now,
            }

        for raw in deletes:
            try:
                rel = manifest.workspace.normalize_rel(str(raw))
            except Exception:
                continue
            changed_paths.add(rel)
            manifest.files.pop(rel, None)

        manifest.data["local_revision"] = max(manifest.local_revision, accepted)
        manifest.data["last_accepted_revision"] = accepted
        manifest.data["last_indexed_revision"] = indexed
        if changed_paths:
            manifest.data["pending"] = [
                item for item in manifest.pending if item.get("path") not in changed_paths
            ]
        manifest.save()

    def _record_manifest_failure(
        self,
        *,
        files: list[dict[str, Any]],
        deletes: list[str],
    ) -> None:
        """Persist failed sync operations so a later push can drain them."""
        manifest = self._load_manifest()
        if manifest is None:
            return

        changed = False
        for payload in files:
            try:
                rel = manifest.workspace.normalize_rel(str(payload.get("path") or ""))
                changed = manifest.mark_changed(rel) is not None or changed
            except Exception:
                continue
        for raw in deletes:
            try:
                rel = manifest.workspace.normalize_rel(str(raw))
                changed = manifest.mark_changed(rel) is not None or changed
            except Exception:
                continue

        if changed:
            manifest.save()

    def _push_sync_batch(
        self,
        *,
        files: list[dict[str, Any]],
        deletes: list[str],
        started: float,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AgentResult:
        result = AgentResult(sync_protocol="/sync/batch")
        if not self._workspace_id:
            result.errors.append("workspace_id is required for /sync")
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result
        base_revision = self._sync_status_revision()
        client_revision = base_revision + 1
        body = {
            "client_id": "omnicode-agent",
            "base_revision": base_revision,
            "client_revision": client_revision,
            "files": files,
            "deletes": [{"path": p} for p in deletes],
        }
        if metadata:
            body["metadata"] = metadata
        try:
            r = self._post("/sync/batch", body)
        except Exception as exc:
            result.errors.append(f"sync batch: {exc}")
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            self._record_manifest_failure(files=files, deletes=deletes)
            return result
        if r.status_code != 200:
            result.errors.append(
                f"sync batch: HTTP {r.status_code}: {r.text[:200]}"
            )
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            self._record_manifest_failure(files=files, deletes=deletes)
            return result
        try:
            payload = r.json()
        except ValueError:
            result.errors.append(f"sync batch: non-JSON response {r.text[:200]}")
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            self._record_manifest_failure(files=files, deletes=deletes)
            return result
        body_out = payload.get("result", payload)
        if not isinstance(body_out, dict) or not body_out.get("ok", False):
            message = "unknown"
            if isinstance(body_out, dict):
                message = str(body_out.get("error") or body_out.get("message") or message)
            result.errors.append(f"sync batch: {message}")
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            self._record_manifest_failure(files=files, deletes=deletes)
            return result
        pushed_raw = (
            body_out.get("files_accepted")
            if "files_accepted" in body_out else len(files)
        )
        deleted_raw = (
            body_out.get("deletes_accepted")
            if "deletes_accepted" in body_out else len(deletes)
        )
        result.pushed = int(pushed_raw or 0)
        result.deleted = int(deleted_raw or 0)
        result.accepted_revision = int(
            body_out.get("accepted_revision") or client_revision
        )
        result.indexed_revision = int(
            body_out.get("indexed_revision") or result.accepted_revision
        )
        self._sync_revision = result.accepted_revision
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        self._record_manifest_ack(files=files, deletes=deletes, result=result)
        return result

    # ------------------------------------------------------------ public API
    def health(self) -> bool:
        """Probe the remote /health endpoint. Returns True iff 200."""
        try:
            r = self._client.get("/health", timeout=5.0, headers=self._headers())
            return bool(r.status_code == 200)
        except Exception:
            return False

    def flush_pending(self, *, max_batches: int = 10) -> AgentResult:
        """Retry manifest pending operations before sending newer changes."""
        result = AgentResult()
        started = time.monotonic()
        manifest = self._load_manifest()
        if manifest is None or not manifest.pending:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        for _ in range(max(1, int(max_batches or 1))):
            queue = SyncQueue(manifest)
            batch = queue.next_batch(
                max_files=self._batch_max_files,
                max_bytes=self._batch_max_bytes,
            )
            if batch is None:
                break
            batch_result = self._push_sync_batch(
                files=[
                    {
                        "path": item.path,
                        "hash": item.hash,
                        "size": item.size,
                        "mtime_ms": item.mtime_ms,
                        "encoding": item.encoding,
                        "content": item.content,
                    }
                    for item in batch.files
                ],
                deletes=[item.path for item in batch.deletes],
                started=time.monotonic(),
                metadata={"phase": "pending_flush"},
            )
            result.merge(batch_result)
            if batch_result.errors:
                break
            manifest = self._load_manifest()
            if manifest is None or not manifest.pending:
                break

        if not result.sync_protocol:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def push_file(self, path: str | Path) -> AgentResult:
        """Upload a single file body through /sync/batch."""
        result = AgentResult()
        started = time.monotonic()

        result.merge(self.flush_pending())
        if result.errors:
            return result

        rel = self._rel(Path(path)) if not isinstance(path, str) else path
        if _is_excluded(rel, self._excludes) or _is_binary_path(rel):
            result.skipped = 1
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        text = self._read_text(rel)
        if text is None:
            result.skipped = 1
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        result.merge(
            self._push_sync_batch(
                files=[self._sync_file_payload(rel, text)],
                deletes=[],
                started=started,
            )
        )
        return result

    def push_batch(
        self,
        paths: Iterable[str | Path],
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AgentResult:
        """Upload many files through chunked /sync/batch requests."""
        result = AgentResult()
        started = time.monotonic()
        batch: list[dict[str, Any]] = []
        batch_bytes = 0

        result.merge(self.flush_pending())
        if result.errors:
            return result

        def _flush() -> None:
            nonlocal batch, batch_bytes
            if not batch:
                return
            result.merge(
                self._push_sync_batch(
                    files=batch,
                    deletes=[],
                    started=time.monotonic(),
                    metadata=metadata,
                )
            )
            batch = []
            batch_bytes = 0

        for path in paths:
            rel = self._rel(Path(path)) if not isinstance(path, str) else path
            if _is_excluded(rel, self._excludes) or _is_binary_path(rel):
                result.skipped += 1
                continue
            text = self._read_text(rel)
            if text is None:
                result.skipped += 1
                continue
            payload = self._sync_file_payload(rel, text)
            payload_size = int(payload.get("size") or 0)
            would_exceed_files = len(batch) >= self._batch_max_files
            would_exceed_bytes = (
                bool(batch)
                and batch_bytes + payload_size > self._batch_max_bytes
            )
            if would_exceed_files or would_exceed_bytes:
                _flush()
            batch.append(payload)
            batch_bytes += payload_size
            if payload_size >= self._batch_max_bytes:
                _flush()

        _flush()
        if not result.sync_protocol:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def delete_file(self, path: str | Path) -> AgentResult:
        """Tell the remote to drop ``path`` through /sync/batch."""
        started = time.monotonic()
        result = self.flush_pending()
        if result.errors:
            return result
        rel = self._rel(Path(path)) if not isinstance(path, str) else path
        result.merge(
            self._push_sync_batch(files=[], deletes=[rel], started=started)
        )
        return result

    def sync_status(self) -> dict:
        """Read the remote sync headline."""
        if not self._workspace_id:
            return {"error": "workspace_id is required for /sync"}
        try:
            r = self._client.get("/sync/status", headers=self._headers())
            if r.status_code == 200:
                payload = r.json()
                body = payload.get("result", payload)
                if isinstance(body, dict):
                    return body
            return {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


__all__ = ["AgentClient", "AgentResult"]
