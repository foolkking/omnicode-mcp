"""Per-workspace FAISS / SQLite sharding (Wave 2 W2-10).

Multi-tenant deployments need each workspace to keep its index in a
separate directory so:

* Search results from workspace A can never leak into workspace B.
* Re-indexing one workspace doesn't churn another's FAISS file.
* Removing a workspace cleanly drops its disk footprint.

Layout:

```
<working_dir>/
  .data/
    shards/
      default/                     ← legacy single-tenant mounts
        vector_store.faiss
        vector_store.db
        file_tracker.db
        snapshots/
        edit_sessions/
      wk_<id>/                     ← one per registered workspace
        vector_store.faiss
        ...
```

A first-run **auto-migrate** moves the legacy files from
``<wd>/.data/`` into ``<wd>/.data/shards/default/`` if they exist and
the shards dir is empty. We don't delete anything on the original
path — we move; if the migration fails halfway the user can roll back
manually.

Helpers exposed:

* :func:`resolve_shard_dir(working_dir, shard_id)` — returns the
  directory the engine should mount, creating it on demand.
* :func:`auto_migrate_legacy(working_dir)` — idempotent first-run
  migration into ``shards/default``.
* :func:`drop_shard(working_dir, shard_id)` — recursive delete used
  by ``DELETE /workspaces/{id}``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# Files we move out of the legacy `<wd>/.data/` into the default shard
# during auto-migration. Anything else (e.g. providers.db on a really
# old install) stays where it is so users can keep using it.
_LEGACY_FILES: tuple[str, ...] = (
    "vector_store.faiss",
    "vector_store.db",
    "file_tracker.db",
    "selections.db",
)
_LEGACY_DIRS: tuple[str, ...] = (
    "snapshots",
    "edit_sessions",
)

DEFAULT_SHARD_ID = "default"


def _shards_root(working_dir: str | os.PathLike) -> Path:
    return Path(working_dir) / ".data" / "shards"


def resolve_shard_dir(
    working_dir: str | os.PathLike,
    shard_id: str = DEFAULT_SHARD_ID,
) -> str:
    """Return the absolute path of the shard directory, creating it
    plus the parent ``shards/`` root on first call."""
    shard_id = (shard_id or DEFAULT_SHARD_ID).strip() or DEFAULT_SHARD_ID
    root = _shards_root(working_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / shard_id
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def auto_migrate_legacy(working_dir: str | os.PathLike) -> dict:
    """Move legacy ``<wd>/.data/<file>`` artefacts into the default
    shard. Returns a small summary dict for logging.

    Idempotent: nothing happens when the default shard already has
    files OR when no legacy artefacts exist.
    """
    wd = Path(working_dir)
    legacy_dir = wd / ".data"
    if not legacy_dir.is_dir():
        return {"migrated_files": 0, "migrated_dirs": 0, "skipped": "no .data"}

    default_dir = _shards_root(wd) / DEFAULT_SHARD_ID
    default_dir.mkdir(parents=True, exist_ok=True)

    # If anything is already in the default shard we assume migration
    # was done in a previous session and bail out.
    if any(default_dir.iterdir()):
        return {
            "migrated_files": 0,
            "migrated_dirs": 0,
            "skipped": "default shard already populated",
        }

    moved_files = 0
    for name in _LEGACY_FILES:
        src = legacy_dir / name
        if src.is_file():
            shutil.move(str(src), str(default_dir / name))
            moved_files += 1

    moved_dirs = 0
    for name in _LEGACY_DIRS:
        src = legacy_dir / name
        if src.is_dir():
            # ``shutil.move`` of a directory that already exists at
            # the destination would raise; we already checked the
            # default shard is empty above so this is safe.
            shutil.move(str(src), str(default_dir / name))
            moved_dirs += 1

    if moved_files or moved_dirs:
        logger.info(
            "Sharding migration: moved %d files + %d dirs into %s",
            moved_files,
            moved_dirs,
            default_dir,
        )
    return {
        "migrated_files": moved_files,
        "migrated_dirs": moved_dirs,
        "default_shard_dir": str(default_dir),
    }


def drop_shard(
    working_dir: str | os.PathLike,
    shard_id: str,
) -> bool:
    """Recursively delete a shard directory. Refuses to drop the
    legacy default shard (id ``"default"``) because its contents may
    have been migrated from outside the registry."""
    if not shard_id or shard_id == DEFAULT_SHARD_ID:
        raise ValueError(
            "Refusing to drop the default shard via drop_shard(). "
            "Delete the workspace registry entry first."
        )
    target = _shards_root(working_dir) / shard_id
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    logger.info("Sharding: dropped shard at %s", target)
    return True


def list_shards(working_dir: str | os.PathLike) -> Iterable[str]:
    """List the shard ids present on disk under the working dir."""
    root = _shards_root(working_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


__all__ = [
    "DEFAULT_SHARD_ID",
    "resolve_shard_dir",
    "auto_migrate_legacy",
    "drop_shard",
    "list_shards",
]
