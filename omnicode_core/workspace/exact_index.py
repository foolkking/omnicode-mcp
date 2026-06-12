"""Lightweight exact symbol/text index for cloud snapshots.

This index is deliberately simpler than the semantic/vector store.  It is fed
directly from ``/sync/batch`` so exact symbol and literal text queries can be
served while the slower embedding index catches up.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    normalize_snapshot_path,
)


_PY_SYMBOL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>class|def|async\s+def)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class ExactSymbolRow:
    path: str
    name: str
    kind: str
    line_start: int
    line_end: int
    signature: str
    hash: str
    revision: int
    score: float
    why: str


@dataclass(frozen=True)
class ExactTextRow:
    path: str
    line_no: int
    line_text: str
    hash: str
    revision: int
    match_span: tuple[int, int]
    context_before: list[str]
    context_after: list[str]


def _norm_name(value: str) -> str:
    return (value or "").strip().lower()


def _patterns_match(path: str, patterns: Optional[list[str]]) -> bool:
    if not patterns:
        return True
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def _guess_language(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or "text"


def _line_fts_enabled() -> bool:
    raw = os.environ.get("OMNICODE_EXACT_LINE_FTS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _line_match(
    line: str,
    query: str,
    *,
    use_regex: bool,
    case_sensitive: bool,
) -> Optional[tuple[int, int]]:
    if use_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        match = re.search(query, line, flags=flags)
        return match.span() if match else None
    haystack = line if case_sensitive else line.lower()
    needle = query if case_sensitive else query.lower()
    start = haystack.find(needle)
    if start < 0:
        return None
    return start, start + len(needle)


class SnapshotExactIndex:
    """SQLite-backed exact index for one cloud snapshot store."""

    def __init__(self, *, store: Optional[CloudSnapshotStore] = None) -> None:
        self.store = store or CloudSnapshotStore()
        self._locks_guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}

    def _workspace_lock(self, workspace_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(workspace_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[workspace_id] = lock
            return lock

    def _db_path(self, workspace_id: str) -> Path:
        rel = normalize_snapshot_path(f"{workspace_id}/index-anchor").split("/")[0]
        return self.store.workspaces_root / rel / "exact_index.sqlite3"

    def _connect(self, workspace_id: str) -> sqlite3.Connection:
        path = self._db_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "path TEXT PRIMARY KEY, hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "language TEXT NOT NULL, size INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS symbols ("
            "path TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL, "
            "kind TEXT NOT NULL, line_start INTEGER NOT NULL, "
            "line_end INTEGER NOT NULL, signature TEXT NOT NULL, "
            "hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "PRIMARY KEY(path, name, line_start))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_name "
            "ON symbols(name_norm, kind, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lines ("
            "path TEXT NOT NULL, line_no INTEGER NOT NULL, line_text TEXT NOT NULL, "
            "hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "PRIMARY KEY(path, line_no))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_path ON lines(path)")
        if not _line_fts_enabled():
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES"
                "('line_fts_available', '0')"
            )
        else:
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS line_fts USING fts5("
                    "path UNINDEXED, line_no UNINDEXED, line_text, "
                    "hash UNINDEXED, revision UNINDEXED, tokenize='trigram')"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES"
                    "('line_fts_available', '1')"
                )
            except sqlite3.Error:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES"
                    "('line_fts_available', '0')"
                )
        conn.commit()

    def _fts_available(self, conn: sqlite3.Connection) -> bool:
        if not _line_fts_enabled():
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES"
                "('line_fts_available', '0')"
            )
            return False
        return self._meta_int(conn, "line_fts_available") == 1

    def _meta_int(self, conn: sqlite3.Connection, key: str) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def _set_meta_int(self, conn: sqlite3.Connection, key: str, value: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (key, str(int(value))),
        )

    def update_batch(
        self,
        *,
        workspace_id: str,
        changed_files: list[dict[str, Any]],
        deleted_paths: list[str],
        revision: int,
    ) -> int:
        """Update exact index rows for a sync batch and return indexed revision."""
        with self._workspace_lock(workspace_id):
            with self._connect(workspace_id) as conn:
                fts_available = self._fts_available(conn)
                for raw_path in deleted_paths:
                    path = normalize_snapshot_path(raw_path)
                    self._delete_path(conn, path, fts_available=fts_available)

                for item in changed_files:
                    path = normalize_snapshot_path(str(item["path"]))
                    content = str(item.get("content") or "")
                    hash_value = str(item.get("hash") or "")
                    size = int(
                        item.get("size")
                        or len(content.encode("utf-8", errors="replace"))
                    )
                    self._upsert_file(
                        conn,
                        path=path,
                        content=content,
                        hash_value=hash_value,
                        size=size,
                        revision=revision,
                        fts_available=fts_available,
                    )

                current = self._meta_int(conn, "exact_indexed_revision")
                self._set_meta_int(
                    conn,
                    "exact_indexed_revision",
                    max(current, revision),
                )
                conn.commit()
                return self._meta_int(conn, "exact_indexed_revision")

    def _delete_path(
        self,
        conn: sqlite3.Connection,
        path: str,
        *,
        fts_available: bool,
    ) -> None:
        conn.execute("DELETE FROM files WHERE path=?", (path,))
        conn.execute("DELETE FROM symbols WHERE path=?", (path,))
        conn.execute("DELETE FROM lines WHERE path=?", (path,))
        if fts_available:
            conn.execute("DELETE FROM line_fts WHERE path=?", (path,))

    def _upsert_file(
        self,
        conn: sqlite3.Connection,
        *,
        path: str,
        content: str,
        hash_value: str,
        size: int,
        revision: int,
        fts_available: bool,
    ) -> None:
        self._delete_path(conn, path, fts_available=fts_available)
        language = _guess_language(path)
        conn.execute(
            "INSERT OR REPLACE INTO files(path, hash, revision, language, size) "
            "VALUES(?, ?, ?, ?, ?)",
            (path, hash_value, int(revision), language, int(size)),
        )

        lines = content.splitlines()
        line_rows = [
            (path, idx + 1, line, hash_value, int(revision))
            for idx, line in enumerate(lines)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO lines(path, line_no, line_text, hash, revision) "
            "VALUES(?, ?, ?, ?, ?)",
            line_rows,
        )
        if fts_available and line_rows:
            conn.executemany(
                "INSERT INTO line_fts(path, line_no, line_text, hash, revision) "
                "VALUES(?, ?, ?, ?, ?)",
                line_rows,
            )

        symbol_rows = []
        for idx, line in enumerate(lines):
            match = _PY_SYMBOL_RE.match(line)
            if not match:
                continue
            raw_kind = match.group("kind")
            kind = "class" if raw_kind == "class" else "function"
            name = match.group("name")
            symbol_rows.append(
                (
                    path,
                    name,
                    _norm_name(name),
                    kind,
                    idx + 1,
                    idx + 1,
                    line.strip(),
                    hash_value,
                    int(revision),
                )
            )
        conn.executemany(
            "INSERT OR REPLACE INTO symbols("
            "path, name, name_norm, kind, line_start, line_end, signature, hash, revision"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            symbol_rows,
        )

    def status(self, *, workspace_id: str) -> dict[str, Any]:
        with self._workspace_lock(workspace_id):
            with self._connect(workspace_id) as conn:
                files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
                symbols = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
                lines = conn.execute("SELECT COUNT(*) AS n FROM lines").fetchone()["n"]
                return {
                    "workspace_id": workspace_id,
                    "exact_indexed_revision": self._meta_int(
                        conn,
                        "exact_indexed_revision",
                    ),
                    "files": int(files or 0),
                    "symbols": int(symbols or 0),
                    "lines": int(lines or 0),
                    "line_fts_available": self._fts_available(conn),
                }

    def search_symbols(
        self,
        *,
        workspace_id: str,
        query: str,
        symbol_type: Optional[str] = None,
        file_pattern: Optional[str] = None,
        fuzzy: bool = True,
        min_score: float = 0.5,
        max_results: int = 20,
    ) -> list[ExactSymbolRow]:
        if max_results <= 0:
            return []
        q = _norm_name(query)
        if not q:
            return []
        patterns = (
            [p.strip() for p in file_pattern.split(",") if p.strip()]
            if file_pattern
            else None
        )
        with self._workspace_lock(workspace_id):
            with self._connect(workspace_id) as conn:
                rows: list[ExactSymbolRow] = []
                seen: set[tuple[str, str, int]] = set()

                def add(raw: sqlite3.Row, score: float, why: str) -> None:
                    if symbol_type and raw["kind"] != symbol_type:
                        return
                    if not _patterns_match(str(raw["path"]), patterns):
                        return
                    if score < min_score:
                        return
                    key = (str(raw["path"]), str(raw["name"]), int(raw["line_start"]))
                    if key in seen:
                        return
                    seen.add(key)
                    rows.append(
                        ExactSymbolRow(
                            path=str(raw["path"]),
                            name=str(raw["name"]),
                            kind=str(raw["kind"]),
                            line_start=int(raw["line_start"]),
                            line_end=int(raw["line_end"]),
                            signature=str(raw["signature"]),
                            hash=str(raw["hash"]),
                            revision=int(raw["revision"]),
                            score=score,
                            why=why,
                        )
                    )

                for raw in conn.execute(
                    "SELECT * FROM symbols WHERE name_norm=? "
                    "ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, "
                    "CASE WHEN kind='class' THEN 0 ELSE 1 END, path LIMIT ?",
                    (q, query.strip(), max_results),
                ):
                    add(raw, 1.0, "symbol:exact")

                if rows:
                    return rows[:max_results]

                for raw in conn.execute(
                    "SELECT * FROM symbols WHERE name_norm LIKE ? "
                    "ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, "
                    "CASE WHEN kind='class' THEN 0 ELSE 1 END, name_norm, path LIMIT ?",
                    (q + "%", query.strip(), max_results * 4),
                ):
                    add(raw, 0.9, "symbol:prefix")
                    if len(rows) >= max_results:
                        return rows[:max_results]

                if fuzzy:
                    for raw in conn.execute(
                        "SELECT * FROM symbols WHERE name_norm LIKE ? "
                        "ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, "
                        "CASE WHEN kind='class' THEN 0 ELSE 1 END, name_norm, path LIMIT ?",
                        ("%" + q + "%", query.strip(), max_results * 8),
                    ):
                        add(raw, 0.7, "symbol:contains")
                        if len(rows) >= max_results:
                            return rows[:max_results]

                return rows[:max_results]

    def search_text(
        self,
        *,
        workspace_id: str,
        query: str,
        file_pattern: Optional[str] = None,
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
        context_lines: int = 2,
    ) -> list[ExactTextRow]:
        if max_results <= 0 or not query:
            return []
        patterns = (
            [p.strip() for p in file_pattern.split(",") if p.strip()]
            if file_pattern
            else None
        )
        with self._workspace_lock(workspace_id):
            with self._connect(workspace_id) as conn:
                table = "line_fts" if self._fts_available(conn) and not use_regex else "lines"
                sql = (
                    f"SELECT path, line_no, line_text, hash, revision FROM {table} "
                    "WHERE line_text LIKE ? LIMIT ?"
                    if not use_regex
                    else "SELECT path, line_no, line_text, hash, revision FROM lines LIMIT ?"
                )
                params: tuple[Any, ...] = (
                    ("%" + query + "%", max(max_results * 20, 500))
                    if not use_regex
                    else (max(max_results * 200, 5000),)
                )
                out: list[ExactTextRow] = []
                for raw in conn.execute(sql, params):
                    path = str(raw["path"])
                    if not _patterns_match(path, patterns):
                        continue
                    line_text = str(raw["line_text"])
                    span = _line_match(
                        line_text,
                        query,
                        use_regex=use_regex,
                        case_sensitive=case_sensitive,
                    )
                    if span is None:
                        continue
                    line_no = int(raw["line_no"])
                    before, after = self._line_context(
                        conn,
                        path=path,
                        line_no=line_no,
                        context_lines=context_lines,
                    )
                    out.append(
                        ExactTextRow(
                            path=path,
                            line_no=line_no,
                            line_text=line_text,
                            hash=str(raw["hash"]),
                            revision=int(raw["revision"]),
                            match_span=span,
                            context_before=before,
                            context_after=after,
                        )
                    )
                    if len(out) >= max_results:
                        break
                return out

    def _line_context(
        self,
        conn: sqlite3.Connection,
        *,
        path: str,
        line_no: int,
        context_lines: int,
    ) -> tuple[list[str], list[str]]:
        if context_lines <= 0:
            return [], []
        start = max(1, line_no - context_lines)
        end = line_no + context_lines
        rows = conn.execute(
            "SELECT line_no, line_text FROM lines WHERE path=? "
            "AND line_no BETWEEN ? AND ? ORDER BY line_no",
            (path, start, end),
        ).fetchall()
        before = [str(row["line_text"]) for row in rows if int(row["line_no"]) < line_no]
        after = [str(row["line_text"]) for row in rows if int(row["line_no"]) > line_no]
        return before, after


__all__ = [
    "ExactSymbolRow",
    "ExactTextRow",
    "SnapshotExactIndex",
]
