import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import faiss
import numpy as np

logger = logging.getLogger(__name__)

class VectorStore:
    """
    Vector store supporting metadata filtering and clean updates.
    """
    def __init__(self, db_path: str, dimension: int = 384):
        self.dimension = dimension
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # FAISS index is persisted next to the SQLite metadata so semantic
        # search survives process restarts.  Without this, ``index.ntotal``
        # stays at 0 after a reboot and every semantic query returns [].
        self.faiss_path = self.db_path.with_suffix(".faiss")

        # Use IndexIDMap to support deletion/updates in FAISS
        if self.faiss_path.exists():
            try:
                self.index = faiss.read_index(str(self.faiss_path))
                logger.info(
                    "Loaded FAISS index from %s (ntotal=%d)",
                    self.faiss_path, self.index.ntotal,
                )
            except Exception as exc:
                logger.warning("Could not read FAISS index %s: %s — rebuilding empty", self.faiss_path, exc)
                self.index = faiss.IndexIDMap(faiss.IndexFlatIP(dimension))
        else:
            self.index = faiss.IndexIDMap(faiss.IndexFlatIP(dimension))
        self._init_db()

    def _persist_index(self) -> None:
        """Flush the FAISS index to disk so semantic search survives restarts."""
        try:
            faiss.write_index(self.index, str(self.faiss_path))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to persist FAISS index to %s: %s", self.faiss_path, exc)

    def close(self) -> None:
        """Close the SQLite handle owned by this store."""
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def replace_from(self, staging: "VectorStore") -> Dict[str, Any]:
        """Atomically replace this store from a completed staging store.

        Semantic full rebuilds can take minutes on large repositories. Build
        them away from the active store, then hold the active lock only for the
        final SQLite/FAISS swap so queries never observe a half-built index.
        """

        if staging is self:
            raise ValueError("staging vector store must differ from active store")
        target_dimension = staging.index_dimension()
        db_tmp = Path(f"{self.db_path}.activate.tmp")
        faiss_tmp = Path(f"{self.faiss_path}.activate.tmp")
        with self._lock, staging._lock:
            staging.conn.commit()
            staging._persist_index()
            if db_tmp.exists():
                db_tmp.unlink()
            if faiss_tmp.exists():
                faiss_tmp.unlink()

            backup_conn = sqlite3.connect(db_tmp)
            try:
                staging.conn.backup(backup_conn)
                backup_conn.commit()
            finally:
                backup_conn.close()
            faiss.write_index(staging.index, str(faiss_tmp))

            self.conn.close()
            try:
                os.replace(db_tmp, self.db_path)
                os.replace(faiss_tmp, self.faiss_path)
            finally:
                if db_tmp.exists():
                    db_tmp.unlink()
                if faiss_tmp.exists():
                    faiss_tmp.unlink()

            self.dimension = int(target_dimension)
            self.index = faiss.read_index(str(self.faiss_path))
            self._init_db()
            return {
                "activated": True,
                "dimension": self.index_dimension(),
                "vector_count": int(self.index.ntotal),
                "db_path": str(self.db_path),
            }

    def index_dimension(self) -> int:
        """Return the dimension expected by the mounted FAISS index."""
        return int(getattr(self.index, "d", self.dimension))

    def reset_index(self, *, dimension: int, clear_metadata: bool = True) -> None:
        """Clear semantic vectors and recreate FAISS for ``dimension``.

        This is intentionally explicit and is used only by a requested
        semantic rebuild. A model switch must never silently discard an
        existing vector index during ordinary startup or query handling.
        """
        target = int(dimension)
        if target <= 0:
            raise ValueError("semantic index dimension must be positive")
        with self._lock:
            self.dimension = target
            self.index = faiss.IndexIDMap(faiss.IndexFlatIP(target))
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM chunks")
            if clear_metadata:
                cursor.execute("DELETE FROM index_meta")
            self.conn.commit()
            self._persist_index()

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        cursor = self.conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chunks (
            faiss_id INTEGER PRIMARY KEY,
            chunk_id TEXT UNIQUE,
            file_path TEXT,
            chunk_type TEXT,
            content TEXT,
            metadata JSON
        )
        ''')
        # Track FAISS embeddings as a BLOB column so we can rebuild the
        # in-memory ANN index from disk on startup.  Older databases that
        # were created without this column will get it migrated in here.
        cursor.execute("PRAGMA table_info(chunks)")
        cols = {row[1] for row in cursor.fetchall()}
        if "embedding" not in cols:
            cursor.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        ''')
        self.conn.commit()

        # Self-heal: if SQLite has chunks but the FAISS index doesn't, try
        # rebuilding from the embedding BLOB column.  When that column is
        # also empty (e.g. legacy DBs from before the BLOB was added) we
        # leave a clear log line so the operator runs `/search/index`.
        cursor.execute("SELECT COUNT(*) FROM chunks")
        chunk_count = cursor.fetchone()[0]
        if chunk_count > 0 and self.index.ntotal == 0:
            cursor.execute("SELECT faiss_id, embedding FROM chunks WHERE embedding IS NOT NULL")
            rows = cursor.fetchall()
            rebuilt = 0
            if rows:
                ids = []
                vecs = []
                for r in rows:
                    try:
                        vec = np.frombuffer(r["embedding"], dtype=np.float32)
                        if vec.size != self.dimension:
                            continue
                        ids.append(int(r["faiss_id"]))
                        vecs.append(vec)
                    except Exception:
                        continue
                if vecs:
                    arr = np.vstack(vecs).astype(np.float32)
                    faiss.normalize_L2(arr)
                    self.index.add_with_ids(arr, np.array(ids, dtype=np.int64))
                    rebuilt = len(vecs)
                    self._persist_index()
            if rebuilt:
                logger.info("Rebuilt FAISS index from %d stored embeddings", rebuilt)
            else:
                logger.warning(
                    "%d chunks in %s but no on-disk FAISS index — semantic search "
                    "will return 0 results until you POST /search/index.",
                    chunk_count, self.db_path,
                )

    def _get_next_faiss_id(self) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT MAX(faiss_id) FROM chunks')
            result = cursor.fetchone()[0]
            return (result or 0) + 1

    async def add(self, chunk_id: str, embedding: np.ndarray, file_path: str, chunk_type: str, content: str, metadata: Dict[str, Any] = None):
        """Add a chunk to the store"""
        with self._lock:
            faiss_id = self._get_next_faiss_id()

            # Ensure embedding is 2D and float32
            if len(embedding.shape) == 1:
                embedding = embedding.reshape(1, -1)
            embedding = embedding.astype(np.float32)
            faiss.normalize_L2(embedding)

            # Add to FAISS
            id_array = np.array([faiss_id], dtype=np.int64)
            self.index.add_with_ids(embedding, id_array)

            # Add to SQLite
            cursor = self.conn.cursor()
            cursor.execute('''
            INSERT OR REPLACE INTO chunks (faiss_id, chunk_id, file_path, chunk_type, content, metadata, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                faiss_id, chunk_id, file_path, chunk_type, content,
                json.dumps(metadata or {}),
                embedding.tobytes(),  # 384 * float32 = 1536 bytes
            ))
            self.conn.commit()
            self._persist_index()

    async def add_many(self, items: List[Dict[str, Any]]) -> int:
        """Add many chunks with one SQLite commit and one FAISS persist."""
        if not items:
            return 0
        with self._lock:
            start_id = self._get_next_faiss_id()
            embeddings = []
            rows = []
            for offset, item in enumerate(items):
                embedding = item["embedding"]
                if len(embedding.shape) == 2:
                    embedding = embedding.reshape(-1)
                embeddings.append(embedding.astype(np.float32))
                rows.append((
                    start_id + offset,
                    item["chunk_id"],
                    item["file_path"],
                    item["chunk_type"],
                    item["content"],
                    json.dumps(item.get("metadata") or {}),
                ))

            matrix = np.vstack(embeddings).astype(np.float32)
            faiss.normalize_L2(matrix)
            id_array = np.arange(start_id, start_id + len(items), dtype=np.int64)
            self.index.add_with_ids(matrix, id_array)

            cursor = self.conn.cursor()
            cursor.executemany('''
            INSERT OR REPLACE INTO chunks (faiss_id, chunk_id, file_path, chunk_type, content, metadata, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', [
                row + (matrix[idx].tobytes(),)
                for idx, row in enumerate(rows)
            ])
            self.conn.commit()
            self._persist_index()
            return len(items)

    async def delete_by_file(self, file_path: str) -> int:
        """Delete every chunk associated with ``file_path``.

        Returns the number of chunks removed, so callers (most usefully
        the local-agent ``/index/delete-file`` endpoint) can detect a
        no-op and avoid persisting an empty FAISS write.
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT faiss_id FROM chunks WHERE file_path = ?', (file_path,))
            rows = cursor.fetchall()

            if not rows:
                return 0

            ids_to_remove = [row[0] for row in rows]

            # Remove from FAISS
            id_array = np.array(ids_to_remove, dtype=np.int64)
            self.index.remove_ids(id_array)

            # Remove from SQLite
            cursor.execute('DELETE FROM chunks WHERE file_path = ?', (file_path,))
            self.conn.commit()
            self._persist_index()
            return len(ids_to_remove)

    async def delete_by_files(self, file_paths: List[str]) -> int:
        """Delete chunks for many files with one SQLite commit/FAISS persist."""
        paths = list(dict.fromkeys(file_paths))
        if not paths:
            return 0
        with self._lock:
            cursor = self.conn.cursor()
            placeholders = ",".join("?" for _path in paths)
            cursor.execute(
                f"SELECT faiss_id FROM chunks WHERE file_path IN ({placeholders})",
                paths,
            )
            rows = cursor.fetchall()
            if not rows:
                return 0

            ids_to_remove = [row[0] for row in rows]
            id_array = np.array(ids_to_remove, dtype=np.int64)
            self.index.remove_ids(id_array)
            cursor.execute(
                f"DELETE FROM chunks WHERE file_path IN ({placeholders})",
                paths,
            )
            self.conn.commit()
            self._persist_index()
            return len(ids_to_remove)

    async def search(self, query_embedding: np.ndarray, top_k: int = 10, metadata_filter: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Search with post-filtering"""
        with self._lock:
            if self.index.ntotal == 0:
                return []

            if len(query_embedding.shape) == 1:
                query_embedding = query_embedding.reshape(1, -1)
            query_embedding = query_embedding.astype(np.float32)
            faiss.normalize_L2(query_embedding)

            # Fetch more if we are filtering
            fetch_k = top_k * 5 if metadata_filter else top_k
            distances, indices = self.index.search(query_embedding, min(fetch_k, self.index.ntotal))

            results = []
            cursor = self.conn.cursor()

            for dist, faiss_id in zip(distances[0], indices[0], strict=False):
                if faiss_id == -1:
                    continue

                cursor.execute('SELECT chunk_id, file_path, content, chunk_type, metadata FROM chunks WHERE faiss_id = ?', (int(faiss_id),))
                row = cursor.fetchone()

                if row:
                    # Apply metadata filtering
                    meta = json.loads(row['metadata'])
                    if metadata_filter:
                        match = True
                        for k, v in metadata_filter.items():
                            # Example: simple equality check
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
                        "score": float(dist),
                        "metadata": meta
                    })

                    if len(results) >= top_k:
                        break

            return results

    def set_index_metadata(
        self,
        *,
        embedding_model: str,
        embedding_dimension: int | None,
        embedding_backend: str,
        embedding_revision: str | None = None,
        chunker_version: str = "unknown",
        normalization: str = "l2",
        workspace_id: str | None = None,
        indexed_revision: int | None = None,
    ) -> Dict[str, Any]:
        """Persist semantic-index metadata tied to the embedding model."""
        metadata = {
            "embedding_model": embedding_model,
            "embedding_revision": embedding_revision,
            "embedding_dimension": embedding_dimension,
            "embedding_backend": embedding_backend,
            "chunker_version": chunker_version,
            "normalization": normalization,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workspace_id": workspace_id,
            "indexed_revision": indexed_revision,
        }
        with self._lock:
            cursor = self.conn.cursor()
            cursor.executemany(
                "INSERT OR REPLACE INTO index_meta(key, value) VALUES(?, ?)",
                [(key, json.dumps(value)) for key, value in metadata.items()],
            )
            self.conn.commit()
        return metadata

    def get_index_metadata(self) -> Dict[str, Any]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT key, value FROM index_meta")
            rows = cursor.fetchall()
        out: Dict[str, Any] = {}
        for row in rows:
            try:
                out[str(row["key"])] = json.loads(row["value"])
            except Exception:
                out[str(row["key"])] = row["value"]
        return out

    def semantic_metadata_status(
        self,
        *,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
        embedding_dimension: int | None = None,
        chunker_version: str | None = None,
        minimum_indexed_revision: int | None = None,
    ) -> Dict[str, Any]:
        metadata = self.get_index_metadata()
        stale_reasons: List[str] = []
        invalid_reasons: List[str] = []
        mounted_dimension = self.index_dimension()
        if embedding_dimension and mounted_dimension != int(embedding_dimension):
            invalid_reasons.append("faiss_dimension_mismatch")
        if embedding_model and metadata.get("embedding_model") not in {
            None,
            embedding_model,
        }:
            stale_reasons.append("embedding_model_mismatch")
        if embedding_revision and metadata.get("embedding_revision") not in {
            None,
            embedding_revision,
        }:
            stale_reasons.append("embedding_revision_mismatch")
        if embedding_dimension and metadata.get("embedding_dimension") not in {
            None,
            embedding_dimension,
        }:
            invalid_reasons.append("embedding_dimension_mismatch")
        if chunker_version and metadata.get("chunker_version") not in {
            None,
            chunker_version,
        }:
            stale_reasons.append("chunker_version_mismatch")
        if (
            minimum_indexed_revision is not None
            and int(metadata.get("indexed_revision") or 0)
            < int(minimum_indexed_revision)
        ):
            stale_reasons.append("indexed_revision_behind")
        ready = bool(self.index.ntotal > 0 and metadata)
        stale_reason = (
            ";".join(invalid_reasons)
            if invalid_reasons
            else ";".join(stale_reasons)
            if stale_reasons
            else None
        )
        return {
            "semantic_index_ready": ready and not stale_reasons and not invalid_reasons,
            "semantic_index_model": metadata.get("embedding_model"),
            "semantic_index_dimension": metadata.get("embedding_dimension"),
            "faiss_dimension": mounted_dimension,
            "semantic_index_stale_reason": stale_reason,
            "semantic_index_invalid": bool(invalid_reasons),
            "semantic_index_stale": bool(stale_reasons),
            "chunker_version": metadata.get("chunker_version"),
            "workspace_id": metadata.get("workspace_id"),
            "indexed_revision": metadata.get("indexed_revision"),
            "vector_count": int(self.index.ntotal),
            "metadata": metadata,
        }
