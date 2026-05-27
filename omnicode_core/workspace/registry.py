"""User-level workspace registry.

Stores workspace bookmarks in JSON at
``~/.kiro/codebase-mcp/workspaces.json`` so they survive server restarts
and are shared across the user's projects (matching how the provider DB
is now stored — see omnicode/config/settings.py).

Each workspace entry is a small JSON object:

```json
{
  "id":   "wk_<uuid>",
  "name": "my-app",
  "path": "C:/Users/me/projects/my-app",
  "created_at": "2026-05-27T12:00:00+00:00",
  "active":     true
}
```

Exactly one workspace can be active. Switching active workspace just
flips the flag — actually applying it (re-initialising services in
``main`` for the new ``WORKING_DIR``) is the caller's job. We expose
``get_active_path()`` for convenience.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store_path() -> Path:
    return Path.home() / ".kiro" / "codebase-mcp" / "workspaces.json"


@dataclass
class Workspace:
    id: str
    name: str
    path: str
    created_at: str = field(default_factory=_now)
    active: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        return cls(
            id=d["id"],
            name=d["name"],
            path=d["path"],
            created_at=d.get("created_at", _now()),
            active=bool(d.get("active", False)),
        )


class WorkspaceRegistry:
    """Thread-safe JSON-backed workspace bookmark store."""

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path: Path = store_path or _default_store_path()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ persistence
    def _load(self) -> List[Workspace]:
        if not self.store_path.exists():
            return []
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "workspaces.json unreadable (%s); starting fresh.", exc
            )
            return []
        out: List[Workspace] = []
        for raw in data.get("workspaces", []):
            try:
                out.append(Workspace.from_dict(raw))
            except Exception as exc:
                logger.warning("Skipping malformed workspace entry: %s", exc)
        return out

    def _save(self, items: List[Workspace]) -> None:
        payload = {"workspaces": [w.to_dict() for w in items]}
        tmp = self.store_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.store_path)

    # ------------------------------------------------------------ public API
    def list(self) -> List[Workspace]:
        with _LOCK:
            return self._load()

    def get(self, workspace_id: str) -> Optional[Workspace]:
        with _LOCK:
            for w in self._load():
                if w.id == workspace_id:
                    return w
        return None

    def get_active(self) -> Optional[Workspace]:
        with _LOCK:
            for w in self._load():
                if w.active:
                    return w
        return None

    def get_active_path(self) -> Optional[str]:
        active = self.get_active()
        return active.path if active else None

    def add(self, name: str, path: str, set_active: bool = False) -> Workspace:
        abs_path = str(Path(path).expanduser().resolve())
        if not Path(abs_path).is_dir():
            raise NotADirectoryError(abs_path)
        with _LOCK:
            items = self._load()
            # If a workspace with the same path already exists, return it.
            for w in items:
                if w.path == abs_path:
                    if set_active:
                        for other in items:
                            other.active = (other.id == w.id)
                        self._save(items)
                    return w
            new = Workspace(
                id=f"wk_{uuid.uuid4().hex[:12]}",
                name=name or Path(abs_path).name,
                path=abs_path,
                active=set_active,
            )
            if set_active:
                for w in items:
                    w.active = False
            items.append(new)
            self._save(items)
            return new

    def remove(self, workspace_id: str) -> bool:
        with _LOCK:
            items = self._load()
            kept = [w for w in items if w.id != workspace_id]
            if len(kept) == len(items):
                return False
            # If we removed the active one, promote the first remaining.
            if not any(w.active for w in kept) and kept:
                kept[0].active = True
            self._save(kept)
            return True

    def set_active(self, workspace_id: str) -> Optional[Workspace]:
        with _LOCK:
            items = self._load()
            target: Optional[Workspace] = None
            for w in items:
                w.active = (w.id == workspace_id)
                if w.active:
                    target = w
            if target is None:
                return None
            self._save(items)
            return target

    def rename(self, workspace_id: str, new_name: str) -> Optional[Workspace]:
        with _LOCK:
            items = self._load()
            target: Optional[Workspace] = None
            for w in items:
                if w.id == workspace_id:
                    w.name = new_name
                    target = w
                    break
            if target is None:
                return None
            self._save(items)
            return target


_DEFAULT_REGISTRY: Optional[WorkspaceRegistry] = None


def get_workspace_registry() -> WorkspaceRegistry:
    """Return the process-wide default registry (lazy)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = WorkspaceRegistry()
    return _DEFAULT_REGISTRY


__all__ = ["Workspace", "WorkspaceRegistry", "get_workspace_registry"]
