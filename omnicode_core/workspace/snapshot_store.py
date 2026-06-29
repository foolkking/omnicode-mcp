"""Persistent cloud-side snapshot store for hybrid sync."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional


class SnapshotStoreError(ValueError):
    """Raised when snapshot storage input is invalid."""


_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def default_snapshot_store_path() -> Path:
    state_dir = os.environ.get("OMNICODE_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / "cloud-sync"
    return Path.home() / ".omnicode" / "cloud-sync"


def default_workspace_store_path() -> Path:
    explicit = os.environ.get("OMNICODE_WORKSPACE_STORE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_snapshot_store_path() / "workspaces"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _validate_workspace_id(workspace_id: str) -> str:
    value = (workspace_id or "").strip()
    if not value:
        raise SnapshotStoreError("workspace_id cannot be empty")
    if value in {".", ".."} or not _WORKSPACE_ID_RE.match(value):
        raise SnapshotStoreError(
            "workspace_id may contain only letters, numbers, '.', '_', ':', '-'"
        )
    return value


def normalize_snapshot_path(path: str) -> str:
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        raise SnapshotStoreError("path cannot be empty")
    if Path(raw).is_absolute() or raw.startswith("/"):
        raise SnapshotStoreError(f"path must be workspace-relative: {path!r}")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise SnapshotStoreError(f"path escapes workspace: {path!r}")
        parts.append(part)
    if not parts:
        raise SnapshotStoreError("path cannot resolve to workspace root")
    return "/".join(parts)


def _content_digest(content: str, encoding: str) -> str:
    return hashlib.sha256(content.encode(encoding, errors="replace")).hexdigest()


def _expected_digest(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value.lower()


def _replace_with_retry(tmp: Path, target: Path, *, attempts: int = 5) -> None:
    """Atomically replace a file, tolerating short Windows file-handle races."""

    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.02 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SnapshotRecord:
    path: str
    hash: str
    size: int
    mtime_ms: int
    encoding: str
    object_path: str
    revision: int
    updated_at: str
    mirror_path: Optional[str] = None


@dataclass(frozen=True)
class SnapshotBatchResult:
    records: list[SnapshotRecord]
    accepted_revision: int
    indexed_revision: int
    file_count: int
    delete_count: int


class CloudSnapshotStore:
    """Content-addressed snapshot storage keyed by workspace id and rel path."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        workspace_store_root: Optional[Path] = None,
        materialize_mirror: Optional[bool] = None,
        mirror_readonly: Optional[bool] = None,
    ) -> None:
        self.root = Path(root or default_snapshot_store_path()).expanduser().resolve()
        self.workspaces_root = Path(
            workspace_store_root or (
                self.root / "workspaces"
                if root is not None
                else default_workspace_store_path()
            ),
        ).expanduser().resolve()
        self.materialize_mirror = (
            _env_bool("OMNICODE_MATERIALIZE_MIRROR", True)
            if materialize_mirror is None
            else materialize_mirror
        )
        self.mirror_readonly = (
            _env_bool("OMNICODE_MIRROR_READONLY", True)
            if mirror_readonly is None
            else mirror_readonly
        )
        self._locks_guard = threading.RLock()
        self._workspace_locks: dict[str, threading.RLock] = {}

    def _workspace_lock(self, workspace_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._workspace_locks.get(workspace_id)
            if lock is None:
                lock = threading.RLock()
                self._workspace_locks[workspace_id] = lock
            return lock

    def upsert(
        self,
        *,
        workspace_id: str,
        path: str,
        content: str,
        hash_value: str,
        size: int,
        mtime_ms: int,
        encoding: str,
        revision: int,
    ) -> SnapshotRecord:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            return self._upsert_unlocked(
                workspace_id=workspace,
                path=path,
                content=content,
                hash_value=hash_value,
                size=size,
                mtime_ms=mtime_ms,
                encoding=encoding,
                revision=revision,
            )

    def _upsert_unlocked(
        self,
        *,
        workspace_id: str,
        path: str,
        content: str,
        hash_value: str,
        size: int,
        mtime_ms: int,
        encoding: str,
        revision: int,
    ) -> SnapshotRecord:
        workspace = _validate_workspace_id(workspace_id)
        rel = normalize_snapshot_path(path)
        digest = _content_digest(content, encoding)
        expected = _expected_digest(hash_value)
        if expected and expected != digest:
            raise SnapshotStoreError(
                f"hash mismatch for {rel}: expected sha256:{expected}, got sha256:{digest}"
            )
        encoded = content.encode(encoding, errors="replace")
        if size != len(encoded):
            raise SnapshotStoreError(
                f"size mismatch for {rel}: expected {size}, got {len(encoded)}"
            )

        ws_dir = self._workspace_dir(workspace)
        obj_rel = f"objects/sha256/{digest}"
        obj_path = ws_dir / obj_rel
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        if not obj_path.exists():
            tmp = obj_path.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            os.replace(tmp, obj_path)

        mirror_rel = None
        if self.materialize_mirror:
            mirror_rel = self._write_mirror(
                workspace_id=workspace,
                path=rel,
                encoded=encoded,
            )

        record = SnapshotRecord(
            path=rel,
            hash=f"sha256:{digest}",
            size=size,
            mtime_ms=mtime_ms,
            encoding=encoding,
            object_path=obj_rel,
            revision=revision,
            updated_at=_utc_now(),
            mirror_path=mirror_rel,
        )
        index = self._load_index(workspace)
        files = index.setdefault("files", {})
        files[rel] = asdict(record)
        self._set_accepted_revision(index, revision)
        index["workspace_id"] = workspace
        self._save_index(workspace, index)
        return record

    def delete(self, *, workspace_id: str, path: str, revision: int) -> None:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            self._delete_unlocked(
                workspace_id=workspace,
                path=path,
                revision=revision,
            )

    def _delete_unlocked(self, *, workspace_id: str, path: str, revision: int) -> None:
        workspace = _validate_workspace_id(workspace_id)
        rel = normalize_snapshot_path(path)
        index = self._load_index(workspace)
        files = index.setdefault("files", {})
        if isinstance(files, dict):
            files.pop(rel, None)
        tombstones = index.setdefault("deletes", {})
        if isinstance(tombstones, dict):
            tombstones[rel] = {"path": rel, "revision": revision, "deleted_at": _utc_now()}
        if self.materialize_mirror:
            self._delete_mirror(workspace_id=workspace, path=rel)
        self._set_accepted_revision(index, revision)
        index["workspace_id"] = workspace
        self._save_index(workspace, index)

    def apply_batch(
        self,
        *,
        workspace_id: str,
        files: list[dict[str, Any]],
        deletes: list[str],
        revision: int,
    ) -> SnapshotBatchResult:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            return self._apply_batch_unlocked(
                workspace_id=workspace,
                files=files,
                deletes=deletes,
                revision=revision,
            )

    def _apply_batch_unlocked(
        self,
        *,
        workspace_id: str,
        files: list[dict[str, Any]],
        deletes: list[str],
        revision: int,
    ) -> SnapshotBatchResult:
        workspace = _validate_workspace_id(workspace_id)
        index = self._load_index(workspace)
        index_files = index.setdefault("files", {})
        if not isinstance(index_files, dict):
            index_files = {}
            index["files"] = index_files
        tombstones = index.setdefault("deletes", {})
        if not isinstance(tombstones, dict):
            tombstones = {}
            index["deletes"] = tombstones

        records: list[SnapshotRecord] = []
        for item in files:
            raw_path = str(item["path"])
            encoding = str(item.get("encoding") or "utf-8")
            content = str(item.get("content") or "")
            hash_value = str(item.get("hash") or "")
            size = int(item.get("size") or 0)
            mtime_ms = int(item.get("mtime_ms") or 0)
            rel = normalize_snapshot_path(raw_path)
            digest = _content_digest(content, encoding)
            expected = _expected_digest(hash_value)
            if expected and expected != digest:
                raise SnapshotStoreError(
                    f"hash mismatch for {rel}: expected sha256:{expected}, got sha256:{digest}"
                )
            encoded = content.encode(encoding, errors="replace")
            if size != len(encoded):
                raise SnapshotStoreError(
                    f"size mismatch for {rel}: expected {size}, got {len(encoded)}"
                )

            ws_dir = self._workspace_dir(workspace)
            obj_rel = f"objects/sha256/{digest}"
            obj_path = ws_dir / obj_rel
            obj_path.parent.mkdir(parents=True, exist_ok=True)
            if not obj_path.exists():
                tmp = obj_path.with_suffix(".tmp")
                tmp.write_bytes(encoded)
                os.replace(tmp, obj_path)

            mirror_rel = None
            if self.materialize_mirror:
                mirror_rel = self._write_mirror(
                    workspace_id=workspace,
                    path=rel,
                    encoded=encoded,
                )

            record = SnapshotRecord(
                path=rel,
                hash=f"sha256:{digest}",
                size=size,
                mtime_ms=mtime_ms,
                encoding=encoding,
                object_path=obj_rel,
                revision=revision,
                updated_at=_utc_now(),
                mirror_path=mirror_rel,
            )
            index_files[rel] = asdict(record)
            tombstones.pop(rel, None)
            records.append(record)

        for raw_path in deletes:
            rel = normalize_snapshot_path(raw_path)
            index_files.pop(rel, None)
            tombstones[rel] = {
                "path": rel,
                "revision": revision,
                "deleted_at": _utc_now(),
            }
            if self.materialize_mirror:
                self._delete_mirror(workspace_id=workspace, path=rel)

        self._set_accepted_revision(index, revision)
        index["workspace_id"] = workspace
        self._save_index(workspace, index)
        accepted = int(index.get("accepted_revision", index.get("latest_revision", 0)))
        indexed = int(index.get("indexed_revision", 0))
        return SnapshotBatchResult(
            records=records,
            accepted_revision=accepted,
            indexed_revision=indexed,
            file_count=len(index_files),
            delete_count=len(tombstones),
        )

    def mark_indexed(
        self,
        *,
        workspace_id: str,
        revision: int,
        semantic_coverage: Optional[str] = None,
    ) -> int:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            return self._mark_indexed_unlocked(
                workspace_id=workspace,
                revision=revision,
                semantic_coverage=semantic_coverage,
            )

    def _mark_indexed_unlocked(
        self,
        *,
        workspace_id: str,
        revision: int,
        semantic_coverage: Optional[str] = None,
    ) -> int:
        workspace = _validate_workspace_id(workspace_id)
        index = self._load_index(workspace)
        accepted = int(index.get("accepted_revision", index.get("latest_revision", 0)))
        if revision > accepted:
            raise SnapshotStoreError(
                f"indexed revision {revision} exceeds accepted revision {accepted}"
            )
        indexed = max(int(index.get("indexed_revision", 0)), revision)
        index["indexed_revision"] = indexed
        index["workspace_id"] = workspace
        coverage = (semantic_coverage or "").strip()
        if coverage:
            if coverage in {
                "initial_sync_exact_only",
                "initial_sync_large_repo_exact_only",
                "exact_only_initial_sync",
            }:
                index["semantic_initial_exact_only"] = True
                index["semantic_index_coverage"] = "exact_only_initial_sync"
            elif index.get("semantic_initial_exact_only"):
                if coverage in {"semantic_full", "selected_files", "filtered"}:
                    index["semantic_initial_exact_only"] = False
                    index["semantic_index_coverage"] = coverage
                elif coverage not in {"unchanged", "deletes_only", "filtered_empty"}:
                    index["semantic_index_coverage"] = "partial_after_exact_only"
            elif coverage not in {"unchanged", "deletes_only"}:
                index["semantic_index_coverage"] = coverage
        self._save_index(workspace, index)
        return indexed

    def status(self, workspace_id: str) -> dict[str, Any]:
        workspace = _validate_workspace_id(workspace_id)
        fast = self._load_status_summary(workspace)
        if fast is not None:
            return fast
        with self._workspace_lock(workspace):
            return self._status_unlocked(workspace)

    def _status_unlocked(self, workspace: str) -> dict[str, Any]:
        index = self._load_index(workspace)
        files = index.get("files", {})
        deletes = index.get("deletes", {})
        accepted = int(index.get("accepted_revision", index.get("latest_revision", 0)))
        indexed = int(index.get("indexed_revision", 0))
        return {
            "workspace_id": workspace,
            "latest_revision": accepted,
            "accepted_revision": accepted,
            "indexed_revision": indexed,
            "semantic_index_coverage": str(
                index.get("semantic_index_coverage") or "unknown"
            ),
            "semantic_initial_exact_only": bool(
                index.get("semantic_initial_exact_only", False)
            ),
            "file_count": len(files) if isinstance(files, dict) else 0,
            "delete_count": len(deletes) if isinstance(deletes, dict) else 0,
        }

    def file_hashes(self, workspace_id: str) -> dict[str, str]:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            return self._file_hashes_unlocked(workspace)

    def _file_hashes_unlocked(self, workspace: str) -> dict[str, str]:
        index = self._load_index(workspace)
        files = index.get("files", {})
        if not isinstance(files, dict):
            return {}
        hashes: dict[str, str] = {}
        for path, raw in files.items():
            if not isinstance(path, str) or not isinstance(raw, dict):
                continue
            hash_value = raw.get("hash")
            if isinstance(hash_value, str):
                hashes[path] = hash_value
        return hashes

    def list_records(self, *, workspace_id: str) -> list[SnapshotRecord]:
        workspace = _validate_workspace_id(workspace_id)
        with self._workspace_lock(workspace):
            return self._list_records_unlocked(workspace)

    def _list_records_unlocked(self, workspace: str) -> list[SnapshotRecord]:
        index = self._load_index(workspace)
        files = index.get("files", {})
        if not isinstance(files, dict):
            return []
        records: list[SnapshotRecord] = []
        for raw in files.values():
            if not isinstance(raw, dict):
                continue
            try:
                records.append(
                    SnapshotRecord(
                        path=str(raw["path"]),
                        hash=str(raw["hash"]),
                        size=int(raw["size"]),
                        mtime_ms=int(raw["mtime_ms"]),
                        encoding=str(raw.get("encoding") or "utf-8"),
                        object_path=str(raw["object_path"]),
                        revision=int(raw["revision"]),
                        updated_at=str(raw["updated_at"]),
                        mirror_path=(
                            str(raw["mirror_path"])
                            if raw.get("mirror_path") is not None
                            else None
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def get_record(self, *, workspace_id: str, path: str) -> Optional[SnapshotRecord]:
        rel = normalize_snapshot_path(path)
        for record in self.list_records(workspace_id=workspace_id):
            if record.path == rel:
                return record
        return None

    def read_text(self, *, workspace_id: str, path: str) -> Optional[str]:
        workspace = _validate_workspace_id(workspace_id)
        rel = normalize_snapshot_path(path)
        with self._workspace_lock(workspace):
            return self._read_text_unlocked(workspace=workspace, path=rel)

    def _read_text_unlocked(self, *, workspace: str, path: str) -> Optional[str]:
        index = self._load_index(workspace)
        files = index.get("files", {})
        if not isinstance(files, dict):
            return None
        raw = files.get(path)
        if not isinstance(raw, dict):
            return None
        object_path = raw.get("object_path")
        if not isinstance(object_path, str) or not object_path:
            return None
        obj = self._workspace_dir(workspace) / object_path
        if not obj.is_file():
            return None
        return obj.read_text(
            encoding=str(raw.get("encoding") or "utf-8"),
            errors="replace",
        )

    def read_record_text(
        self,
        *,
        workspace_id: str,
        record: SnapshotRecord,
    ) -> Optional[str]:
        """Read a known snapshot record without reloading the workspace index."""
        workspace = _validate_workspace_id(workspace_id)
        rel = normalize_snapshot_path(record.path)
        with self._workspace_lock(workspace):
            if rel != record.path:
                return None
            obj = self._workspace_dir(workspace) / record.object_path
            ws_dir = self._workspace_dir(workspace).resolve()
            try:
                resolved = obj.resolve()
            except OSError:
                return None
            if resolved != ws_dir and ws_dir not in resolved.parents:
                raise SnapshotStoreError(
                    f"snapshot object path escapes workspace: {record.path}"
                )
            if not resolved.is_file():
                return None
            return resolved.read_text(
                encoding=record.encoding or "utf-8",
                errors="replace",
            )

    def _workspace_dir(self, workspace_id: str) -> Path:
        return self.workspaces_root / workspace_id

    def _mirror_root(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "mirror"

    def _mirror_path(self, workspace_id: str, path: str) -> Path:
        return self._mirror_root(workspace_id).joinpath(*PurePosixPath(path).parts)

    def _index_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "index.json"

    def _status_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "status.json"

    def _ensure_under(self, root: Path, path: Path) -> None:
        root_resolved = root.resolve()
        parent_resolved = path.parent.resolve()
        if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
            raise SnapshotStoreError(f"mirror path escapes workspace: {path}")

    def _write_mirror(self, *, workspace_id: str, path: str, encoded: bytes) -> str:
        mirror_root = self._mirror_root(workspace_id)
        target = self._mirror_path(workspace_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_under(mirror_root, target)
        if target.is_symlink():
            raise SnapshotStoreError(f"mirror path is a symlink: {path}")
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(encoded)
        self._replace_mirror_file(tmp=tmp, target=target)
        if self.mirror_readonly:
            target.chmod(stat.S_IREAD)
        return "mirror/" + path

    def _replace_mirror_file(self, *, tmp: Path, target: Path) -> None:
        """Replace a mirror file, including Windows readonly targets.

        The mirror is intentionally read-only for consumers, but the backend
        must still be able to materialize a newer snapshot over the same path.
        On Windows, replacing a readonly destination may fail even after a
        previous chmod attempt, so the fallback clears the bit, unlinks the
        old file, then moves the temp file into place.
        """
        attempts = 3
        last_error: PermissionError | None = None
        for attempt in range(attempts):
            try:
                if target.exists():
                    target.chmod(stat.S_IREAD | stat.S_IWRITE)
                os.replace(tmp, target)
                return
            except PermissionError as exc:
                last_error = exc
                try:
                    if target.exists():
                        target.chmod(stat.S_IREAD | stat.S_IWRITE)
                        target.unlink()
                    os.replace(tmp, target)
                    return
                except PermissionError as retry_exc:
                    last_error = retry_exc
                    if attempt < attempts - 1:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    break
        if last_error is not None:
            raise last_error
        os.replace(tmp, target)

    def _delete_mirror(self, *, workspace_id: str, path: str) -> None:
        mirror_root = self._mirror_root(workspace_id)
        target = self._mirror_path(workspace_id, path)
        mirror_root.mkdir(parents=True, exist_ok=True)
        self._ensure_under(mirror_root, target)
        if target.is_symlink():
            raise SnapshotStoreError(f"mirror path is a symlink: {path}")
        if target.exists():
            target.chmod(stat.S_IREAD | stat.S_IWRITE)
            target.unlink()
        parent = target.parent
        while parent != mirror_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _load_index(self, workspace_id: str) -> dict[str, Any]:
        path = self._index_path(workspace_id)
        if not path.exists():
            return {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "latest_revision": 0,
                "accepted_revision": 0,
                "indexed_revision": 0,
                "files": {},
                "deletes": {},
            }
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SnapshotStoreError("snapshot index root must be a JSON object")
        raw.setdefault("files", {})
        raw.setdefault("deletes", {})
        raw.setdefault("latest_revision", 0)
        raw.setdefault("accepted_revision", raw.get("latest_revision", 0))
        raw.setdefault("indexed_revision", 0)
        return raw

    def _save_index(self, workspace_id: str, index: dict[str, Any]) -> None:
        path = self._index_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        _replace_with_retry(tmp, path)
        self._save_status_summary(workspace_id, index)

    def _status_from_index(self, workspace_id: str, index: dict[str, Any]) -> dict[str, Any]:
        files = index.get("files", {})
        deletes = index.get("deletes", {})
        accepted = int(index.get("accepted_revision", index.get("latest_revision", 0)))
        indexed = int(index.get("indexed_revision", 0))
        return {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "latest_revision": accepted,
            "accepted_revision": accepted,
            "indexed_revision": indexed,
            "semantic_index_coverage": str(
                index.get("semantic_index_coverage") or "unknown"
            ),
            "semantic_initial_exact_only": bool(
                index.get("semantic_initial_exact_only", False)
            ),
            "file_count": len(files) if isinstance(files, dict) else 0,
            "delete_count": len(deletes) if isinstance(deletes, dict) else 0,
        }

    def _save_status_summary(self, workspace_id: str, index: dict[str, Any]) -> None:
        path = self._status_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                self._status_from_index(workspace_id, index),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
            newline="\n",
        )
        _replace_with_retry(tmp, path)

    def _load_status_summary(self, workspace_id: str) -> Optional[dict[str, Any]]:
        path = self._status_path(workspace_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return {
            "workspace_id": str(raw.get("workspace_id") or workspace_id),
            "latest_revision": int(raw.get("latest_revision", 0) or 0),
            "accepted_revision": int(raw.get("accepted_revision", 0) or 0),
            "indexed_revision": int(raw.get("indexed_revision", 0) or 0),
            "semantic_index_coverage": str(
                raw.get("semantic_index_coverage") or "unknown"
            ),
            "semantic_initial_exact_only": bool(
                raw.get("semantic_initial_exact_only", False)
            ),
            "file_count": int(raw.get("file_count", 0) or 0),
            "delete_count": int(raw.get("delete_count", 0) or 0),
        }

    def _set_accepted_revision(self, index: dict[str, Any], revision: int) -> None:
        accepted = max(
            int(index.get("accepted_revision", index.get("latest_revision", 0))),
            revision,
        )
        index["accepted_revision"] = accepted
        index["latest_revision"] = accepted


__all__ = [
    "CloudSnapshotStore",
    "SnapshotBatchResult",
    "SnapshotRecord",
    "SnapshotStoreError",
    "default_snapshot_store_path",
    "default_workspace_store_path",
    "normalize_snapshot_path",
]
