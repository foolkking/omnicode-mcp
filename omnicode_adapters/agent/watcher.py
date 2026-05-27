"""Filesystem-watching wrapper around :class:`AgentClient`.

The watcher coalesces rapid bursts of file events (saving 8 files in
1.5 s should produce ONE upsert-batch HTTP call, not 8 sequential
calls) and feeds the results back to the user via simple stdout
prints.

Falls back to a polling loop if ``watchfiles`` isn't installed so the
agent still works on locked-down systems — albeit at a higher CPU cost.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from omnicode_adapters.agent.client import AgentClient, AgentResult, _is_excluded

logger = logging.getLogger(__name__)


def _initial_walk(workspace: Path, max_files: int = 5000) -> list[str]:
    """One-shot scan to seed the remote index on first connect."""
    out: list[str] = []
    for root, dirs, files in os.walk(workspace):
        rel_root = os.path.relpath(root, workspace).replace("\\", "/")
        # Prune ignored dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if not _is_excluded(
            (rel_root + "/" + d + "/").lstrip("./"), ()
        )]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace).replace(
                "\\", "/"
            )
            if _is_excluded(rel, ()):
                continue
            out.append(rel)
            if len(out) >= max_files:
                logger.warning(
                    "agent: initial walk hit the %d-file cap; later changes "
                    "will still sync via the watch loop.",
                    max_files,
                )
                return out
    return out


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
                # Outside the workspace — ignore.
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
        paths = _initial_walk(self._workspace)
        if not paths:
            return AgentResult()
        self._print(f"[agent] initial sync: {len(paths)} files…")
        result = self._client.push_batch(paths)
        self._print(
            f"[agent] initial sync done — pushed={result.pushed} "
            f"skipped={result.skipped} errors={len(result.errors)} "
            f"({result.elapsed_ms} ms)"
        )
        return result

    def run(self) -> None:
        """Block forever, reacting to filesystem events."""
        try:
            from watchfiles import watch  # type: ignore[import-not-found]
        except ImportError:
            self._print(
                "[agent] watchfiles not installed — install via "
                "`pip install omnicode-mcp[agent]` for live sync. "
                "Falling back to a 5s poll loop."
            )
            self._run_polling()
            return

        self._print(f"[agent] watching {self._workspace}…")
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
        """Best-effort fallback when watchfiles isn't installed."""
        last_seen: dict[str, float] = {}
        while True:
            try:
                modified: list[str] = []
                deleted: list[str] = []
                current: set[str] = set()
                for rel in _initial_walk(self._workspace):
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
    initial_sync: bool = True,
    excludes: Iterable[str] = (),
    debounce_ms: int = 800,
) -> None:
    """End-to-end agent entry point — used by the CLI."""
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise NotADirectoryError(ws)

    client = AgentClient(
        remote=remote,
        token=token,
        workspace=ws,
        excludes=tuple(excludes),
    )
    if not client.health():
        print(
            f"[agent] WARNING: cannot reach {remote}/health — "
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


__all__ = ["Watcher", "run_agent"]
