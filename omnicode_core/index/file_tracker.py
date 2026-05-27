"""
Incremental file tracker — detects which files changed since last index.

Maintains a SQLite table of (file_path, mtime, size, content_hash) and
compares against the filesystem to produce a changeset:
  - new files (need full indexing)
  - modified files (need re-indexing)
  - deleted files (need removal from index)
  - unchanged files (skip)

This is the key to making `omnicode index` go from 30-60s → 2-3s on
typical projects where only a few files changed between runs.
"""

import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Extensions we index (same set as engine.py)
INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".cpp", ".h", ".cc", ".c", ".cxx", ".hpp", ".hh",
    ".java", ".go", ".rs",
}

# Directories to always skip
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "build", "dist", ".data", ".codebase", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", "env", ".tox", ".eggs",
}


@dataclass
class FileChange:
    """Represents a detected change in the filesystem."""
    path: str           # relative to working_dir
    change_type: str    # "new" | "modified" | "deleted"
    mtime: float = 0.0
    size: int = 0
    content_hash: str = ""


class FileTracker:
    """Tracks file state for incremental indexing.

    Stores file metadata in a SQLite table inside .data/ and compares
    against the live filesystem to produce a minimal changeset.
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_state (
                file_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                language TEXT,
                last_indexed_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def detect_changes(self, working_dir: str) -> List[FileChange]:
        """Compare filesystem against stored state.

        Returns a list of FileChange objects describing what needs to be
        re-indexed.  Unchanged files are NOT included in the output.
        """
        working_dir = os.path.abspath(working_dir)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Load stored state
        cursor = conn.execute("SELECT file_path, mtime, size, content_hash FROM file_state")
        stored: Dict[str, Tuple[float, int, str]] = {}
        for row in cursor.fetchall():
            stored[row["file_path"]] = (row["mtime"], row["size"], row["content_hash"])

        # Scan filesystem
        live_files: Set[str] = set()
        changes: List[FileChange] = []

        for root, dirs, files in os.walk(working_dir):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in INDEXABLE_EXTENSIONS:
                    continue

                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, working_dir)
                # Normalize to forward slashes for cross-platform consistency
                rel_path = rel_path.replace("\\", "/")
                live_files.add(rel_path)

                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue

                mtime = stat.st_mtime
                size = stat.st_size

                if rel_path not in stored:
                    # New file
                    content_hash = self._hash_file(full_path)
                    changes.append(FileChange(
                        path=rel_path,
                        change_type="new",
                        mtime=mtime,
                        size=size,
                        content_hash=content_hash,
                    ))
                else:
                    old_mtime, old_size, old_hash = stored[rel_path]
                    # Quick check: if mtime and size unchanged, skip hash
                    if mtime == old_mtime and size == old_size:
                        continue
                    # Size or mtime changed — compute hash to confirm
                    content_hash = self._hash_file(full_path)
                    if content_hash == old_hash:
                        # Content identical despite mtime change (e.g. touch)
                        # Update mtime in DB but don't re-index
                        conn.execute(
                            "UPDATE file_state SET mtime = ? WHERE file_path = ?",
                            (mtime, rel_path),
                        )
                        continue
                    # Truly modified
                    changes.append(FileChange(
                        path=rel_path,
                        change_type="modified",
                        mtime=mtime,
                        size=size,
                        content_hash=content_hash,
                    ))

        # Detect deletions
        for stored_path in stored:
            if stored_path not in live_files:
                changes.append(FileChange(
                    path=stored_path,
                    change_type="deleted",
                ))

        conn.commit()
        conn.close()

        return changes

    def mark_indexed(self, working_dir: str, file_path: str, content_hash: str = ""):
        """Record that a file has been successfully indexed.

        Called after update_file() succeeds so the next detect_changes()
        knows this file is up to date.
        """
        full_path = os.path.join(os.path.abspath(working_dir), file_path)
        try:
            stat = os.stat(full_path)
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            return

        if not content_hash:
            content_hash = self._hash_file(full_path)

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO file_state
                (file_path, mtime, size, content_hash, language, last_indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (file_path.replace("\\", "/"), mtime, size, content_hash, ext, now))
        conn.commit()
        conn.close()

    def mark_deleted(self, file_path: str):
        """Remove a file from the tracker (after its chunks are deleted)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM file_state WHERE file_path = ?",
                     (file_path.replace("\\", "/"),))
        conn.commit()
        conn.close()

    def get_stats(self) -> dict:
        """Return tracker statistics."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT COUNT(*) FROM file_state")
        total = cursor.fetchone()[0]
        cursor = conn.execute("SELECT MAX(last_indexed_at) FROM file_state")
        last = cursor.fetchone()[0]
        conn.close()
        return {"tracked_files": total, "last_indexed": last}

    def clear(self):
        """Clear all tracking state (for force rebuild)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM file_state")
        conn.commit()
        conn.close()

    @staticmethod
    def _hash_file(path: str) -> str:
        """Compute SHA-256 of file content (first 512KB for speed on huge files)."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                # Read first 512KB — enough to detect meaningful changes
                # without spending time on multi-MB generated files.
                data = f.read(512 * 1024)
                h.update(data)
                if len(data) == 512 * 1024:
                    # For large files, also hash the last 64KB to catch
                    # appended content.
                    f.seek(-65536, 2)
                    h.update(f.read())
        except OSError:
            return ""
        return h.hexdigest()
