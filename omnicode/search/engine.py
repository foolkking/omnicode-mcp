import fnmatch
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from omnicode.ast_engine.chunker import CHUNKER_VERSION, ASTChunker
from omnicode.ast_engine.parser import UnifiedASTParser
from omnicode.search.hybrid_search import HybridSearchEngine
from omnicode.search.models import SearchRequest
from omnicode.search.vector_store import VectorStore
from omnicode_core.embeddings.backend import UnavailableEmbeddingBackend

logger = logging.getLogger(__name__)


def _norm_path(file_path: str) -> str:
    """Normalize a workspace-relative path for index storage.

    All chunks live under a single canonical key: forward slashes, no
    leading ``./``. Without this, the same file gets indexed twice on
    Windows whenever one caller uses ``omnicode\\search\\engine.py``
    and another uses ``omnicode/search/engine.py`` — and ~30% of search
    hits end up duplicated under the wrong shape.
    """
    if not file_path:
        return file_path
    p = str(file_path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _matches_any_glob(path: str, patterns: List[str]) -> bool:
    """Return True if ``path`` matches at least one of ``patterns``.

    Both ``path`` and the patterns are normalized to forward slashes
    before matching. Patterns can be:
      - basename globs:    "*.py"
      - subtree globs:     "src/**", "tests/**/*.py"
      - exact paths:       "main.py"
    """
    norm = _norm_path(path)
    base = os.path.basename(norm)
    for raw in patterns:
        pat = raw.strip().replace("\\", "/")
        if not pat or pat == "*" or pat == "**":
            return True
        if "/" not in pat:
            if fnmatch.fnmatch(base, pat):
                return True
        else:
            if fnmatch.fnmatch(norm, pat):
                return True
    return False


def _normalize_upsert_item(item: Any) -> tuple[str, str, Dict[str, Any]]:
    """Accept legacy ``(path, content)`` and metadata-aware upsert rows."""
    if isinstance(item, dict):
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")
        metadata = item.get("metadata") or {}
        return path, content, dict(metadata) if isinstance(metadata, dict) else {}
    try:
        path, content, metadata = item
    except ValueError:
        path, content = item
        metadata = {}
    return str(path), str(content), dict(metadata) if isinstance(metadata, dict) else {}


def _semantic_chunk_limit(metadata: Dict[str, Any]) -> int:
    try:
        return int(metadata.get("semantic_max_chunks_per_file") or 0)
    except (TypeError, ValueError):
        return 0


def _limit_semantic_chunks(chunks: list[Any], metadata: Dict[str, Any]) -> list[Any]:
    limit = _semantic_chunk_limit(metadata)
    if limit <= 0 or len(chunks) <= limit:
        return chunks
    metadata["semantic_chunk_limit_applied"] = True
    metadata["semantic_chunk_limit"] = limit
    metadata["semantic_chunks_original"] = len(chunks)
    metadata["semantic_chunks_dropped"] = len(chunks) - limit
    return chunks[:limit]


class LegacySearchResult:
    """
    Adapter class representing a search result in the legacy API schema.

    The ``why_matched`` field (Wave 1, gap §7) explains *why* a result
    appears: a list of compact tags such as ``["semantic", "symbol"]``
    or ``["text:exact", "recent_git_change"]`` so AI editors and humans
    can decide which signal to trust.
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
        relevance_score: float,
        why_matched: Optional[List[str]] = None,
    ):
        self.file_path = file_path
        self.symbol_name = symbol_name
        self.chunk_type = chunk_type
        self.line_start = line_start
        self.line_end = line_end
        self.signature = signature
        self.docstring = docstring
        self.relevance_score = relevance_score
        self.why_matched = list(why_matched) if why_matched else []

class SqliteKeywordSearcher:
    """
    Keyword searcher query engine on SQLite chunks for hybrid search combination.
    """
    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    async def search(self, query: str, top_k: int = 10, metadata_filter: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        with self.vector_store._lock:
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
    def __init__(
        self,
        working_dir: str,
        shard_id: Optional[str] = None,
        *,
        db_dir: Optional[str] = None,
    ):
        self.working_dir = os.path.abspath(working_dir)

        # Sharding (Wave 2 W2-10). When ``shard_id`` is omitted the
        # engine mounts the default shard so existing single-tenant
        # mounts keep working. Multi-tenant cloud deployments pick a
        # shard id per registered workspace and route reads/writes
        # accordingly.
        from omnicode_core.index.sharding import (
            DEFAULT_SHARD_ID,
            auto_migrate_legacy,
            resolve_shard_dir,
        )

        self.shard_id = shard_id or DEFAULT_SHARD_ID
        # First-run migration only matters for the default shard;
        # named shards are always created fresh.
        if self.shard_id == DEFAULT_SHARD_ID:
            auto_migrate_legacy(self.working_dir)
        if db_dir:
            self.db_dir = os.path.abspath(db_dir)
            os.makedirs(self.db_dir, exist_ok=True)
        else:
            self.db_dir = resolve_shard_dir(self.working_dir, self.shard_id)

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
            "index_size": 0,
            "semantic_available": False,
            "semantic_unavailable_reason": "embedding_model_not_loaded",
        }

    def replace_semantic_index_from(
        self,
        staging: "SemanticSearchEngine",
    ) -> Dict[str, Any]:
        """Activate a fully-built staging semantic index."""

        activation = self.vector_store.replace_from(staging.vector_store)
        self.keyword_searcher = SqliteKeywordSearcher(self.vector_store)
        self.hybrid_engine = HybridSearchEngine(
            self.vector_store,
            self.keyword_searcher,
        )
        self.refresh_stats()
        return activation

    def _embedding_backend_model_name(self) -> Optional[str]:
        backend = self.embedding_model
        if backend is None:
            return None
        for attr in ("model_name", "_model_name", "model", "_model"):
            value = getattr(backend, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _ensure_embedding_backend(self) -> None:
        """Refresh unavailable/stale embedding backend handles.

        A backend may start before ``omnicode models pull`` has populated the
        fixed cache directory.  Keep exact search available in that state, but
        let semantic operations recover after the model appears without
        requiring a process restart.
        """
        from omnicode_core.embeddings import get_default_backend
        from omnicode_core.embeddings.models import embedding_model_config

        model_name = embedding_model_config().model_name
        cached_name = self._embedding_backend_model_name()
        if (
            self.embedding_model is None
            or isinstance(self.embedding_model, UnavailableEmbeddingBackend)
            or (cached_name is not None and cached_name != model_name)
        ):
            self.embedding_model = get_default_backend(model_name)

    def _semantic_runtime(self) -> Dict[str, Any]:
        from omnicode_core.embeddings.models import embedding_model_config

        config = embedding_model_config()
        backend = self.embedding_model
        dimension = getattr(backend, "dimension", None)
        return {
            "embedding_model": config.model_name,
            "embedding_revision": config.revision,
            "embedding_dimension": int(dimension) if dimension else None,
            "embedding_backend": getattr(backend, "name", "unavailable"),
            "chunker_version": CHUNKER_VERSION,
            "normalization": "l2",
        }

    def semantic_index_status(self) -> Dict[str, Any]:
        self._ensure_embedding_backend()
        runtime = self._semantic_runtime()
        status = self.vector_store.semantic_metadata_status(
            embedding_model=runtime["embedding_model"],
            embedding_revision=runtime["embedding_revision"],
            embedding_dimension=runtime["embedding_dimension"],
            chunker_version=runtime["chunker_version"],
        )
        status["embedding_available"] = self.semantic_available()
        status["runtime"] = runtime
        if not self.semantic_available():
            status["semantic_index_ready"] = False
            status["semantic_index_stale_reason"] = (
                self.semantic_unavailable_reason()
            )
        return status

    def prepare_semantic_index(
        self,
        *,
        force: bool = False,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate the mounted vectors or reset them for an explicit rebuild."""
        self._ensure_embedding_backend()
        runtime = self._semantic_runtime()
        dimension = runtime.get("embedding_dimension")
        if not self.semantic_available() or not dimension:
            raise RuntimeError(
                "EMBEDDING_UNAVAILABLE: "
                + (self.semantic_unavailable_reason() or "embedding dimension unavailable")
            )
        status = self.semantic_index_status()
        incompatible = bool(
            status.get("semantic_index_stale")
            or status.get("semantic_index_invalid")
        )
        metadata_missing = not bool(status.get("metadata"))
        vector_count = int(status.get("vector_count") or 0)
        if force:
            self.vector_store.reset_index(dimension=int(dimension))
            status = self.semantic_index_status()
        elif vector_count == 0 and self.vector_store.index_dimension() != int(dimension):
            self.vector_store.reset_index(dimension=int(dimension))
            status = self.semantic_index_status()
        elif incompatible or (vector_count > 0 and metadata_missing):
            reason = status.get("semantic_index_stale_reason") or "metadata_missing"
            raise RuntimeError(
                f"SEMANTIC_INDEX_INCOMPATIBLE: {reason}; "
                "run omni_index(scope='semantic', force=true)"
            )
        if workspace_id:
            runtime["workspace_id"] = workspace_id
        return status

    def _write_semantic_metadata(
        self,
        *,
        workspace_id: Optional[str] = None,
        indexed_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        runtime = self._semantic_runtime()
        return self.vector_store.set_index_metadata(
            embedding_model=str(runtime["embedding_model"]),
            embedding_revision=runtime.get("embedding_revision"),
            embedding_dimension=runtime.get("embedding_dimension"),
            embedding_backend=str(runtime["embedding_backend"]),
            chunker_version=str(runtime["chunker_version"]),
            normalization=str(runtime["normalization"]),
            workspace_id=workspace_id,
            indexed_revision=indexed_revision,
        )

    async def initialize(self) -> None:
        """Initialize the embedding model and verify DB files"""
        logger.info("Initializing Semantic Search Engine...")
        if self.embedding_model is None:
            try:
                from omnicode_core.embeddings import get_default_backend
                from omnicode_core.embeddings.models import embedding_model_config

                model_name = embedding_model_config().model_name
                # ``get_default_backend`` honours OMNICODE_EMBEDDING_BACKEND
                # (local | remote | hybrid). Local mode keeps the offline
                # SentenceTransformer behaviour the engine has had.
                self.embedding_model = get_default_backend(model_name)
                logger.info(f"✅ embedding backend ready: {self.embedding_model.name} ({model_name})")
            except Exception as e:
                logger.error(f"❌ Failed to load sentence-transformers: {e}")
                raise

        # One-shot self-heal: drop any legacy backslash-shaped duplicates
        # left over from before path normalization landed.
        try:
            self._dedupe_legacy_path_rows()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Legacy path dedupe skipped: %s", exc)

        self.refresh_stats()

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

    def refresh_stats(self) -> None:
        """Refresh cheap index statistics without reinitializing heavy services."""
        try:
            cursor = self.vector_store.conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT file_path), COUNT(*) FROM chunks")
            row = cursor.fetchone()
            if row:
                self.stats["total_files"] = row[0]
                self.stats["total_chunks"] = row[1]
                self.stats["last_indexed"] = (
                    time.strftime("%Y-%m-%d %H:%M:%S") if row[1] > 0 else "never"
                )

            cursor.execute(
                "SELECT COUNT(*) FROM chunks WHERE chunk_type IN "
                "('function', 'class', 'method', 'function_definition', "
                "'class_definition', 'method_definition', 'function_declaration')"
            )
            row = cursor.fetchone()
            if row:
                self.stats["total_symbols"] = row[0]

            db_file = os.path.join(self.db_dir, "vector_store.db")
            self.stats["index_size"] = (
                os.path.getsize(db_file) if os.path.exists(db_file) else 0
            )
            available = self.semantic_available()
            self.stats["semantic_available"] = available
            self.stats["semantic_unavailable_reason"] = (
                None if available else self.semantic_unavailable_reason()
            )
            self.stats.update(self.semantic_index_status())
        except Exception as e:
            logger.warning(f"Failed to refresh search stats from DB: {e}")

    def get_stats(self) -> dict:
        return self.stats

    def semantic_available(self) -> bool:
        backend = self.embedding_model
        if backend is None:
            return False
        if isinstance(backend, UnavailableEmbeddingBackend):
            return False
        return callable(getattr(backend, "encode", None))

    def semantic_unavailable_reason(self) -> str:
        backend = self.embedding_model
        if backend is None:
            return "embedding_model_not_loaded"
        if isinstance(backend, UnavailableEmbeddingBackend):
            status_fn = getattr(backend, "status", None)
            if callable(status_fn):
                try:
                    status = status_fn()
                    return (
                        status.get("error_code")
                        or status.get("error")
                        or "embedding_unavailable"
                    )
                except Exception:
                    return "embedding_unavailable"
            return "embedding_unavailable"
        return ""

    def _dedupe_legacy_path_rows(self) -> int:
        """Drop SQLite chunks whose ``file_path`` contains a backslash.

        Older index runs wrote rows under both ``a/b.py`` *and*
        ``a\\b.py`` shapes on Windows. After path normalization landed
        we keep only the forward-slash variant; this helper strips the
        legacy duplicates on startup so search results stop showing the
        same file twice. Returns the number of rows deleted.
        """
        try:
            cursor = self.vector_store.conn.cursor()
            # SQLite's ``LIKE`` doesn't natively understand backslash —
            # use the ``instr`` builtin, which is stable across platforms
            # and avoids ESCAPE-clause portability issues.
            cursor.execute(
                "SELECT COUNT(*) FROM chunks WHERE instr(file_path, '\\') > 0"
            )
            n = cursor.fetchone()[0] or 0
            if not n:
                return 0
            # Drop the FAISS rows for those chunks first so search() can
            # never surface them again — even before the next reindex.
            cursor.execute(
                "SELECT faiss_id FROM chunks WHERE instr(file_path, '\\') > 0"
            )
            ids = [int(r[0]) for r in cursor.fetchall()]
            if ids:
                import numpy as np
                try:
                    self.vector_store.index.remove_ids(np.array(ids, dtype=np.int64))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("FAISS remove_ids during dedupe failed: %s", exc)
            cursor.execute("DELETE FROM chunks WHERE instr(file_path, '\\') > 0")
            self.vector_store.conn.commit()
            self.vector_store._persist_index()
            logger.info(
                "Dedupe: removed %d legacy backslash-path chunk(s) from index.", n
            )
            return n
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Legacy path dedupe failed: %s", exc)
            return 0

    async def update_file(self, file_path: str) -> None:
        """Parse, chunk, embed, and store a single file"""
        file_path = _norm_path(file_path)
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

        await self.upsert_content(file_path, content)

    async def upsert_content(
        self,
        file_path: str,
        content: str,
        *,
        refresh: bool = True,
        content_hash: Optional[str] = None,
        revision: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> int:
        """Index ``content`` for ``file_path`` without reading from disk.

        Used by the local-agent hybrid mode (Wave 2, W2-2): the agent
        watches a real working tree on the user's machine and pushes
        file bodies up to a remote OmniCode instance that has no
        access to the original tree. Returns the number of chunks
        produced so the agent can log progress / detect no-ops.
        """
        # Canonicalize path early so writes and lookups match across
        # platforms. See _norm_path docstring.
        file_path = _norm_path(file_path)

        # 1. Delete old chunks for this file — including any legacy
        #    backslash-shaped duplicates left over from before path
        #    normalization landed.
        await self.vector_store.delete_by_file(file_path)
        legacy = file_path.replace("/", "\\")
        if legacy != file_path:
            await self.vector_store.delete_by_file(legacy)

        # 2. Extract AST chunks
        language = os.path.splitext(file_path)[1].lstrip(".") or "python"
        chunks = self.chunker.chunk_file(content, file_path, language)

        # 3. Generate embeddings and add to store
        index_metadata: Dict[str, Any] = {}
        if content_hash:
            index_metadata["content_hash"] = content_hash
            index_metadata["snapshot_hash"] = content_hash
        if revision is not None:
            index_metadata["snapshot_revision"] = int(revision)
        if workspace_id:
            index_metadata["workspace_id"] = workspace_id

        self._ensure_embedding_backend()
        if not self.semantic_available():
            logger.warning(
                "Semantic upsert skipped for %s: %s",
                file_path,
                self.semantic_unavailable_reason(),
            )
            if refresh:
                self.refresh_stats()
            return 0
        self.prepare_semantic_index(workspace_id=workspace_id)
        chunks = _limit_semantic_chunks(chunks, index_metadata)

        for chunk in chunks:
            emb = self.embedding_model.encode(chunk.content)
            metadata = {
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "symbol_name": chunk.symbol_name or "",
                "signature": chunk.signature or "",
                "docstring": chunk.docstring or "",
                **index_metadata,
            }
            await self.vector_store.add(
                chunk_id=chunk.chunk_id,
                embedding=emb,
                file_path=file_path,
                chunk_type=chunk.chunk_type,
                content=chunk.content,
                metadata=metadata,
            )
        if chunks:
            self._write_semantic_metadata(
                workspace_id=workspace_id,
                indexed_revision=revision,
            )

        # Refresh stats lazily — caller can request /index/stats afterwards.
        if refresh:
            self.refresh_stats()
        return len(chunks)

    async def upsert_contents(
        self,
        files: list[Any],
        *,
        refresh: bool = True,
    ) -> int:
        """Index many in-memory file bodies with batched embeddings/writes."""
        raw_files = list(files)
        if not raw_files:
            return 0
        self._ensure_embedding_backend()
        if not self.semantic_available():
            logger.warning(
                "Semantic bulk upsert skipped for %d files: %s",
                len(raw_files),
                self.semantic_unavailable_reason(),
            )
            if refresh:
                self.refresh_stats()
            return 0
        batch_workspace_ids = {
            str(metadata.get("workspace_id"))
            for _path, _content, metadata in (
                _normalize_upsert_item(item) for item in raw_files
            )
            if metadata.get("workspace_id")
        }
        batch_workspace_id = (
            next(iter(batch_workspace_ids)) if len(batch_workspace_ids) == 1 else None
        )
        batch_revisions = [
            int(metadata.get("snapshot_revision"))
            for _path, _content, metadata in (
                _normalize_upsert_item(item) for item in raw_files
            )
            if metadata.get("snapshot_revision") is not None
        ]
        batch_indexed_revision = max(batch_revisions) if batch_revisions else None
        self.prepare_semantic_index(workspace_id=batch_workspace_id)

        normalized_files = [
            (_norm_path(path), content, metadata)
            for path, content, metadata in (
                _normalize_upsert_item(item) for item in raw_files
            )
        ]
        upsert_stats = {
            "files_seen": len(normalized_files),
            "files_truncated_by_chunk_limit": 0,
            "chunks_dropped_by_limit": 0,
        }
        delete_paths: list[str] = []
        for file_path, _content, _metadata in normalized_files:
            delete_paths.append(file_path)
            legacy = file_path.replace("/", "\\")
            if legacy != file_path:
                delete_paths.append(legacy)
        delete_many = getattr(self.vector_store, "delete_by_files", None)
        if callable(delete_many):
            await delete_many(delete_paths)
        else:
            for path in delete_paths:
                await self.vector_store.delete_by_file(path)

        chunk_rows = []
        chunk_texts = []
        for file_path, content, index_metadata in normalized_files:
            language = os.path.splitext(file_path)[1].lstrip(".") or "python"
            chunks = self.chunker.chunk_file(content, file_path, language)
            original_chunk_count = len(chunks)
            chunks = _limit_semantic_chunks(chunks, index_metadata)
            if len(chunks) < original_chunk_count:
                upsert_stats["files_truncated_by_chunk_limit"] += 1
                upsert_stats["chunks_dropped_by_limit"] += (
                    original_chunk_count - len(chunks)
                )
            for chunk in chunks:
                chunk_texts.append(chunk.content)
                chunk_rows.append((file_path, chunk, dict(index_metadata)))
        self.last_upsert_stats = upsert_stats

        if not chunk_rows:
            if refresh:
                self.refresh_stats()
            return 0

        embeddings = self.embedding_model.encode(chunk_texts)
        add_items = []
        for idx, (file_path, chunk, index_metadata) in enumerate(chunk_rows):
            add_items.append({
                "chunk_id": chunk.chunk_id,
                "embedding": embeddings[idx],
                "file_path": file_path,
                "chunk_type": chunk.chunk_type,
                "content": chunk.content,
                "metadata": {
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "symbol_name": chunk.symbol_name or "",
                    "signature": chunk.signature or "",
                    "docstring": chunk.docstring or "",
                    **index_metadata,
                },
            })
        add_many = getattr(self.vector_store, "add_many", None)
        if callable(add_many):
            await add_many(add_items)
        else:
            for item in add_items:
                await self.vector_store.add(**item)
        if add_items:
            self._write_semantic_metadata(
                workspace_id=batch_workspace_id,
                indexed_revision=batch_indexed_revision,
            )

        if refresh:
            self.refresh_stats()
        return len(add_items)

    def indexed_file_hashes(
        self,
        *,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Return content hashes already present in the semantic index."""
        hashes: Dict[str, str] = {}
        missing: set[str] = set()
        cursor = self.vector_store.conn.cursor()
        cursor.execute("SELECT file_path, metadata FROM chunks")
        for row in cursor.fetchall():
            path = row["file_path"]
            if not isinstance(path, str) or path in missing:
                continue
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except Exception:
                metadata = {}
            if workspace_id and metadata.get("workspace_id") != workspace_id:
                continue
            content_hash = metadata.get("content_hash") or metadata.get("snapshot_hash")
            if not isinstance(content_hash, str) or not content_hash:
                hashes.pop(path, None)
                missing.add(path)
                continue
            existing = hashes.get(path)
            if existing is not None and existing != content_hash:
                hashes.pop(path, None)
                missing.add(path)
                continue
            hashes[path] = content_hash
        return hashes

    async def delete_file_index(
        self,
        file_path: str,
        *,
        refresh: bool = True,
    ) -> bool:
        """Remove ``file_path`` from the vector store. Returns whether
        anything was actually deleted."""
        file_path = _norm_path(file_path)
        existed = await self.vector_store.delete_by_file(file_path)
        legacy = file_path.replace("/", "\\")
        if legacy != file_path:
            existed = await self.vector_store.delete_by_file(legacy) or existed
        if refresh:
            self.refresh_stats()
        return bool(existed)

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
                norm_change_path = _norm_path(change.path)
                if change.change_type == "deleted":
                    await self.vector_store.delete_by_file(norm_change_path)
                    legacy = norm_change_path.replace("/", "\\")
                    if legacy != norm_change_path:
                        await self.vector_store.delete_by_file(legacy)
                    tracker.mark_deleted(norm_change_path)
                    logger.debug(f"Removed from index: {norm_change_path}")
                else:
                    # new or modified — re-index
                    await self.update_file(norm_change_path)
                    tracker.mark_indexed(
                        self.working_dir, norm_change_path, change.content_hash
                    )
            except Exception as e:
                logger.warning(f"Failed to index {change.path}: {e}")

        # Recalculate stats
        self.refresh_stats()
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
            ".md": "markdown", ".markdown": "markdown",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".sh": "shell", ".bash": "shell",
            ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".cs": "csharp",
        }.get(ext, "text")

    async def search(self, request: SearchRequest) -> List[LegacySearchResult]:
        """Execute hybrid RRF search or text search and map to legacy SearchResult model"""
        logger.info(f"Executing search: query='{request.query}', type='{request.search_type}'")

        # ``file_pattern`` is a comma-separated glob list. We over-fetch
        # at the SQL level and apply fnmatch on the way out so every
        # search type ends up filtered consistently.
        glob_list: List[str] = []
        if request.file_pattern:
            glob_list = [
                g.strip() for g in str(request.file_pattern).split(",") if g.strip()
            ]

        def _glob_ok(file_path: str) -> bool:
            if not glob_list:
                return True
            return _matches_any_glob(file_path, glob_list)

        # Semantic search uses metadata_filter only when a single glob
        # is passed and it's an exact path; otherwise we filter in
        # Python after FAISS recall. The previous behaviour silently
        # truncated to ``glob_list[0]`` and treated it as an exact
        # ``file_path`` match — which dropped almost every result.
        metadata_filter = None
        if len(glob_list) == 1 and "*" not in glob_list[0] and "?" not in glob_list[0]:
            metadata_filter = {"file_path": _norm_path(glob_list[0])}

        results = []

        if request.search_type == "text":
            # Simple text scanning in SQLite chunks
            cursor = self.vector_store.conn.cursor()
            cursor.execute("SELECT file_path, content FROM chunks WHERE content LIKE ?", (f"%{request.query}%",))
            rows = cursor.fetchall()

            for row in rows:
                if not _glob_ok(row["file_path"]):
                    continue
                results.append(LegacySearchResult(
                    file_path=row["file_path"],
                    symbol_name="",
                    chunk_type=row["content"][:200],  # matched content context
                    line_start=1,
                    line_end=1,
                    signature="",
                    docstring="",
                    relevance_score=1.0,
                    why_matched=["text"],
                ))
                if len(results) >= request.max_results:
                    break
        elif request.search_type in ("symbol", "symbol_exact", "fuzzy_symbol"):
            # Symbol search — match the literal symbol name stored in
            # ``metadata.symbol_name`` (extracted at indexing time by the
            # AST chunker). Uses RapidFuzz for fuzzy mode so 1-2 char
            # typos still recall (e.g. "creat_app" -> "create_app").
            cursor = self.vector_store.conn.cursor()
            q = request.query.strip()
            if not q:
                return results

            fuzzy = request.search_type != "symbol_exact"

            # Optional symbol-type filter (function / class / method / ...).
            sql_extra = ""
            sql_params: List[Any] = []
            if getattr(request, "symbol_type", None):
                sql_extra = " WHERE chunk_type = ?"
                sql_params.append(request.symbol_type)

            cursor.execute(
                f"""
                SELECT file_path, chunk_type, content, metadata
                FROM chunks
                {sql_extra}
                """,
                sql_params,
            )
            rows = cursor.fetchall()

            ql = q.lower()
            scored = []

            # RapidFuzz lets a 1-2 char edit-distance still surface, which
            # the previous LIKE-based scorer dropped to score=0. Fall back
            # to substring matching when rapidfuzz isn't installed so the
            # tool keeps working on minimal deployments.
            try:
                from rapidfuzz import fuzz as _rf_fuzz
                _have_rapidfuzz = True
            except Exception:  # pragma: no cover - defensive
                _rf_fuzz = None
                _have_rapidfuzz = False

            min_score = float(request.min_score or 0.0)

            for row in rows:
                # Glob filter early — saves JSON parse on rows we'd drop.
                if not _glob_ok(row["file_path"]):
                    continue
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                except Exception:
                    meta = {}
                name = (meta.get("symbol_name") or "").strip()
                if not name:
                    continue
                nl = name.lower()

                # Tier 1: hard literal matches keep the deterministic
                # 1.0 / 0.9 / 0.7 scoring so behaviour stays predictable
                # for "exact" mode and unit tests.
                if nl == ql:
                    score = 1.0
                elif nl.startswith(ql):
                    score = 0.9
                elif ql in nl:
                    score = 0.7
                else:
                    if not fuzzy:
                        continue
                    # Tier 2 (fuzzy only): RapidFuzz token-set ratio so
                    # 1-2 char typos still rank near 0.6-0.8.
                    if _have_rapidfuzz:
                        ratio = _rf_fuzz.WRatio(ql, nl)
                        # Map 0-100 -> 0.0-0.95 with a 50 floor so
                        # marginal matches don't pollute the result list.
                        if ratio < 60:
                            continue
                        score = min(0.95, 0.5 + (ratio - 60) / 100)
                    else:
                        # No rapidfuzz: only fall back when query is a
                        # short prefix of a token inside the symbol.
                        tokens = nl.replace("_", " ").replace(".", " ").split()
                        if any(t.startswith(ql) for t in tokens):
                            score = 0.55
                        else:
                            continue

                if score < min_score:
                    continue
                scored.append((score, row, meta, name))

            # Highest scoring matches first
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, row, meta, name in scored[: request.max_results]:
                # Tag *why* this row matched so AI editors can decide which
                # signal to trust. Exact symbol hits are the strongest;
                # prefix/contains/fuzzy matches get more conservative tags.
                if score >= 1.0:
                    why = ["symbol:exact"]
                elif score >= 0.9:
                    why = ["symbol:prefix"]
                elif score >= 0.7:
                    why = ["symbol:contains"]
                else:
                    why = ["symbol:fuzzy"]
                    if _have_rapidfuzz:
                        why.append("rapidfuzz")
                results.append(LegacySearchResult(
                    file_path=row["file_path"],
                    symbol_name=name,
                    chunk_type=row["chunk_type"],
                    line_start=meta.get("start_line", 1),
                    line_end=meta.get("end_line", 1),
                    signature=meta.get("signature", ""),
                    docstring=meta.get("docstring", ""),
                    relevance_score=score,
                    why_matched=why,
                ))
        else:
            # Semantic / Hybrid search using RRF
            semantic_status = self.semantic_index_status()
            if not semantic_status.get("semantic_index_ready"):
                reason = (
                    semantic_status.get("semantic_index_stale_reason")
                    or "semantic index is not ready"
                )
                raise RuntimeError(f"SEMANTIC_INDEX_NOT_READY: {reason}")
            query_emb = self.embedding_model.encode(request.query)
            # Over-fetch when we'll be Python-side glob filtering so the
            # result list isn't suspiciously short on a narrow pattern.
            top_k = request.max_results
            fetch_k = top_k * 5 if (glob_list and metadata_filter is None) else top_k
            hybrid_results = await self.vector_store.search(
                query_emb,
                top_k=fetch_k,
                metadata_filter=metadata_filter,
            )

            for item in hybrid_results:
                if not _glob_ok(item.get("file_path", "")):
                    continue
                meta = item.get("metadata", {})
                # Hybrid RRF can be entered via several recall paths; we
                # keep ``semantic`` as the base tag and append a stronger
                # signal whenever the symbol_name (when present) actually
                # matches the query verbatim.
                why = ["semantic"]
                sym = (meta.get("symbol_name") or "").lower()
                if sym and request.query.lower() in sym:
                    why.append("symbol:contains")
                if sym and sym == request.query.lower():
                    why.append("symbol:exact")
                results.append(LegacySearchResult(
                    file_path=item["file_path"],
                    symbol_name=meta.get("symbol_name", ""),
                    chunk_type=item["chunk_type"],
                    line_start=meta.get("start_line", 1),
                    line_end=meta.get("end_line", 1),
                    signature=meta.get("signature", ""),
                    docstring=meta.get("docstring", ""),
                    relevance_score=item["score"],
                    why_matched=why,
                ))
                if len(results) >= top_k:
                    break

            # Optional cross-encoder reranker (Wave 2 W2-9). Toggle via
            # ``OMNICODE_RERANKER=true``. The reranker is responsible for
            # tagging promoted items with ``"reranked"`` itself.
            try:
                from omnicode_core.search.reranker import get_reranker

                results = get_reranker().rerank(request.query, results)
            except Exception as exc:
                logger.debug("Reranker pass skipped: %s", exc)

        return results



# ---------------------------------------------------------------------------
# Backwards-compat alias.  Older callers import ``SearchEngine``; the public
# class is now ``SemanticSearchEngine``.
# ---------------------------------------------------------------------------
SearchEngine = SemanticSearchEngine
