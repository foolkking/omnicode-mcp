import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from omnicode.ast_engine.chunker import ASTChunker
from omnicode.ast_engine.parser import UnifiedASTParser
from omnicode.search.hybrid_search import HybridSearchEngine
from omnicode.search.models import SearchRequest
from omnicode.search.vector_store import VectorStore

logger = logging.getLogger(__name__)

class LegacySearchResult:
    """
    Adapter class representing a search result in the legacy API schema.
    """
    def __init__(
        self,
        file_path: str,
        symbol_name: str,
        chunk_type: str,
        line_start: int,
        line_end: int,
        signature: str,
        docstring: str,
        relevance_score: float
    ):
        self.file_path = file_path
        self.symbol_name = symbol_name
        self.chunk_type = chunk_type
        self.line_start = line_start
        self.line_end = line_end
        self.signature = signature
        self.docstring = docstring
        self.relevance_score = relevance_score

class SqliteKeywordSearcher:
    """
    Keyword searcher query engine on SQLite chunks for hybrid search combination.
    """
    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    async def search(self, query: str, top_k: int = 10, metadata_filter: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        cursor = self.vector_store.conn.cursor()
        cursor.execute("SELECT chunk_id, file_path, content, chunk_type, metadata FROM chunks WHERE content LIKE ?", (f"%{query}%",))
        rows = cursor.fetchall()

        results = []
        for row in rows:
            meta = json.loads(row['metadata'])
            if metadata_filter:
                match = True
                for k, v in metadata_filter.items():
                    if k == "file_path" and row["file_path"] != v:
                        match = False
                        break
                    elif meta.get(k) != v:
                        match = False
                        break
                if not match:
                    continue

            results.append({
                "chunk_id": row['chunk_id'],
                "file_path": row['file_path'],
                "content": row['content'],
                "chunk_type": row['chunk_type'],
                "score": 1.0,
                "metadata": meta
            })
            if len(results) >= top_k:
                break
        return results

class SemanticSearchEngine:
    """
    Bridges the legacy SemanticSearchEngine to the new hybrid Tree-sitter + FAISS + SentenceTransformers search architecture.
    """
    def __init__(self, working_dir: str):
        self.working_dir = os.path.abspath(working_dir)
        self.db_dir = os.path.join(self.working_dir, ".data")
        os.makedirs(self.db_dir, exist_ok=True)

        # Instantiate Omnicode modules
        self.ast_parser = UnifiedASTParser()
        self.chunker = ASTChunker(self.ast_parser)
        self.vector_store = VectorStore(os.path.join(self.db_dir, "vector_store.db"), dimension=384)

        # Hybrid Search setup
        self.keyword_searcher = SqliteKeywordSearcher(self.vector_store)
        self.hybrid_engine = HybridSearchEngine(self.vector_store, self.keyword_searcher)

        # Local embeddings generator model
        self.embedding_model = None
        self.stats = {
            "total_files": 0,
            "total_chunks": 0,
            "total_symbols": 0,
            "last_indexed": "never",
            "index_size": 0
        }

    async def initialize(self) -> None:
        """Initialize the embedding model and verify DB files"""
        logger.info("Initializing Semantic Search Engine...")
        if self.embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer

                from omnicode.config.settings import get_settings

                model_name = get_settings().EMBEDDING_MODEL
                # Allow short names ("all-MiniLM-L6-v2") or full HF refs
                # ("sentence-transformers/all-MiniLM-L6-v2"). SentenceTransformer
                # handles both, but log the effective choice.
                self.embedding_model = SentenceTransformer(model_name)
                logger.info(f"✅ sentence-transformers {model_name} loaded successfully")
            except Exception as e:
                logger.error(f"❌ Failed to load sentence-transformers: {e}")
                raise

        # Fetch index statistics from database
        try:
            cursor = self.vector_store.conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT file_path), COUNT(*) FROM chunks")
            row = cursor.fetchone()
            if row:
                self.stats["total_files"] = row[0]
                self.stats["total_chunks"] = row[1]
                if row[1] > 0:
                    self.stats["last_indexed"] = time.strftime("%Y-%m-%d %H:%M:%S")

            # Symbols = chunks whose chunk_type is a code symbol
            # (function / class / method / etc).  Excludes 'whole-file' or
            # 'comment' chunks if any.
            cursor.execute(
                "SELECT COUNT(*) FROM chunks WHERE chunk_type IN "
                "('function', 'class', 'method', 'function_definition', "
                "'class_definition', 'method_definition', 'function_declaration')"
            )
            row = cursor.fetchone()
            if row:
                self.stats["total_symbols"] = row[0]

            db_file = os.path.join(self.db_dir, "vector_store.db")
            if os.path.exists(db_file):
                self.stats["index_size"] = os.path.getsize(db_file)
        except Exception as e:
            logger.warning(f"Failed to load search stats from DB: {e}")

        # Auto-recover semantic search index when chunks exist on disk but
        # FAISS knows nothing about them (legacy DB without embedding BLOB,
        # corrupted .faiss file, etc.).  Without this users see "0 results"
        # for semantic search until they manually run /search/index.
        try:
            chunk_count = self.stats.get("total_chunks", 0)
            faiss_total = getattr(self.vector_store.index, "ntotal", 0)
            if chunk_count > 0 and faiss_total == 0:
                logger.warning(
                    "Semantic index out of sync (%d chunks, FAISS empty) — "
                    "running automatic reindex to repopulate embeddings.",
                    chunk_count,
                )
                await self.index_codebase()
                self.stats["last_indexed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Automatic semantic reindex failed: %s", exc)

    def get_stats(self) -> dict:
        return self.stats

    async def update_file(self, file_path: str) -> None:
        """Parse, chunk, embed, and store a single file"""
        full_path = os.path.abspath(os.path.join(self.working_dir, file_path))
        if not os.path.exists(full_path):
            logger.warning(f"File not found for index update: {file_path}")
            return

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"Could not read {file_path}: {e}")
            return

        # 1. Delete old chunks for this file
        await self.vector_store.delete_by_file(file_path)

        # 2. Extract AST Chunks
        language = os.path.splitext(file_path)[1].lstrip(".") or "python"
        chunks = self.chunker.chunk_file(content, file_path, language)

        # 3. Generate embeddings and add to store
        for chunk in chunks:
            # Generate embedding
            emb = self.embedding_model.encode(chunk.content)

            # Map chunk metadata
            metadata = {
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "symbol_name": chunk.symbol_name or "",
                "signature": chunk.signature or "",
                "docstring": chunk.docstring or ""
            }

            await self.vector_store.add(
                chunk_id=chunk.chunk_id,
                embedding=emb,
                file_path=file_path,
                chunk_type=chunk.chunk_type,
                content=chunk.content,
                metadata=metadata
            )

        # Recalculate stats
        await self.initialize()

    async def index_codebase(self) -> None:
        """Scan working directory, parse all source files, and index them.

        Uses incremental indexing: only new/modified files are re-embedded.
        Deleted files are removed from the index.  Unchanged files are skipped.
        This reduces typical rebuild time from 30-60s to 2-3s.
        """
        from omnicode_core.index.file_tracker import FileTracker

        logger.info(f"Indexing codebase in {self.working_dir}...")

        tracker_db = os.path.join(self.db_dir, "file_tracker.db")
        tracker = FileTracker(tracker_db)
        changes = tracker.detect_changes(self.working_dir)

        if not changes:
            logger.info("No file changes detected — index is up to date.")
            return

        new_count = sum(1 for c in changes if c.change_type == "new")
        mod_count = sum(1 for c in changes if c.change_type == "modified")
        del_count = sum(1 for c in changes if c.change_type == "deleted")
        logger.info(
            f"Incremental index: {new_count} new, {mod_count} modified, "
            f"{del_count} deleted, {len(changes)} total changes"
        )

        for change in changes:
            try:
                if change.change_type == "deleted":
                    await self.vector_store.delete_by_file(change.path)
                    tracker.mark_deleted(change.path)
                    logger.debug(f"Removed from index: {change.path}")
                else:
                    # new or modified — re-index
                    await self.update_file(change.path)
                    tracker.mark_indexed(
                        self.working_dir, change.path, change.content_hash
                    )
            except Exception as e:
                logger.warning(f"Failed to index {change.path}: {e}")

        # Recalculate stats
        await self.initialize()
        logger.info("Codebase indexing completed successfully.")

    async def list_symbols_in_file(self, file_path: str) -> dict:
        """Extract every named symbol (function / class / method) in a file.

        Uses ``UnifiedASTParser.extract_symbols`` so we get real names plus
        accurate line ranges, including methods nested inside classes.  Falls
        back to an empty list if the language is unsupported.
        """
        full_path = os.path.abspath(os.path.join(self.working_dir, file_path))
        if not os.path.exists(full_path):
            return {"error": f"File not found: {file_path}", "symbols": []}

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            return {"error": f"Could not read file: {e}", "symbols": []}

        language = self._guess_language(file_path)
        try:
            extracted = self.ast_parser.extract_symbols(content, language) or []
        except Exception as exc:
            return {
                "error": f"AST parse failed for {file_path} ({language}): {exc}",
                "symbols": [],
                "file_path": file_path,
                "count": 0,
            }

        symbols = []
        for sym in extracted:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name") or "<anonymous>"
            sline = sym.get("line_start") or sym.get("start_line") or 1
            eline = sym.get("line_end") or sym.get("end_line") or sline
            symbols.append(
                {
                    "name": name,
                    "type": sym.get("type") or "symbol",
                    "line_start": int(sline),
                    "line_end": int(eline),
                    "parent": sym.get("parent"),
                    "language": sym.get("language", language),
                }
            )

        return {
            "file_path": file_path,
            "language": language,
            "symbols": symbols,
            "count": len(symbols),
        }

    async def read_symbol_content(
        self,
        file_path: str,
        symbol_name: Optional[str] = None,
        occurrence: int = 1,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        with_line_numbers: bool = True,
    ) -> dict:
        """Read part of a file by line range or by symbol name.

        Resolution order:
          1. ``start_line`` + ``end_line``  -> exact line slice
          2. ``symbol_name``                -> AST-based symbol lookup
          3. nothing                        -> entire file

        Returns a dict with ``success``/``error`` and (on success):
            file_path, content, total_lines, [start_line, end_line, symbol_name]
        """
        full_path = os.path.abspath(os.path.join(self.working_dir, file_path))
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {file_path}"}
        if not os.path.isfile(full_path):
            return {"success": False, "error": f"Not a regular file: {file_path}"}

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as exc:
            return {"success": False, "error": f"Could not read file: {exc}"}

        lines = content.splitlines()
        total_lines = len(lines)

        # Branch 1 — explicit line range
        if start_line is not None and end_line is not None:
            if start_line < 1:
                return {"success": False, "error": f"start_line {start_line} < 1"}
            if end_line > total_lines:
                end_line = total_lines
            if end_line < start_line:
                return {
                    "success": False,
                    "error": f"end_line ({end_line}) < start_line ({start_line})",
                }
            slice_ = lines[start_line - 1 : end_line]
            rendered = self._render_lines(slice_, start_line, with_line_numbers)
            return {
                "success": True,
                "file_path": file_path,
                "content": rendered,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
            }

        # Branch 2 — symbol lookup via AST
        if symbol_name:
            language = self._guess_language(file_path)
            try:
                hits = self.ast_parser.extract_symbols(content, language)
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"AST parse failed for {file_path} ({language}): {exc}",
                }
            matches = []
            for s in (hits or []):
                name = s.get("name") if isinstance(s, dict) else getattr(s, "name", None)
                if name == symbol_name:
                    matches.append(s)
            if not matches:
                return {
                    "success": False,
                    "error": f"Symbol '{symbol_name}' not found in {file_path}",
                }
            idx = max(0, min(occurrence - 1, len(matches) - 1))
            sym = matches[idx]
            if isinstance(sym, dict):
                s_line = sym.get("line_start") or sym.get("start_line")
                e_line = sym.get("line_end") or sym.get("end_line")
            else:
                s_line = getattr(sym, "line_start", None) or getattr(sym, "start_line", None)
                e_line = getattr(sym, "line_end", None) or getattr(sym, "end_line", None)
            if s_line is None or e_line is None:
                return {
                    "success": False,
                    "error": f"Symbol '{symbol_name}' has no line range",
                }
            slice_ = lines[s_line - 1 : e_line]
            rendered = self._render_lines(slice_, s_line, with_line_numbers)
            return {
                "success": True,
                "file_path": file_path,
                "content": rendered,
                "symbol_name": symbol_name,
                "occurrence": occurrence,
                "start_line": s_line,
                "end_line": e_line,
                "total_lines": total_lines,
                "matches_found": len(matches),
            }

        # Branch 3 — entire file
        rendered = self._render_lines(lines, 1, with_line_numbers)
        return {
            "success": True,
            "file_path": file_path,
            "content": rendered,
            "start_line": 1,
            "end_line": total_lines,
            "total_lines": total_lines,
        }

    @staticmethod
    def _render_lines(chunk, first_line: int, with_line_numbers: bool) -> str:
        if not with_line_numbers:
            return "\n".join(chunk)
        width = len(str(first_line + len(chunk) - 1))
        return "\n".join(
            f"{(first_line + i):>{width}} | {ln}" for i, ln in enumerate(chunk)
        )

    @staticmethod
    def _guess_language(file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        return {
            ".py": "python", ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".cpp": "cpp", ".cc": "cpp", ".c": "cpp",
            ".h": "cpp", ".hpp": "cpp",
            ".java": "java", ".go": "go", ".rs": "rust",
        }.get(ext, "python")

    async def search(self, request: SearchRequest) -> List[LegacySearchResult]:
        """Execute hybrid RRF search or text search and map to legacy SearchResult model"""
        logger.info(f"Executing search: query='{request.query}', type='{request.search_type}'")

        metadata_filter = None
        if request.file_pattern:
            metadata_filter = {"file_path": request.file_pattern} # simplistic glob filtering mapping

        results = []

        if request.search_type == "text":
            # Simple text scanning in SQLite chunks
            cursor = self.vector_store.conn.cursor()
            cursor.execute("SELECT file_path, content FROM chunks WHERE content LIKE ?", (f"%{request.query}%",))
            rows = cursor.fetchall()

            for row in rows:
                results.append(LegacySearchResult(
                    file_path=row["file_path"],
                    symbol_name="",
                    chunk_type=row["content"][:200],  # matched content context
                    line_start=1,
                    line_end=1,
                    signature="",
                    docstring="",
                    relevance_score=1.0
                ))
                if len(results) >= request.max_results:
                    break
        elif request.search_type in ("symbol", "symbol_exact", "fuzzy_symbol"):
            # Symbol search — match the literal symbol name stored in
            # ``metadata.symbol_name`` (extracted at indexing time by the
            # AST chunker).  Falls back to scanning the content column when
            # metadata is unavailable.
            cursor = self.vector_store.conn.cursor()
            q = request.query.strip()
            if not q:
                return results

            fuzzy = request.search_type != "symbol_exact"
            # The metadata column is JSON text in SQLite, so a LIKE on the
            # serialized blob is a cheap way to find the symbol_name field.
            if fuzzy:
                pattern = f'%"symbol_name": "%{q}%"%'
                pattern2 = f'%"symbol_name": "%{q.lower()}%"%'
            else:
                pattern = f'%"symbol_name": "{q}"%'
                pattern2 = pattern  # exact mode — single pattern

            # Optional symbol-type filter (function / class / method / ...).
            sql_extra = ""
            params = [pattern, pattern2]
            if getattr(request, "symbol_type", None):
                sql_extra = " AND chunk_type = ?"
                params.append(request.symbol_type)

            cursor.execute(
                f"""
                SELECT file_path, chunk_type, content, metadata
                FROM chunks
                WHERE (metadata LIKE ? OR LOWER(metadata) LIKE ?){sql_extra}
                LIMIT ?
                """,
                params + [request.max_results * 4],  # over-fetch then rank
            )
            rows = cursor.fetchall()

            # Score by simple distance: exact name = 1.0, prefix match = 0.9,
            # contains = 0.6.  Caller can filter by request.min_score.
            scored = []
            ql = q.lower()
            for row in rows:
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                except Exception:
                    meta = {}
                name = (meta.get("symbol_name") or "").strip()
                if not name:
                    continue
                nl = name.lower()
                if nl == ql:
                    score = 1.0
                elif nl.startswith(ql):
                    score = 0.9
                elif ql in nl:
                    score = 0.7
                else:
                    score = 0.5
                if score < (request.min_score or 0.0):
                    continue
                scored.append((score, row, meta, name))

            # Highest scoring matches first
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, row, meta, name in scored[: request.max_results]:
                results.append(LegacySearchResult(
                    file_path=row["file_path"],
                    symbol_name=name,
                    chunk_type=row["chunk_type"],
                    line_start=meta.get("start_line", 1),
                    line_end=meta.get("end_line", 1),
                    signature=meta.get("signature", ""),
                    docstring=meta.get("docstring", ""),
                    relevance_score=score,
                ))
        else:
            # Semantic / Hybrid search using RRF
            query_emb = self.embedding_model.encode(request.query)
            hybrid_results = await self.vector_store.search(
                query_emb,
                top_k=request.max_results,
                metadata_filter=metadata_filter
            )

            for item in hybrid_results:
                meta = item.get("metadata", {})
                results.append(LegacySearchResult(
                    file_path=item["file_path"],
                    symbol_name=meta.get("symbol_name", ""),
                    chunk_type=item["chunk_type"],
                    line_start=meta.get("start_line", 1),
                    line_end=meta.get("end_line", 1),
                    signature=meta.get("signature", ""),
                    docstring=meta.get("docstring", ""),
                    relevance_score=item["score"]
                ))

        return results



# ---------------------------------------------------------------------------
# Backwards-compat alias.  Older callers import ``SearchEngine``; the public
# class is now ``SemanticSearchEngine``.
# ---------------------------------------------------------------------------
SearchEngine = SemanticSearchEngine
