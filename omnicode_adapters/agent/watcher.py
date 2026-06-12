"""Filesystem-watching wrapper around :class:`AgentClient`.

The watcher coalesces rapid bursts of file events (saving 8 files in
1.5 s should produce one /sync/batch HTTP call, not 8 sequential calls)
and feeds the results back to the user via simple stdout prints.

Falls back to a polling loop if ``watchfiles`` isn't installed so the
agent still works on locked-down systems, albeit at a higher CPU cost.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from omnicode_adapters.agent.client import AgentClient, AgentResult, _is_excluded

logger = logging.getLogger(__name__)


@dataclass
class InitialWalkResult:
    paths: list[str]
    files_seen: int
    truncated: bool = False
    cap: Optional[int] = None


def _initial_sync_cap_from_env() -> Optional[int]:
    """Read the optional initial-sync file cap from the environment.

    The default is no file-count cap. Set OMNICODE_AGENT_MAX_INITIAL_FILES to a
    positive integer to cap the startup walk for very large repositories.
    """
    raw = os.environ.get("OMNICODE_AGENT_MAX_INITIAL_FILES", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "agent: ignoring invalid OMNICODE_AGENT_MAX_INITIAL_FILES=%r",
            raw,
        )
        return None
    return value if value > 0 else None


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("agent: ignoring invalid %s=%r", name, raw)
        return default
    return value if value > 0 else default


def _initial_walk(workspace: Path, max_files: Optional[int] = None) -> InitialWalkResult:
    """One-shot scan to seed the remote index on first connect."""
    out: list[str] = []
    files_seen = 0
    cap = int(max_files or 0)
    for root, dirs, files in os.walk(workspace):
        rel_root = os.path.relpath(root, workspace).replace("\\", "/")
        # Prune ignored dirs in-place so os.walk does not descend into them.
        dirs[:] = [
            d
            for d in dirs
            if not _is_excluded((rel_root + "/" + d + "/").lstrip("./"), ())
        ]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace).replace(
                "\\", "/"
            )
            if _is_excluded(rel, ()):
                continue
            files_seen += 1
            out.append(rel)
            if cap > 0 and len(out) >= cap:
                logger.warning(
                    "agent: initial walk hit the %d-file cap; later changes "
                    "will still sync via the watch loop.",
                    cap,
                )
                return InitialWalkResult(
                    paths=out,
                    files_seen=files_seen,
                    truncated=True,
                    cap=cap,
                )
    return InitialWalkResult(paths=out, files_seen=files_seen, cap=cap or None)


class Watcher:
    """Glue between ``watchfiles`` and ``AgentClient``."""

    def __init__(
        self,
        client: AgentClient,
        workspace: Path,
        debounce_ms: int = 800,
        printer: Callable[[str], None] = print,
    ) -> None:
        self._client = client
        self._workspace = workspace.resolve()
        self._debounce_ms = debounce_ms
        self._print = printer

    # ------------------------------------------------------------ helpers
    def _resolve_event_paths(self, raw_changes) -> tuple[list[str], list[str]]:
        """Bucket raw watchfiles events into (modified, deleted) paths."""
        # ``watchfiles.Change`` is an IntEnum (added=1, modified=2, deleted=3).
        modified: set[str] = set()
        deleted: set[str] = set()
        for change_type, path in raw_changes:
            try:
                rel = str(Path(path).resolve().relative_to(self._workspace))
            except ValueError:
                # Outside the workspace: ignore.
                continue
            rel = rel.replace("\\", "/")
            if int(change_type) == 3:  # Change.deleted
                deleted.add(rel)
            else:
                modified.add(rel)
        return sorted(modified), sorted(deleted)

    # ------------------------------------------------------------ entry points
    def initial_sync(self) -> AgentResult:
        """Push every file in the workspace once on startup."""
        walk = _initial_walk(
            self._workspace,
            max_files=_initial_sync_cap_from_env(),
        )
        paths = walk.paths
        if not paths:
            return AgentResult(
                files_seen=walk.files_seen,
                initial_sync_truncated=walk.truncated,
                initial_sync_cap=walk.cap,
            )
        if walk.truncated:
            self._print(
                f"[agent] initial sync: {len(paths)} of {walk.files_seen} "
                f"files (truncated at cap={walk.cap})..."
            )
        else:
            self._print(f"[agent] initial sync: {len(paths)} files...")
        result = self._client.push_batch(
            paths,
            metadata={
                "phase": "initial_sync",
                "files_seen": walk.files_seen,
                "files_pushed": len(paths),
                "truncated": walk.truncated,
                "cap": walk.cap,
            },
        )
        result.files_seen = walk.files_seen
        result.initial_sync_truncated = walk.truncated
        result.initial_sync_cap = walk.cap
        self._print(
            f"[agent] initial sync done - pushed={result.pushed} "
            f"skipped={result.skipped} errors={len(result.errors)} "
            f"({result.elapsed_ms} ms)"
        )
        if walk.truncated:
            self._print(
                "[agent] initial sync was truncated; set "
                "OMNICODE_AGENT_MAX_INITIAL_FILES=0 or a larger value to "
                "scan more files."
            )
        return result

    def run(self) -> None:
        """Block forever, reacting to filesystem events."""
        try:
            from watchfiles import watch  # type: ignore[import-not-found]
        except ImportError:
            self._print(
                "[agent] watchfiles not installed; install via "
                "`pip install omnicode-mcp[agent]` for live sync. "
                "Falling back to a 5s poll loop."
            )
            self._run_polling()
            return

        self._print(f"[agent] watching {self._workspace}...")
        for raw in watch(
            str(self._workspace),
            debounce=self._debounce_ms,
            yield_on_timeout=False,
            recursive=True,
        ):
            modified, deleted = self._resolve_event_paths(raw)
            if not modified and not deleted:
                continue
            self._sync_burst(modified, deleted)

    # ------------------------------------------------------------ internals
    def _sync_burst(self, modified: list[str], deleted: list[str]) -> None:
        agg = AgentResult()
        if modified:
            agg.merge(self._client.push_batch(modified))
        for rel in deleted:
            agg.merge(self._client.delete_file(rel))
        self._print(
            f"[agent] sync: pushed={agg.pushed} deleted={agg.deleted} "
            f"skipped={agg.skipped} errors={len(agg.errors)} "
            f"({agg.elapsed_ms} ms)"
        )
        for err in agg.errors[:5]:
            self._print(f"[agent]   ! {err}")

    def _run_polling(self, interval: float = 5.0) -> None:
        """Best-effort fallback when watchfiles is not installed."""
        last_seen: dict[str, float] = {}
        while True:
            try:
                modified: list[str] = []
                deleted: list[str] = []
                current: set[str] = set()
                for rel in _initial_walk(self._workspace).paths:
                    full = self._workspace / rel
                    try:
                        mtime = full.stat().st_mtime
                    except OSError:
                        continue
                    current.add(rel)
                    prev = last_seen.get(rel)
                    if prev is None or mtime > prev:
                        modified.append(rel)
                        last_seen[rel] = mtime
                for rel in list(last_seen):
                    if rel not in current:
                        deleted.append(rel)
                        last_seen.pop(rel, None)
                if modified or deleted:
                    self._sync_burst(modified, deleted)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._print(f"[agent] poll loop error: {exc}")
            time.sleep(interval)


def run_agent(
    *,
    remote: str,
    token: Optional[str],
    workspace: str,
    workspace_id: Optional[str] = None,
    initial_sync: bool = True,
    excludes: Iterable[str] = (),
    debounce_ms: int = 800,
) -> None:
    """End-to-end agent entry point used by the CLI."""
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise NotADirectoryError(ws)

    client = AgentClient(
        remote=remote,
        token=token,
        workspace=ws,
        workspace_id=workspace_id,
        excludes=tuple(excludes),
        batch_max_files=_positive_int_from_env(
            "OMNICODE_AGENT_BATCH_MAX_FILES",
            100,
        ),
        batch_max_bytes=_positive_int_from_env(
            "OMNICODE_AGENT_BATCH_MAX_BYTES",
            1_000_000,
        ),
    )
    if not client.health():
        print(
            f"[agent] WARNING: cannot reach {remote}/health; "
            "the remote may not be running yet. Will keep retrying on push."
        )
    else:
        status = client.sync_status()
        print(
            f"[agent] connected to {remote} "
            f"(indexed_files={status.get('indexed_files', '?')}, "
            f"chunks={status.get('indexed_chunks', '?')})"
        )

    watcher = Watcher(client=client, workspace=ws, debounce_ms=debounce_ms)
    try:
        if initial_sync:
            watcher.initial_sync()
        watcher.run()
    finally:
        client.close()


__all__ = [
    "InitialWalkResult",
    "Watcher",
    "_initial_walk",
    "run_agent",
]
