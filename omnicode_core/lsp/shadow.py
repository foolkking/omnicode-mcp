"""State-dir shadow workspace for language servers that write into project roots."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


_SKIP_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    ".metals",
    ".bloop",
    ".scala-build",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}
_MAX_FILE_BYTES = 8_000_000


class LSPShadowWorkspace:
    """Incrementally materialize a writable language-server workspace."""

    def __init__(self, source_root: str | Path, shadow_root: str | Path) -> None:
        self.source_root = Path(source_root).resolve()
        self.shadow_root = Path(shadow_root).resolve()
        self.workspace_root = self.shadow_root / "workspace"
        self.manifest_path = self.shadow_root / "manifest.json"
        self.status_path = self.shadow_root / "status.json"

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.is_file():
            return {}
        try:
            payload = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _signature(path: Path) -> dict[str, int]:
        stat = path.stat()
        return {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }

    def _allowed_file(self, path: Path) -> bool:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                return False
        except OSError:
            return False
        return True

    def sync_file(self, relative_path: str) -> dict[str, Any]:
        normalized = relative_path.replace("\\", "/").lstrip("/")
        source = (self.source_root / normalized).resolve()
        try:
            source.relative_to(self.source_root)
        except ValueError as exc:
            raise ValueError("shadow path escapes source workspace") from exc
        target = self.workspace_root / normalized
        if not source.is_file():
            if target.exists():
                target.unlink()
            return {"path": normalized, "deleted": True}
        if not self._allowed_file(source):
            return {"path": normalized, "skipped": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {"path": normalized, "copied": True}

    def sync_full(self) -> dict[str, Any]:
        started = time.monotonic()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        previous = self._load_manifest()
        current: dict[str, dict[str, Any]] = {}
        copied = 0
        unchanged = 0
        skipped = 0
        errors = 0

        for root, dir_names, file_names in os.walk(self.source_root):
            dir_names[:] = [
                name
                for name in dir_names
                if name not in _SKIP_DIRS
            ]
            current_root = Path(root)
            for file_name in file_names:
                source = current_root / file_name
                try:
                    relative = source.relative_to(
                        self.source_root
                    ).as_posix()
                    if not self._allowed_file(source):
                        skipped += 1
                        continue
                    signature = self._signature(source)
                    current[relative] = signature
                    target = self.workspace_root / relative
                    if (
                        previous.get(relative) == signature
                        and target.is_file()
                    ):
                        unchanged += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied += 1
                except OSError:
                    errors += 1

        deleted = 0
        for relative in sorted(set(previous) - set(current)):
            target = self.workspace_root / relative
            try:
                if target.is_file():
                    target.unlink()
                    deleted += 1
            except OSError:
                errors += 1

        self.shadow_root.mkdir(parents=True, exist_ok=True)
        tmp_manifest = self.manifest_path.with_suffix(".tmp")
        tmp_manifest.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_manifest.replace(self.manifest_path)
        status = {
            "ready": errors == 0,
            "source_root": str(self.source_root),
            "workspace_root": str(self.workspace_root),
            "files": len(current),
            "copied": copied,
            "unchanged": unchanged,
            "deleted": deleted,
            "skipped": skipped,
            "errors": errors,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": time.time(),
        }
        self.status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return status

    def status(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            return {
                "ready": False,
                "workspace_root": str(self.workspace_root),
                "reason": "shadow_workspace_not_bootstrapped",
            }
        try:
            payload = json.loads(
                self.status_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            return {
                "ready": False,
                "workspace_root": str(self.workspace_root),
                "reason": f"{exc.__class__.__name__}: {exc}",
            }
        return payload if isinstance(payload, dict) else {
            "ready": False,
            "workspace_root": str(self.workspace_root),
            "reason": "invalid_shadow_status",
        }


__all__ = ["LSPShadowWorkspace"]
