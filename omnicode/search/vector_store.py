import json
import logging
import sqlite3
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

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path)
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
        cursor = self.conn.cursor()
        cursor.execute('SELECT MAX(faiss_id) FROM chunks')
        result = cursor.fetchone()[0]
        return (result or 0) + 1

    async def add(self, chunk_id: str, embedding: np.ndarray, file_path: str, chunk_type: str, content: str, metadata: Dict[str, Any] = None):
        """Add a chunk to the store"""
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

    async def delete_by_file(self, file_path: str) -> int:
        """Delete every chunk associated with ``file_path``.

        Returns the number of chunks removed, so callers (most usefully
        the local-agent ``/index/delete-file`` endpoint) can detect a
        no-op and avoid persisting an empty FAISS write.
        """
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

    async def search(self, query_embedding: np.ndarray, top_k: int = 10, metadata_filter: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Search with post-filtering"""
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
