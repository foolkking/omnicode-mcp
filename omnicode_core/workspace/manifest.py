"""Local manifest for hybrid sync state."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from omnicode_core.workspace.local import LocalWorkspace


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _workspace_root_hash(root: Path) -> str:
    return _sha256_bytes(str(root.resolve()).encode("utf-8"))


def default_manifest_path(workspace_id: str) -> Path:
    return Path.home() / ".omnicode" / "workspaces" / workspace_id / "manifest.json"


@dataclass(frozen=True)
class ManifestChange:
    op: str
    path: str
    hash: Optional[str]
    revision: int


class LocalManifest:
    """Stateful manifest file with hash-based pending queue updates."""

    def __init__(
        self,
        *,
        workspace: LocalWorkspace,
        path: Optional[Path] = None,
        max_file_bytes: int = 1_000_000,
        ignore_paths: tuple[str, ...] = (),
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.workspace = workspace
        self.path = path or default_manifest_path(workspace.workspace_id)
        self.max_file_bytes = max_file_bytes
        self.ignore_paths = tuple(ignore_paths)
        self.data = data or self._new_data()

    @classmethod
    def load(
        cls,
        *,
        workspace: LocalWorkspace,
        path: Optional[Path] = None,
        max_file_bytes: int = 1_000_000,
        ignore_paths: tuple[str, ...] = (),
    ) -> "LocalManifest":
        manifest_path = path or default_manifest_path(workspace.workspace_id)
        if manifest_path.exists():
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root must be a JSON object")
            data = raw
        else:
            data = None
        return cls(
            workspace=workspace,
            path=manifest_path,
            max_file_bytes=max_file_bytes,
            ignore_paths=ignore_paths,
            data=data,
        )

    def _new_data(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workspace_id": self.workspace.workspace_id,
            "workspace_root_hash": _workspace_root_hash(self.workspace.root),
            "client_id": f"local-{uuid.uuid4().hex}",
            "local_revision": 0,
            "last_accepted_revision": 0,
            "last_indexed_revision": 0,
            "files": {},
            "pending": [],
        }

    @property
    def local_revision(self) -> int:
        return int(self.data.get("local_revision", 0))

    @property
    def pending(self) -> list[dict[str, Any]]:
        pending = self.data.setdefault("pending", [])
        if not isinstance(pending, list):
            raise ValueError("manifest pending must be a list")
        return pending

    @property
    def files(self) -> dict[str, Any]:
        files = self.data.setdefault("files", {})
        if not isinstance(files, dict):
            raise ValueError("manifest files must be an object")
        return files

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        tmp.replace(self.path)

    def is_ignored(self, rel_path: str) -> bool:
        rel = self.workspace.normalize_rel(rel_path)
        for raw in self.ignore_paths:
            prefix = raw.replace("\\", "/").strip()
            if not prefix:
                continue
            if prefix.endswith("/"):
                prefix = prefix.rstrip("/")
                if rel == prefix or rel.startswith(prefix + "/"):
                    return True
            elif rel == prefix:
                return True
        return False

    def mark_changed(self, path: str | Path) -> Optional[ManifestChange]:
        """Update manifest state for one local path.

        Returns a ManifestChange when the pending queue changed, otherwise
        None for no-op / ignored / unsupported file.
        """
        rel = self.workspace.to_relative(path)
        if self.is_ignored(rel):
            return None

        abs_path = self.workspace.to_absolute(rel)
        if not abs_path.exists():
            return self._record_delete(rel)
        if not abs_path.is_file():
            return None

        stat = abs_path.stat()
        if stat.st_size > self.max_file_bytes:
            return None

        content = abs_path.read_bytes()
        if b"\x00" in content[:4096]:
            return None

        digest = _sha256_bytes(content)
        current = self.files.get(rel) or {}
        if current.get("hash") == digest:
            current["last_seen_at"] = _utc_now()
            self.files[rel] = current
            return None

        revision = self._next_revision()
        self.files[rel] = {
            "hash": digest,
            "size": stat.st_size,
            "mtime_ms": int(stat.st_mtime_ns / 1_000_000),
            "last_uploaded_revision": current.get("last_uploaded_revision", 0),
            "last_seen_at": _utc_now(),
        }
        self._replace_pending({"op": "upsert", "path": rel, "hash": digest})
        return ManifestChange("upsert", rel, digest, revision)

    def _record_delete(self, rel: str) -> Optional[ManifestChange]:
        if rel not in self.files:
            return None
        self.files.pop(rel, None)
        revision = self._next_revision()
        self._replace_pending({"op": "delete", "path": rel})
        return ManifestChange("delete", rel, None, revision)

    def _next_revision(self) -> int:
        revision = self.local_revision + 1
        self.data["local_revision"] = revision
        return revision

    def _replace_pending(self, op: dict[str, Any]) -> None:
        path = op["path"]
        kept = [item for item in self.pending if item.get("path") != path]
        kept.append(op)
        self.data["pending"] = kept


__all__ = ["LocalManifest", "ManifestChange", "default_manifest_path"]
