"""Persistent, revision-aware call graph for local and synced workspaces.

The graph index is intentionally independent from semantic/vector indexing.
It stores deterministic AST call edges in SQLite and can be updated per file,
so graph failures never block exact search, sync acceptance, or safe editing.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from omnicode.ast_engine.graph import CallGraphBuilder
from omnicode.ast_engine.inheritance import InheritanceGraphBuilder
from omnicode.ast_engine.parser import UnifiedASTParser
from omnicode_core.workspace.snapshot_store import (
    CloudSnapshotStore,
    normalize_snapshot_path,
)

_SCHEMA_VERSION = 3
_INDEX_KIND = "workspace_call_graph"


class WorkspaceGraphIndex:
    """SQLite-backed call graph keyed by workspace and snapshot revision."""

    def __init__(
        self,
        *,
        store: Optional[CloudSnapshotStore] = None,
        parser: Optional[UnifiedASTParser] = None,
    ) -> None:
        self.store = store or CloudSnapshotStore()
        self.parser = parser or UnifiedASTParser()
        self.builder = CallGraphBuilder(self.parser)
        self.inheritance_builder = InheritanceGraphBuilder(self.parser)
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
        rel = normalize_snapshot_path(f"{workspace_id}/graph-anchor").split("/")[0]
        return self.store.workspaces_root / rel / "graph_index.sqlite3"

    def _connect(self, workspace_id: str) -> sqlite3.Connection:
        path = self._db_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-100000")
        self._ensure_schema(conn)
        return conn

    def _connect_readonly(
        self,
        workspace_id: str,
        *,
        timeout_s: float = 0.1,
    ) -> sqlite3.Connection:
        path = self._db_path(workspace_id)
        if not path.exists():
            raise FileNotFoundError(str(path))
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=max(timeout_s, 0.001))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS graph_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "path TEXT PRIMARY KEY, hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "language TEXT NOT NULL, supported INTEGER NOT NULL, "
            "edge_count INTEGER NOT NULL, parse_error TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edges ("
            "path TEXT NOT NULL, caller TEXT NOT NULL, callee TEXT NOT NULL, "
            "line INTEGER NOT NULL, language TEXT NOT NULL, hash TEXT NOT NULL, "
            "revision INTEGER NOT NULL, source_provider TEXT NOT NULL DEFAULT 'ast', "
            "confidence REAL NOT NULL DEFAULT 0.8, "
            "PRIMARY KEY(path, caller, callee, line))"
        )
        self._ensure_column(
            conn,
            "edges",
            "source_provider",
            "TEXT NOT NULL DEFAULT 'ast'",
        )
        self._ensure_column(
            conn,
            "edges",
            "confidence",
            "REAL NOT NULL DEFAULT 0.8",
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS call_edges ("
            "path TEXT NOT NULL, caller TEXT NOT NULL, callee TEXT NOT NULL, "
            "line INTEGER NOT NULL, language TEXT NOT NULL, hash TEXT NOT NULL, "
            "revision INTEGER NOT NULL, source_provider TEXT NOT NULL, "
            "confidence REAL NOT NULL, "
            "PRIMARY KEY(path, caller, callee, line, source_provider))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_edges_caller "
            "ON call_edges(caller, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_edges_callee "
            "ON call_edges(callee, path)"
        )
        conn.execute(
            'CREATE TABLE IF NOT EXISTS "references" ('
            "path TEXT NOT NULL, symbol TEXT NOT NULL, line INTEGER NOT NULL, "
            "context TEXT NOT NULL, language TEXT NOT NULL, hash TEXT NOT NULL, "
            "revision INTEGER NOT NULL, source_provider TEXT NOT NULL, "
            "confidence REAL NOT NULL, "
            "PRIMARY KEY(path, symbol, line, source_provider))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_references_symbol "
            'ON "references"(symbol, path)'
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS inheritance_edges ("
            "path TEXT NOT NULL, child TEXT NOT NULL, parent TEXT NOT NULL, "
            "edge_type TEXT NOT NULL, line INTEGER NOT NULL, "
            "language TEXT NOT NULL, hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "source_provider TEXT NOT NULL, confidence REAL NOT NULL, "
            "PRIMARY KEY(path, child, parent, edge_type, line, source_provider))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inheritance_child "
            "ON inheritance_edges(child, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inheritance_parent "
            "ON inheritance_edges(parent, path)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS import_edges ("
            "path TEXT NOT NULL, importer TEXT NOT NULL, imported TEXT NOT NULL, "
            "line INTEGER NOT NULL, language TEXT NOT NULL, hash TEXT NOT NULL, "
            "revision INTEGER NOT NULL, source_provider TEXT NOT NULL, "
            "confidence REAL NOT NULL, "
            "PRIMARY KEY(path, imported, line, source_provider))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_import_edges_imported "
            "ON import_edges(imported, path)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_edges ("
            "symbol TEXT NOT NULL, test_path TEXT NOT NULL, line INTEGER NOT NULL, "
            "language TEXT NOT NULL, revision INTEGER NOT NULL, "
            "source_provider TEXT NOT NULL, confidence REAL NOT NULL, "
            "PRIMARY KEY(symbol, test_path, line, source_provider))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_test_edges_symbol "
            "ON test_edges(symbol, test_path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_caller "
            "ON edges(caller, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_callee "
            "ON edges(callee, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_path ON edges(path)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS definitions ("
            "path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, "
            "parent TEXT, qualified_name TEXT NOT NULL, "
            "line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, "
            "language TEXT NOT NULL, hash TEXT NOT NULL, revision INTEGER NOT NULL, "
            "source_provider TEXT NOT NULL DEFAULT 'ast', "
            "confidence REAL NOT NULL DEFAULT 0.9, "
            "PRIMARY KEY(path, qualified_name, line_start))"
        )
        self._ensure_column(
            conn,
            "definitions",
            "source_provider",
            "TEXT NOT NULL DEFAULT 'ast'",
        )
        self._ensure_column(
            conn,
            "definitions",
            "confidence",
            "REAL NOT NULL DEFAULT 0.9",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_definitions_name "
            "ON definitions(name, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_definitions_parent "
            "ON definitions(parent, path)"
        )
        self._set_meta(conn, "schema_version", str(_SCHEMA_VERSION))
        self._set_meta(conn, "index_kind", _INDEX_KIND)
        if self._meta(conn, "graph_indexed_revision") is None:
            self._set_meta(conn, "graph_indexed_revision", "0")
        if self._meta(conn, "last_error") is None:
            self._set_meta(conn, "last_error", "")
        if self._meta(conn, "coverage_complete") is None:
            self._set_meta(conn, "coverage_complete", "0")
        conn.commit()

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @staticmethod
    def _meta(
        conn: sqlite3.Connection,
        key: str,
        default: Optional[str] = None,
    ) -> Optional[str]:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (key, str(value)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES(?, ?)",
            (key, str(value)),
        )

    @classmethod
    def _meta_int(cls, conn: sqlite3.Connection, key: str) -> int:
        try:
            return int(cls._meta(conn, key, "0") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _language_for_path(path: str) -> Optional[str]:
        suffix = Path(path).suffix.lower()
        if suffix in {".scala", ".sc"}:
            return "scala"
        return CallGraphBuilder.EXT_LANG_MAP.get(suffix)

    def clear_workspace(self, *, workspace_id: str) -> None:
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                conn.execute("DELETE FROM edges")
                conn.execute("DELETE FROM call_edges")
                conn.execute('DELETE FROM "references"')
                conn.execute("DELETE FROM inheritance_edges")
                conn.execute("DELETE FROM import_edges")
                conn.execute("DELETE FROM test_edges")
                conn.execute("DELETE FROM definitions")
                conn.execute("DELETE FROM files")
                self._set_meta(conn, "graph_indexed_revision", "0")
                self._set_meta(conn, "last_error", "")
                self._set_meta(conn, "coverage_complete", "0")
                conn.commit()
            finally:
                conn.close()

    def record_error(self, *, workspace_id: str, error: str) -> None:
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                self._set_meta(conn, "last_error", str(error))
                conn.commit()
            finally:
                conn.close()

    def update_batch(
        self,
        *,
        workspace_id: str,
        changed_files: list[dict[str, Any]],
        deleted_paths: list[str],
        revision: int,
        coverage_complete: Optional[bool] = True,
    ) -> int:
        """Replace graph rows for changed paths and advance graph revision."""
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                for raw_path in deleted_paths:
                    self._delete_path(conn, normalize_snapshot_path(raw_path))

                for item in changed_files:
                    path = normalize_snapshot_path(str(item["path"]))
                    content = str(item.get("content") or "")
                    hash_value = str(item.get("hash") or "")
                    self._upsert_file(
                        conn,
                        path=path,
                        content=content,
                        hash_value=hash_value,
                        revision=revision,
                    )

                current = self._meta_int(conn, "graph_indexed_revision")
                self._set_meta(
                    conn,
                    "graph_indexed_revision",
                    str(max(current, int(revision))),
                )
                self._set_meta(conn, "last_error", "")
                if coverage_complete is not None:
                    self._set_meta(
                        conn,
                        "coverage_complete",
                        "1" if coverage_complete else "0",
                    )
                conn.commit()
                return self._meta_int(conn, "graph_indexed_revision")
            finally:
                conn.close()

    @staticmethod
    def _delete_path(conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM edges WHERE path=?", (path,))
        conn.execute("DELETE FROM call_edges WHERE path=?", (path,))
        conn.execute('DELETE FROM "references" WHERE path=?', (path,))
        conn.execute("DELETE FROM inheritance_edges WHERE path=?", (path,))
        conn.execute("DELETE FROM import_edges WHERE path=?", (path,))
        conn.execute("DELETE FROM test_edges WHERE test_path=?", (path,))
        conn.execute("DELETE FROM definitions WHERE path=?", (path,))
        conn.execute("DELETE FROM files WHERE path=?", (path,))

    @staticmethod
    def _is_test_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        name = Path(normalized).name
        return (
            "/test/" in f"/{normalized}/"
            or "/tests/" in f"/{normalized}/"
            or "/src/test/" in f"/{normalized}/"
            or name.startswith("test_")
            or name.endswith(
                (
                    "_test.py",
                    "_tests.py",
                    "test.java",
                    "tests.java",
                    "test.scala",
                    "tests.scala",
                    "spec.scala",
                )
            )
        )

    @staticmethod
    def _line_context(content: str, line: int) -> str:
        lines = content.splitlines()
        if 1 <= int(line) <= len(lines):
            return lines[int(line) - 1].strip()[:500]
        return ""

    @staticmethod
    def _extract_scala_graph(
        content: str,
    ) -> tuple[
        list[tuple[str, str, int]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[tuple[str, str, str, int]],
    ]:
        """Coarse Scala parser used only when Metals/tree-sitter is absent."""

        calls: list[tuple[str, str, int]] = []
        definitions: list[dict[str, Any]] = []
        imports: list[dict[str, Any]] = []
        inheritance: list[tuple[str, str, str, int]] = []
        current_callable: Optional[str] = None
        declaration_re = re.compile(
            r"\b(class|trait|object|enum|def)\s+([A-Za-z_$][\w$]*)"
        )
        call_re = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
        ignored_calls = {
            "if",
            "for",
            "while",
            "match",
            "catch",
            "new",
            "this",
            "super",
        }
        for line_no, raw_line in enumerate(content.splitlines(), 1):
            line = raw_line.split("//", 1)[0]
            stripped = line.strip()
            if stripped.startswith("import "):
                module = stripped[len("import ") :].strip()
                imports.append({
                    "module": module,
                    "raw": stripped,
                    "line": line_no,
                    "language": "scala",
                })
            declaration = declaration_re.search(line)
            if declaration:
                kind, name = declaration.groups()
                parent = None
                definitions.append({
                    "name": name,
                    "type": "method" if kind == "def" else kind,
                    "line_start": line_no,
                    "line_end": line_no,
                    "parent": parent,
                    "language": "scala",
                })
                if kind == "def":
                    current_callable = name
                if kind in {"class", "trait", "object"}:
                    extends = re.search(
                        rf"\b{re.escape(name)}\b.*?\bextends\s+"
                        r"([A-Za-z_$][\w.$]*)"
                        r"(?P<withs>(?:\s+with\s+[A-Za-z_$][\w.$]*)*)",
                        line,
                    )
                    if extends:
                        inheritance.append(
                            (name, extends.group(1), "extends", line_no)
                        )
                        for base in re.findall(
                            r"\bwith\s+([A-Za-z_$][\w.$]*)",
                            extends.group("withs") or "",
                        ):
                            inheritance.append(
                                (name, base, "with", line_no)
                            )
            if current_callable:
                for callee in call_re.findall(line):
                    if callee in ignored_calls or callee == current_callable:
                        continue
                    calls.append((current_callable, callee, line_no))
        return calls, definitions, imports, inheritance

    def _upsert_file(
        self,
        conn: sqlite3.Connection,
        *,
        path: str,
        content: str,
        hash_value: str,
        revision: int,
    ) -> None:
        self._delete_path(conn, path)
        language = self._language_for_path(path)
        if language is None:
            conn.execute(
                "INSERT INTO files(path, hash, revision, language, supported, "
                "edge_count, parse_error) VALUES(?, ?, ?, ?, 0, 0, NULL)",
                (path, hash_value, int(revision), Path(path).suffix.lower().lstrip(".")),
            )
            return

        parse_error: Optional[str] = None
        edges: list[tuple[str, str, int]] = []
        definitions: list[dict[str, Any]] = []
        imports: list[dict[str, Any]] = []
        inheritance: list[tuple[str, str, str, int]] = []
        source_provider = "tree_sitter_ast"
        confidence = 0.85
        try:
            if language == "scala":
                (
                    edges,
                    definitions,
                    imports,
                    inheritance,
                ) = self._extract_scala_graph(content)
                source_provider = "scala_lexical_fallback"
                confidence = 0.45
            else:
                edges = list(self.parser.extract_calls(content, language))
                definitions = list(
                    self.parser.extract_symbols(content, language)
                )
                imports = list(
                    self.parser.extract_imports(content, language)
                )
                inheritance_graph = (
                    self.inheritance_builder.build_for_content(
                        content,
                        language,
                        path,
                    )
                )
                inheritance = [
                    (
                        edge.subclass,
                        edge.base,
                        edge.kind,
                        int(edge.line or 0),
                    )
                    for edge in inheritance_graph.edges
                ]
        except Exception as exc:  # parser support is capability data, not sync failure
            parse_error = f"{exc.__class__.__name__}: {exc}"

        conn.execute(
            "INSERT INTO files(path, hash, revision, language, supported, "
            "edge_count, parse_error) VALUES(?, ?, ?, ?, 1, ?, ?)",
            (
                path,
                hash_value,
                int(revision),
                language,
                len(edges),
                parse_error,
            ),
        )
        if edges:
            conn.executemany(
                "INSERT OR REPLACE INTO edges("
                "path, caller, callee, line, language, hash, revision, "
                "source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        str(caller),
                        str(callee),
                        int(line),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                    for caller, callee, line in edges
                    if caller and callee
                ],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO call_edges("
                "path, caller, callee, line, language, hash, revision, "
                "source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        str(caller),
                        str(callee),
                        int(line),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                    for caller, callee, line in edges
                    if caller and callee
                ],
            )
            conn.executemany(
                'INSERT OR REPLACE INTO "references"('
                "path, symbol, line, context, language, hash, revision, "
                "source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        str(callee),
                        int(line),
                        self._line_context(content, int(line)),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                    for _caller, callee, line in edges
                    if callee
                ],
            )
            if self._is_test_path(path):
                conn.executemany(
                    "INSERT OR REPLACE INTO test_edges("
                    "symbol, test_path, line, language, revision, "
                    "source_provider, confidence"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            str(callee),
                            path,
                            int(line),
                            language,
                            int(revision),
                            source_provider,
                            confidence,
                        )
                        for _caller, callee, line in edges
                        if callee
                    ],
                )
        if imports:
            conn.executemany(
                "INSERT OR REPLACE INTO import_edges("
                "path, importer, imported, line, language, hash, revision, "
                "source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        path,
                        str(item.get("module") or item.get("raw") or ""),
                        int(item.get("line") or 1),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                    for item in imports
                    if item.get("module") or item.get("raw")
                ],
            )
        if inheritance:
            conn.executemany(
                "INSERT OR REPLACE INTO inheritance_edges("
                "path, child, parent, edge_type, line, language, hash, revision, "
                "source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        child,
                        parent,
                        edge_type,
                        int(line),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                    for child, parent, edge_type, line in inheritance
                    if child and parent
                ],
            )
        if definitions:
            rows = []
            for item in definitions:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                parent = str(item.get("parent") or "").strip() or None
                qualified_name = f"{parent}.{name}" if parent else name
                rows.append(
                    (
                        path,
                        name,
                        str(item.get("type") or "symbol"),
                        parent,
                        qualified_name,
                        int(item.get("line_start") or 1),
                        int(item.get("line_end") or item.get("line_start") or 1),
                        language,
                        hash_value,
                        int(revision),
                        source_provider,
                        confidence,
                    )
                )
            conn.executemany(
                "INSERT OR REPLACE INTO definitions("
                "path, name, kind, parent, qualified_name, line_start, line_end, "
                "language, hash, revision, source_provider, confidence"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def index_workspace_root(
        self,
        *,
        workspace_id: str,
        root: str | os.PathLike[str],
        revision: int = 1,
        force: bool = False,
        max_file_bytes: int = 2_000_000,
        batch_size: int = 250,
    ) -> dict[str, Any]:
        """Build a graph index from a local checkout without writing into it."""
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"workspace root not found: {root_path}")
        if force:
            self.clear_workspace(workspace_id=workspace_id)

        skip_dirs = set(CallGraphBuilder.SKIP_DIRS) | {
            ".data",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".gradle",
            ".idea",
            ".vscode",
        }
        files: list[dict[str, Any]] = []
        scanned = 0
        skipped = 0

        def flush() -> None:
            nonlocal files
            if not files:
                return
            self.update_batch(
                workspace_id=workspace_id,
                changed_files=files,
                deleted_paths=[],
                revision=revision,
                coverage_complete=None,
            )
            files = []

        for current_root, dir_names, file_names in os.walk(root_path):
            dir_names[:] = [name for name in dir_names if name not in skip_dirs]
            current = Path(current_root)
            for file_name in file_names:
                path = current / file_name
                language = self._language_for_path(path.as_posix())
                if language is None:
                    skipped += 1
                    continue
                try:
                    if path.stat().st_size > max_file_bytes:
                        skipped += 1
                        continue
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    skipped += 1
                    continue
                scanned += 1
                files.append(
                    {
                        "path": path.relative_to(root_path).as_posix(),
                        "content": content,
                        "hash": "",
                    }
                )
                if len(files) >= batch_size:
                    flush()
        flush()
        self._set_coverage_complete(
            workspace_id=workspace_id,
            revision=revision,
        )
        return {
            "workspace_id": workspace_id,
            "root": str(root_path),
            "revision": int(revision),
            "files_scanned": scanned,
            "files_skipped": skipped,
            "status": self.status(
                workspace_id=workspace_id,
                accepted_revision=revision,
            ),
        }

    def upsert_lsp_references(
        self,
        *,
        workspace_id: str,
        symbol: str,
        references: list[dict[str, Any]],
        revision: int,
        language: str,
        provider: str,
        confidence: float = 0.98,
    ) -> int:
        """Persist high-confidence LSP references without replacing AST rows."""

        clean_symbol = (symbol or "").strip()
        if not clean_symbol:
            return 0
        rows: list[tuple[Any, ...]] = []
        test_rows: list[tuple[Any, ...]] = []
        for item in references:
            raw_path = item.get("file") or item.get("path")
            if not raw_path:
                continue
            try:
                path = normalize_snapshot_path(str(raw_path))
            except ValueError:
                continue
            line = int(item.get("line") or 0)
            context = str(item.get("context") or item.get("snippet") or "")
            rows.append(
                (
                    path,
                    clean_symbol,
                    line,
                    context[:500],
                    language,
                    str(item.get("hash") or ""),
                    int(revision),
                    provider,
                    float(confidence),
                )
            )
            if self._is_test_path(path):
                test_rows.append(
                    (
                        clean_symbol,
                        path,
                        line,
                        language,
                        int(revision),
                        provider,
                        float(confidence),
                    )
                )
        if not rows:
            return 0
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                conn.execute(
                    'DELETE FROM "references" WHERE symbol=? '
                    "AND source_provider=?",
                    (clean_symbol, provider),
                )
                conn.execute(
                    "DELETE FROM test_edges WHERE symbol=? "
                    "AND source_provider=?",
                    (clean_symbol, provider),
                )
                conn.executemany(
                    'INSERT OR REPLACE INTO "references"('
                    "path, symbol, line, context, language, hash, revision, "
                    "source_provider, confidence"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                if test_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO test_edges("
                        "symbol, test_path, line, language, revision, "
                        "source_provider, confidence"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                        test_rows,
                    )
                conn.commit()
                return len(rows)
            finally:
                conn.close()

    def index_snapshot_store(
        self,
        *,
        workspace_id: str,
        revision: int,
        force: bool = True,
        batch_size: int = 250,
    ) -> dict[str, Any]:
        """Bootstrap complete graph coverage from content-addressed snapshots."""
        if force:
            self.clear_workspace(workspace_id=workspace_id)
        records = self.store.list_records(workspace_id=workspace_id)
        files: list[dict[str, Any]] = []
        indexed = 0
        unreadable = 0

        def flush() -> None:
            nonlocal files
            if not files:
                return
            self.update_batch(
                workspace_id=workspace_id,
                changed_files=files,
                deleted_paths=[],
                revision=revision,
                coverage_complete=None,
            )
            files = []

        for record in records:
            content = self.store.read_record_text(
                workspace_id=workspace_id,
                record=record,
            )
            if content is None:
                unreadable += 1
                continue
            files.append(
                {
                    "path": record.path,
                    "content": content,
                    "hash": record.hash,
                }
            )
            indexed += 1
            if len(files) >= batch_size:
                flush()
        flush()
        self._set_coverage_complete(
            workspace_id=workspace_id,
            revision=revision,
        )
        return {
            "workspace_id": workspace_id,
            "revision": int(revision),
            "records_seen": len(records),
            "files_indexed": indexed,
            "files_unreadable": unreadable,
            "status": self.status(
                workspace_id=workspace_id,
                accepted_revision=revision,
            ),
        }

    def _set_coverage_complete(
        self,
        *,
        workspace_id: str,
        revision: int,
    ) -> None:
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                self._set_meta(conn, "coverage_complete", "1")
                current = self._meta_int(conn, "graph_indexed_revision")
                self._set_meta(
                    conn,
                    "graph_indexed_revision",
                    str(max(current, int(revision))),
                )
                self._set_meta(conn, "last_error", "")
                conn.commit()
            finally:
                conn.close()

    def status(
        self,
        *,
        workspace_id: str,
        accepted_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                file_row = conn.execute(
                    "SELECT COUNT(*) AS files, "
                    "SUM(CASE WHEN supported=1 THEN 1 ELSE 0 END) AS supported, "
                    "SUM(CASE WHEN supported=0 THEN 1 ELSE 0 END) AS unsupported, "
                    "SUM(CASE WHEN parse_error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
                    "FROM files"
                ).fetchone()
                edge_row = conn.execute(
                    "SELECT COUNT(*) AS edges, COUNT(DISTINCT caller) AS callers, "
                    "COUNT(DISTINCT callee) AS callees FROM edges"
                ).fetchone()
                definition_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM definitions"
                ).fetchone()["n"]
                reference_count = conn.execute(
                    'SELECT COUNT(*) AS n FROM "references"'
                ).fetchone()["n"]
                inheritance_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM inheritance_edges"
                ).fetchone()["n"]
                import_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM import_edges"
                ).fetchone()["n"]
                test_edge_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM test_edges"
                ).fetchone()["n"]
                languages = [
                    str(row["language"])
                    for row in conn.execute(
                        "SELECT DISTINCT language FROM files "
                        "WHERE supported=1 ORDER BY language"
                    )
                ]
                indexed_revision = self._meta_int(conn, "graph_indexed_revision")
                last_error = self._meta(conn, "last_error", "") or None
                current = (
                    accepted_revision is None
                    or indexed_revision >= int(accepted_revision)
                )
                needs_rebuild = bool(
                    int(file_row["supported"] or 0) > 0
                    and int(edge_row["edges"] or 0) > 0
                    and int(definition_count or 0) == 0
                )
                coverage_complete = (
                    self._meta_int(conn, "coverage_complete") == 1
                )
                ready = bool(
                    current
                    and not last_error
                    and not needs_rebuild
                    and coverage_complete
                    and int(file_row["supported"] or 0) > 0
                )
                return {
                    "workspace_id": workspace_id,
                    "schema_version": self._meta_int(conn, "schema_version"),
                    "index_kind": self._meta(
                        conn,
                        "index_kind",
                        _INDEX_KIND,
                    ),
                    "graph_indexed_revision": indexed_revision,
                    "accepted_revision": accepted_revision,
                    "pending_revisions": (
                        max(int(accepted_revision) - indexed_revision, 0)
                        if accepted_revision is not None
                        else 0
                    ),
                    "ready": ready,
                    "current": current,
                    "files": int(file_row["files"] or 0),
                    "supported_files": int(file_row["supported"] or 0),
                    "unsupported_files": int(file_row["unsupported"] or 0),
                    "parse_error_files": int(file_row["errors"] or 0),
                    "edges": int(edge_row["edges"] or 0),
                    "definitions": int(definition_count or 0),
                    "references": int(reference_count or 0),
                    "inheritance_edges": int(inheritance_count or 0),
                    "import_edges": int(import_count or 0),
                    "test_edges": int(test_edge_count or 0),
                    "callers": int(edge_row["callers"] or 0),
                    "callees": int(edge_row["callees"] or 0),
                    "languages": languages,
                    "last_error": last_error,
                    "needs_rebuild": needs_rebuild,
                    "coverage_complete": coverage_complete,
                }
            finally:
                conn.close()

    def try_status(
        self,
        *,
        workspace_id: str,
        accepted_revision: Optional[int] = None,
        lock_timeout_ms: int = 50,
    ) -> dict[str, Any]:
        """Return status without waiting behind a long graph index write."""
        lock = self._workspace_lock(workspace_id)
        acquired = lock.acquire(timeout=max(lock_timeout_ms, 0) / 1000.0)
        if not acquired:
            return {
                "workspace_id": workspace_id,
                "ready": False,
                "current": False,
                "busy": True,
                "last_error": "graph_index_busy",
                "accepted_revision": accepted_revision,
                "pending_revisions": None,
            }
        try:
            conn = self._connect_readonly(
                workspace_id,
                timeout_s=max(lock_timeout_ms, 1) / 1000.0,
            )
            try:
                file_row = conn.execute(
                    "SELECT COUNT(*) AS files, "
                    "SUM(CASE WHEN supported=1 THEN 1 ELSE 0 END) AS supported, "
                    "SUM(CASE WHEN supported=0 THEN 1 ELSE 0 END) AS unsupported, "
                    "SUM(CASE WHEN parse_error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
                    "FROM files"
                ).fetchone()
                edge_row = conn.execute(
                    "SELECT COUNT(*) AS edges, COUNT(DISTINCT caller) AS callers, "
                    "COUNT(DISTINCT callee) AS callees FROM edges"
                ).fetchone()
                definition_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM definitions"
                ).fetchone()["n"]
                reference_count = conn.execute(
                    'SELECT COUNT(*) AS n FROM "references"'
                ).fetchone()["n"]
                inheritance_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM inheritance_edges"
                ).fetchone()["n"]
                import_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM import_edges"
                ).fetchone()["n"]
                test_edge_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM test_edges"
                ).fetchone()["n"]
                languages = [
                    str(row["language"])
                    for row in conn.execute(
                        "SELECT DISTINCT language FROM files "
                        "WHERE supported=1 ORDER BY language"
                    )
                ]
                indexed_revision = self._meta_int(conn, "graph_indexed_revision")
                last_error = self._meta(conn, "last_error", "") or None
                current = (
                    accepted_revision is None
                    or indexed_revision >= int(accepted_revision)
                )
                needs_rebuild = bool(
                    int(file_row["supported"] or 0) > 0
                    and int(edge_row["edges"] or 0) > 0
                    and int(definition_count or 0) == 0
                )
                coverage_complete = (
                    self._meta_int(conn, "coverage_complete") == 1
                )
                ready = bool(
                    current
                    and not last_error
                    and not needs_rebuild
                    and coverage_complete
                    and int(file_row["supported"] or 0) > 0
                )
                return {
                    "workspace_id": workspace_id,
                    "schema_version": self._meta_int(conn, "schema_version"),
                    "index_kind": self._meta(conn, "index_kind", _INDEX_KIND),
                    "graph_indexed_revision": indexed_revision,
                    "accepted_revision": accepted_revision,
                    "pending_revisions": (
                        max(int(accepted_revision) - indexed_revision, 0)
                        if accepted_revision is not None
                        else 0
                    ),
                    "ready": ready,
                    "current": current,
                    "busy": False,
                    "files": int(file_row["files"] or 0),
                    "supported_files": int(file_row["supported"] or 0),
                    "unsupported_files": int(file_row["unsupported"] or 0),
                    "parse_error_files": int(file_row["errors"] or 0),
                    "edges": int(edge_row["edges"] or 0),
                    "definitions": int(definition_count or 0),
                    "references": int(reference_count or 0),
                    "inheritance_edges": int(inheritance_count or 0),
                    "import_edges": int(import_count or 0),
                    "test_edges": int(test_edge_count or 0),
                    "callers": int(edge_row["callers"] or 0),
                    "callees": int(edge_row["callees"] or 0),
                    "languages": languages,
                    "last_error": last_error,
                    "needs_rebuild": needs_rebuild,
                    "coverage_complete": coverage_complete,
                }
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.OperationalError) as exc:
            message = str(exc).lower()
            busy = "locked" in message or "busy" in message
            return {
                "workspace_id": workspace_id,
                "ready": False,
                "current": False,
                "busy": busy,
                "last_error": "graph_index_busy" if busy else str(exc),
                "accepted_revision": accepted_revision,
                "pending_revisions": None,
            }
        finally:
            lock.release()

    def try_readiness(
        self,
        *,
        workspace_id: str,
        accepted_revision: Optional[int] = None,
        lock_timeout_ms: int = 50,
    ) -> dict[str, Any]:
        """Return the graph readiness contract without aggregate table scans.

        ``try_status`` intentionally exposes detailed counts, which requires
        scanning large graph tables. Impact/context preflight only needs to
        know whether the persisted graph is current and usable. Keeping this
        probe O(1) in graph size avoids multi-second status work before every
        symbol analysis on repositories such as Kafka.
        """
        lock = self._workspace_lock(workspace_id)
        acquired = lock.acquire(timeout=max(lock_timeout_ms, 0) / 1000.0)
        if not acquired:
            return {
                "workspace_id": workspace_id,
                "ready": False,
                "current": False,
                "busy": True,
                "last_error": "graph_index_busy",
                "accepted_revision": accepted_revision,
                "pending_revisions": None,
                "status_detail": "readiness",
            }
        try:
            conn = self._connect_readonly(
                workspace_id,
                timeout_s=max(lock_timeout_ms, 1) / 1000.0,
            )
            try:
                indexed_revision = self._meta_int(conn, "graph_indexed_revision")
                last_error = self._meta(conn, "last_error", "") or None
                coverage_complete = self._meta_int(conn, "coverage_complete") == 1
                current = (
                    accepted_revision is None
                    or indexed_revision >= int(accepted_revision)
                )
                supported = conn.execute(
                    "SELECT 1 FROM files WHERE supported=1 LIMIT 1"
                ).fetchone() is not None
                edge_exists = conn.execute(
                    "SELECT 1 FROM edges LIMIT 1"
                ).fetchone() is not None
                definition_exists = conn.execute(
                    "SELECT 1 FROM definitions LIMIT 1"
                ).fetchone() is not None
                needs_rebuild = bool(edge_exists and not definition_exists)
                return {
                    "workspace_id": workspace_id,
                    "schema_version": self._meta_int(conn, "schema_version"),
                    "index_kind": self._meta(conn, "index_kind", _INDEX_KIND),
                    "graph_indexed_revision": indexed_revision,
                    "accepted_revision": accepted_revision,
                    "pending_revisions": (
                        max(int(accepted_revision) - indexed_revision, 0)
                        if accepted_revision is not None
                        else 0
                    ),
                    "ready": bool(
                        current
                        and not last_error
                        and not needs_rebuild
                        and coverage_complete
                        and supported
                    ),
                    "current": current,
                    "busy": False,
                    "last_error": last_error,
                    "needs_rebuild": needs_rebuild,
                    "coverage_complete": coverage_complete,
                    "supported_files_present": supported,
                    "status_detail": "readiness",
                }
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.OperationalError) as exc:
            message = str(exc).lower()
            busy = "locked" in message or "busy" in message
            return {
                "workspace_id": workspace_id,
                "ready": False,
                "current": False,
                "busy": busy,
                "last_error": "graph_index_busy" if busy else str(exc),
                "accepted_revision": accepted_revision,
                "pending_revisions": None,
                "status_detail": "readiness",
            }
        finally:
            lock.release()

    def find_definitions(
        self,
        *,
        workspace_id: str,
        symbol: str,
        symbol_path: Optional[str] = None,
        limit: int = 5,
        lock_timeout_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Return lightweight graph definitions for a symbol.

        This is intentionally cheaper than ``impact``. API callers use it as a
        preflight so large unsupported-language files do not trigger broad
        file-level graph aggregation when the graph has no definition for the
        requested symbol.
        """
        clean_symbol = (symbol or "").strip()
        if not clean_symbol:
            return []
        max_rows = max(1, min(int(limit), 50))
        normalized_path = (
            normalize_snapshot_path(symbol_path) if symbol_path else None
        )
        lock = self._workspace_lock(workspace_id)
        acquired = (
            lock.acquire(timeout=max(lock_timeout_ms, 0) / 1000.0)
            if lock_timeout_ms is not None
            else lock.acquire()
        )
        if not acquired:
            return []
        try:
            conn = self._connect_readonly(
                workspace_id,
                timeout_s=(
                    max(lock_timeout_ms, 1) / 1000.0
                    if lock_timeout_ms is not None
                    else 0.1
                ),
            )
            try:
                path_clause = ""
                params: list[Any] = [clean_symbol, clean_symbol]
                if normalized_path:
                    path_clause = " AND path=?"
                    params.append(normalized_path)
                params.extend([clean_symbol, max_rows])
                rows = conn.execute(
                    "SELECT path, name, kind, parent, qualified_name, "
                    "line_start, line_end, language, source_provider, "
                    "confidence, revision FROM definitions "
                    "WHERE (name=? OR qualified_name=?)"
                    f"{path_clause} "
                    "ORDER BY "
                    "CASE WHEN name=? THEN 0 ELSE 1 END, "
                    "path, line_start "
                    "LIMIT ?",
                    params,
                )
                return [dict(row) for row in rows]
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.OperationalError):
            return []
        finally:
            lock.release()

    @staticmethod
    def _query_names(
        conn: sqlite3.Connection,
        *,
        column: str,
        match_column: str,
        names: Iterable[str],
    ) -> set[str]:
        values = sorted({name for name in names if name})
        if not values:
            return set()
        out: set[str] = set()
        for start in range(0, len(values), 400):
            chunk = values[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT DISTINCT {column} AS name FROM edges "
                f"WHERE {match_column} IN ({placeholders})",
                chunk,
            ):
                out.add(str(row["name"]))
        return out

    @staticmethod
    def _traversable_names(
        conn: sqlite3.Connection,
        names: Iterable[str],
        *,
        max_definitions: int = 3,
    ) -> set[str]:
        values = sorted({name for name in names if name})
        if not values:
            return set()
        out: set[str] = set()
        for start in range(0, len(values), 400):
            chunk = values[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT name, COUNT(DISTINCT path) AS definitions "
                "FROM definitions "
                f"WHERE name IN ({placeholders}) GROUP BY name",
                chunk,
            ):
                if int(row["definitions"] or 0) <= max_definitions:
                    out.add(str(row["name"]))
        return out

    def impact(
        self,
        *,
        workspace_id: str,
        symbol: str,
        depth: int = 2,
        symbol_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return persisted graph relations, with class/file aggregation fallback."""
        clean_symbol = (symbol or "").strip()
        if not clean_symbol:
            return {"symbol": clean_symbol, "found": False}
        if symbol_path and self._language_for_path(symbol_path) is None:
            return {
                "symbol": clean_symbol,
                "found": False,
                "resolution_mode": "unsupported_symbol_language",
                "seed_symbols": [clean_symbol],
                "ambiguous_seed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "dependent_symbols": [],
                "affected_symbols": [],
                "dependent_count": 0,
                "affected_count": 0,
                "files_involved": [],
                "files_count": 0,
                "total_blast_radius": 1,
                "reason": "symbol language is not supported by the graph parser",
            }

        with self._workspace_lock(workspace_id):
            conn = self._connect_readonly(workspace_id, timeout_s=1.0)
            try:
                seed_symbols = {clean_symbol}
                resolution_mode = "symbol"
                ambiguous_seed_symbols: set[str] = set()
                direct_callers = self._query_names(
                    conn,
                    column="caller",
                    match_column="callee",
                    names=seed_symbols,
                )
                direct_callees = self._query_names(
                    conn,
                    column="callee",
                    match_column="caller",
                    names=seed_symbols,
                )

                normalized_path = (
                    normalize_snapshot_path(symbol_path)
                    if symbol_path
                    else None
                )
                if not direct_callers and not direct_callees and normalized_path:
                    file_callers = {
                        str(row["name"])
                        for row in conn.execute(
                            "SELECT DISTINCT name FROM definitions "
                            "WHERE path=? AND parent=?",
                            (normalized_path, clean_symbol),
                        )
                    }
                    if not file_callers:
                        file_callers = {
                            str(row["caller"])
                            for row in conn.execute(
                                "SELECT DISTINCT caller FROM edges WHERE path=?",
                                (normalized_path,),
                            )
                        }
                    if file_callers:
                        traversable = self._traversable_names(
                            conn,
                            file_callers,
                        )
                        if traversable:
                            ambiguous_seed_symbols = file_callers - traversable
                            file_callers = traversable
                        seed_symbols = file_callers
                        resolution_mode = "file_symbol_aggregate"
                        direct_callers = self._query_names(
                            conn,
                            column="caller",
                            match_column="callee",
                            names=seed_symbols,
                        )
                        direct_callees = self._query_names(
                            conn,
                            column="callee",
                            match_column="caller",
                            names=seed_symbols,
                        )

                callers = set(direct_callers)
                callees = set(direct_callees)
                caller_frontier = set(seed_symbols)
                callee_frontier = set(seed_symbols)
                for _ in range(max(int(depth), 1)):
                    caller_frontier = self._traversable_names(
                        conn,
                        caller_frontier,
                    )
                    callee_frontier = self._traversable_names(
                        conn,
                        callee_frontier,
                    )
                    if not caller_frontier and not callee_frontier:
                        break
                    next_callers = self._query_names(
                        conn,
                        column="caller",
                        match_column="callee",
                        names=caller_frontier,
                    ) - callers - seed_symbols
                    next_callees = self._query_names(
                        conn,
                        column="callee",
                        match_column="caller",
                        names=callee_frontier,
                    ) - callees - seed_symbols
                    callers.update(next_callers)
                    callees.update(next_callees)
                    caller_frontier = next_callers
                    callee_frontier = next_callees
                    if not caller_frontier and not callee_frontier:
                        break

                all_names = callers | callees | seed_symbols
                files: set[str] = set()
                values = sorted(seed_symbols)
                for start in range(0, len(values), 350):
                    chunk = values[start : start + 350]
                    placeholders = ",".join("?" for _ in chunk)
                    params = chunk + chunk
                    for row in conn.execute(
                        "SELECT DISTINCT path FROM edges "
                        f"WHERE caller IN ({placeholders}) "
                        f"OR callee IN ({placeholders})",
                        params,
                    ):
                        files.add(str(row["path"]))

                definition_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT path, name, kind, parent, qualified_name, "
                        "line_start, line_end, language, source_provider, "
                        "confidence, revision FROM definitions "
                        "WHERE name=? OR qualified_name=? "
                        "ORDER BY confidence DESC, path, line_start LIMIT 50",
                        (clean_symbol, clean_symbol),
                    )
                ]
                reference_rows: list[dict[str, Any]] = []
                for name in sorted(seed_symbols):
                    reference_rows.extend(
                        dict(row)
                        for row in conn.execute(
                            "SELECT path, symbol, line, context, language, "
                            "source_provider, confidence, revision "
                            'FROM "references" WHERE symbol=? '
                            "ORDER BY confidence DESC, path, line LIMIT 100",
                            (name,),
                        )
                    )
                inheritance_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT path, child, parent, edge_type, line, language, "
                        "source_provider, confidence, revision "
                        "FROM inheritance_edges WHERE child=? OR parent=? "
                        "ORDER BY confidence DESC, path, line LIMIT 100",
                        (clean_symbol, clean_symbol),
                    )
                ]
                test_rows: list[dict[str, Any]] = []
                for name in sorted(seed_symbols | {clean_symbol}):
                    test_rows.extend(
                        dict(row)
                        for row in conn.execute(
                            "SELECT symbol, test_path, line, language, "
                            "source_provider, confidence, revision "
                            "FROM test_edges WHERE symbol=? "
                            "ORDER BY confidence DESC, test_path, line LIMIT 100",
                            (name,),
                        )
                    )
                files.update(str(row["path"]) for row in definition_rows)
                files.update(str(row["path"]) for row in reference_rows)
                files.update(str(row["path"]) for row in inheritance_rows)
                files.update(str(row["test_path"]) for row in test_rows)
                bases = sorted({
                    str(row["parent"])
                    for row in inheritance_rows
                    if str(row["child"]) == clean_symbol
                })
                subclasses = sorted({
                    str(row["child"])
                    for row in inheritance_rows
                    if str(row["parent"]) == clean_symbol
                })
                test_candidates = sorted({
                    str(row["test_path"])
                    for row in test_rows
                })
                found = bool(
                    callers
                    or callees
                    or definition_rows
                    or reference_rows
                    or inheritance_rows
                )
                return {
                    "symbol": clean_symbol,
                    "found": found,
                    "resolution_mode": resolution_mode,
                    "seed_symbols": sorted(seed_symbols),
                    "ambiguous_seed_symbols": sorted(ambiguous_seed_symbols),
                    "direct_callers": sorted(direct_callers),
                    "direct_callees": sorted(direct_callees),
                    "dependent_symbols": sorted(callers),
                    "affected_symbols": sorted(callees),
                    "dependent_count": len(callers),
                    "affected_count": len(callees),
                    "files_involved": sorted(files),
                    "files_count": len(files),
                    "total_blast_radius": len(all_names),
                    "definitions": definition_rows,
                    "references": reference_rows,
                    "inheritance": {
                        "bases": bases,
                        "subclasses": subclasses,
                        "edges": inheritance_rows,
                    },
                    "test_candidates": test_candidates,
                    "evidence_providers": sorted({
                        str(row.get("source_provider") or "")
                        for row in (
                            definition_rows
                            + reference_rows
                            + inheritance_rows
                            + test_rows
                        )
                        if row.get("source_provider")
                    }),
                }
            finally:
                conn.close()

    def related_tests(
        self,
        *,
        workspace_id: str,
        symbol: str,
        symbol_path: Optional[str] = None,
        max_results: int = 50,
    ) -> list[str]:
        impact = self.impact(
            workspace_id=workspace_id,
            symbol=symbol,
            depth=3,
            symbol_path=symbol_path,
        )
        names = set(impact.get("dependent_symbols") or [])
        names.update(impact.get("seed_symbols") or [])
        direct_candidates = list(impact.get("test_candidates") or [])
        if direct_candidates:
            return sorted(set(direct_candidates))[:max_results]
        if not names:
            return []
        with self._workspace_lock(workspace_id):
            conn = self._connect(workspace_id)
            try:
                out: set[str] = set()
                values = sorted(names)
                for start in range(0, len(values), 350):
                    chunk = values[start : start + 350]
                    placeholders = ",".join("?" for _ in chunk)
                    params = chunk + chunk
                    for row in conn.execute(
                        "SELECT DISTINCT path FROM edges "
                        f"WHERE (caller IN ({placeholders}) "
                        f"OR callee IN ({placeholders})) "
                        "LIMIT ?",
                        params + [max(max_results * 20, 200)],
                    ):
                        path = str(row["path"])
                        name = Path(path).name.lower()
                        if (
                            name.startswith("test_")
                            or name in {"test.py", "tests.py"}
                            or name.endswith(
                                (
                                    "_test.py",
                                    "_tests.py",
                                    ".spec.js",
                                    ".spec.ts",
                                    ".test.js",
                                    ".test.ts",
                                    "test.java",
                                    "tests.java",
                                    "test.scala",
                                    "tests.scala",
                                    "spec.scala",
                                )
                            )
                        ):
                            out.add(path)
                return sorted(out)[:max_results]
            finally:
                conn.close()


__all__ = ["WorkspaceGraphIndex"]
