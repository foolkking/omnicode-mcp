from typing import List, Dict, Any, Optional
import faiss
import numpy as np
import sqlite3
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class VectorStore:
    """
    Vector store supporting metadata filtering and clean updates.
    """
    def __init__(self, db_path: str, dimension: int = 384):
        self.dimension = dimension
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Use IndexIDMap to support deletion/updates in FAISS
        self.index = faiss.IndexIDMap(faiss.IndexFlatIP(dimension))
        self._init_db()

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
        self.conn.commit()

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
        INSERT OR REPLACE INTO chunks (faiss_id, chunk_id, file_path, chunk_type, content, metadata)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (faiss_id, chunk_id, file_path, chunk_type, content, json.dumps(metadata or {})))
        self.conn.commit()

    async def delete_by_file(self, file_path: str):
        """Properly delete chunks associated with a file from both SQLite and FAISS"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT faiss_id FROM chunks WHERE file_path = ?', (file_path,))
        rows = cursor.fetchall()
        
        if not rows:
            return
            
        ids_to_remove = [row[0] for row in rows]
        
        # Remove from FAISS
        id_array = np.array(ids_to_remove, dtype=np.int64)
        self.index.remove_ids(id_array)
        
        # Remove from SQLite
        cursor.execute('DELETE FROM chunks WHERE file_path = ?', (file_path,))
        self.conn.commit()

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
        
        for dist, faiss_id in zip(distances[0], indices[0]):
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
