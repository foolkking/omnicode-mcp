"""Local workspace path model shared by MCP, agent, and sync code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class WorkspacePathError(ValueError):
    """Raised when a user path cannot be safely resolved in a workspace."""


@dataclass(frozen=True)
class LocalWorkspace:
    root: Path
    workspace_id: str

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.exists():
            raise WorkspacePathError(f"workspace root does not exist: {root}")
        if not root.is_dir():
            raise WorkspacePathError(f"workspace root is not a directory: {root}")
        if not (self.workspace_id or "").strip():
            raise WorkspacePathError("workspace_id cannot be empty")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "workspace_id", self.workspace_id.strip())

    def normalize_rel(self, rel_path: str) -> str:
        """Normalize a workspace-relative path to POSIX separators."""
        raw = (rel_path or "").strip()
        if not raw:
            raise WorkspacePathError("path cannot be empty")

        raw = raw.replace("\\", "/")
        if Path(raw).is_absolute() or raw.startswith("/"):
            raise WorkspacePathError(
                f"path must be workspace-relative: {rel_path!r}"
            )

        parts: list[str] = []
        for part in PurePosixPath(raw).parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise WorkspacePathError(
                    f"path escapes workspace: {rel_path!r}"
                )
            parts.append(part)
        if not parts:
            raise WorkspacePathError("path cannot resolve to workspace root")
        return "/".join(parts)

    def assert_inside(self, path: str | Path) -> None:
        """Reject paths that resolve outside this workspace."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.root / self.normalize_rel(str(path))
        resolved = p.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(
                f"path escapes workspace: {path!r} -> {resolved}"
            ) from exc

    def to_absolute(self, rel_path: str) -> Path:
        """Convert a normalized relative path to an absolute workspace path."""
        rel = self.normalize_rel(rel_path)
        out = (self.root / rel).resolve()
        self.assert_inside(out)
        return out

    def to_relative(self, path: str | Path) -> str:
        """Convert a relative or in-workspace absolute path to normalized rel."""
        p = Path(path).expanduser()
        if p.is_absolute():
            resolved = p.resolve()
            try:
                rel = resolved.relative_to(self.root)
            except ValueError as exc:
                raise WorkspacePathError(
                    f"path escapes workspace: {path!r} -> {resolved}"
                ) from exc
            return self.normalize_rel(rel.as_posix())
        return self.normalize_rel(str(path))


__all__ = ["LocalWorkspace", "WorkspacePathError"]
