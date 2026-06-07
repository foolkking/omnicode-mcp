"""Local sync queue built on top of LocalManifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from omnicode_core.workspace.manifest import LocalManifest


@dataclass(frozen=True)
class SyncFile:
    path: str
    hash: str
    size: int
    mtime_ms: int
    encoding: str
    content: str

    @property
    def payload_bytes(self) -> int:
        return len(self.content.encode(self.encoding, errors="replace"))


@dataclass(frozen=True)
class SyncDelete:
    path: str


@dataclass(frozen=True)
class SyncBatch:
    client_id: str
    base_revision: int
    client_revision: int
    files: list[SyncFile] = field(default_factory=list)
    deletes: list[SyncDelete] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "base_revision": self.base_revision,
            "client_revision": self.client_revision,
            "files": [
                {
                    "path": f.path,
                    "hash": f.hash,
                    "size": f.size,
                    "mtime_ms": f.mtime_ms,
                    "encoding": f.encoding,
                    "content": f.content,
                }
                for f in self.files
            ],
            "deletes": [{"path": d.path} for d in self.deletes],
        }

    @property
    def paths(self) -> set[str]:
        return {f.path for f in self.files} | {d.path for d in self.deletes}


class SyncQueue:
    """Prepare manifest pending entries for the cloud sync protocol."""

    def __init__(self, manifest: LocalManifest) -> None:
        self.manifest = manifest

    def status(self) -> dict[str, Any]:
        return {
            "workspace_id": self.manifest.data.get("workspace_id"),
            "local_revision": self.manifest.local_revision,
            "last_accepted_revision": self.manifest.data.get(
                "last_accepted_revision", 0
            ),
            "last_indexed_revision": self.manifest.data.get(
                "last_indexed_revision", 0
            ),
            "pending_count": len(self.manifest.pending),
        }

    def next_batch(
        self,
        *,
        max_files: int = 25,
        max_bytes: int = 250_000,
    ) -> Optional[SyncBatch]:
        if not self.manifest.pending:
            return None
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        files: list[SyncFile] = []
        deletes: list[SyncDelete] = []
        used_bytes = 0

        for item in self.manifest.pending:
            if len(files) + len(deletes) >= max_files:
                break
            op = item.get("op")
            path = self.manifest.workspace.normalize_rel(str(item.get("path", "")))
            if op == "delete":
                deletes.append(SyncDelete(path=path))
                continue
            if op != "upsert":
                continue

            meta = self.manifest.files.get(path) or {}
            abs_path = self.manifest.workspace.to_absolute(path)
            if not abs_path.is_file():
                continue
            raw_content = abs_path.read_bytes()
            content = raw_content.decode("utf-8", errors="replace")
            sync_file = SyncFile(
                path=path,
                hash=str(item.get("hash") or meta.get("hash") or ""),
                size=int(meta.get("size") or abs_path.stat().st_size),
                mtime_ms=int(meta.get("mtime_ms") or 0),
                encoding="utf-8",
                content=content,
            )
            payload_bytes = sync_file.payload_bytes
            if files or deletes:
                if used_bytes + payload_bytes > max_bytes:
                    break
            files.append(sync_file)
            used_bytes += payload_bytes

        if not files and not deletes:
            return None

        return SyncBatch(
            client_id=str(self.manifest.data.get("client_id", "")),
            base_revision=int(self.manifest.data.get("last_accepted_revision", 0)),
            client_revision=self.manifest.local_revision,
            files=files,
            deletes=deletes,
        )

    def mark_accepted(
        self,
        batch: SyncBatch,
        *,
        accepted_revision: Optional[int] = None,
        indexed_revision: Optional[int] = None,
    ) -> None:
        """Apply a successful server acknowledgement to the manifest."""
        accepted = accepted_revision
        if accepted is None:
            accepted = batch.client_revision
        self.manifest.data["last_accepted_revision"] = accepted
        if indexed_revision is not None:
            self.manifest.data["last_indexed_revision"] = indexed_revision

        sent_paths = batch.paths
        self.manifest.data["pending"] = [
            item for item in self.manifest.pending
            if item.get("path") not in sent_paths
        ]
        for f in batch.files:
            meta = self.manifest.files.get(f.path)
            if isinstance(meta, dict):
                meta["last_uploaded_revision"] = accepted
                self.manifest.files[f.path] = meta

    def mark_failed(self, batch: SyncBatch, *, error: str) -> dict[str, Any]:
        """Return a structured failure without mutating pending entries."""
        return {
            "ok": False,
            "error": error,
            "pending_preserved": True,
            "paths": sorted(batch.paths),
            "local_revision": self.manifest.local_revision,
            "last_accepted_revision": self.manifest.data.get(
                "last_accepted_revision", 0
            ),
        }


__all__ = ["SyncBatch", "SyncDelete", "SyncFile", "SyncQueue"]
