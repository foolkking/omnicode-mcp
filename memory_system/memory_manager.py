"""Memory management system with SQLite storage and semantic search"""

import sqlite3
import json
import uuid
import hashlib
import re as _re
import numpy as np
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sentence_transformers import SentenceTransformer
from enum import Enum

from .models import (
    Memory,
    MemoryRequest,
    MemorySearchRequest,
    MemoryResult,
    MemoryStats,
    ContextSummary,
    MemoryCategory,
    MemoryImportance,
)


class MemoryManager:
    """Manages Claude's memories with SQLite storage and semantic search"""

    def __init__(self, db_path: str = ".data"):
        self.db_path = Path(db_path)
        self.db_path.mkdir(exist_ok=True)

        # Use same database as semantic search
        self.metadata_db = self.db_path / "metadata.db"

        # Initialize embedding model (same as semantic search)
        self.embedding_model = None
        self.embedding_dimension = 384  # all-MiniLM-L6-v2

        # Initialize database
        self._init_database()

    async def initialize(self):
        """Async initialization for embedding model"""
        if self.embedding_model is None:
            # Load same model as semantic search for consistency
            self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

    def _init_database(self):
        """Initialize memory tables in SQLite"""
        conn = sqlite3.connect(self.metadata_db)

        # Create memories table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                subcategory TEXT,
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 3,
                
                -- Metadata
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                session_id TEXT,
                tags_json TEXT,  -- JSON array of tags
                context_json TEXT,  -- JSON object
                related_files_json TEXT,  -- JSON array
                
                -- Status
                status TEXT DEFAULT 'active',
                verified BOOLEAN DEFAULT FALSE,
                
                -- Embeddings for semantic search
                embedding_vector BLOB,

                -- Dedup + popularity tracking
                content_fingerprint TEXT,
                access_count INTEGER DEFAULT 1,
                last_accessed DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Migration: add columns when upgrading from older schemas.
        cur = conn.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in cur.fetchall()}
        if "content_fingerprint" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN content_fingerprint TEXT")
        if "access_count" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 1")
        if "last_accessed" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN last_accessed DATETIME")

        # Backfill: compute fingerprints for any rows without one.
        # SHA-1 in pure SQL isn't available, so we do this in Python.
        cursor = conn.execute(
            "SELECT id, category, content FROM memories "
            "WHERE content_fingerprint IS NULL OR content_fingerprint = ''"
        )
        rows = cursor.fetchall()
        for mid, cat, content in rows:
            fp = self._content_fingerprint(cat, content or "")
            conn.execute(
                "UPDATE memories SET content_fingerprint = ? WHERE id = ?",
                (fp, mid),
            )
        if rows:
            conn.commit()

        # Create indexes for fast queries
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
            CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
            CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
            CREATE INDEX IF NOT EXISTS idx_memories_fingerprint
                ON memories(content_fingerprint);
        """
        )

        # Create memory sessions table for context tracking
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_sessions (
                id TEXT PRIMARY KEY,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                end_time DATETIME,
                focus_area TEXT,
                achievements TEXT,
                created_memories INTEGER DEFAULT 0
            )
        """
        )

        conn.commit()
        conn.close()

    def _serialize_list(self, items: List) -> str:
        """Serialize list to JSON string"""
        return json.dumps(items) if items else "[]"

    def _deserialize_list(self, json_str: Optional[str]) -> List:
        """Deserialize JSON string to list"""
        if not json_str:
            return []
        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            return []

    def _serialize_dict(self, data: Optional[Dict]) -> str:
        """Serialize dict to JSON string"""
        return json.dumps(data) if data else "{}"

    def _deserialize_dict(self, json_str: Optional[str]) -> Dict:
        """Deserialize JSON string to dict"""
        if not json_str:
            return {}
        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _serialize_embedding(self, embedding: np.ndarray) -> bytes:
        """Serialize embedding vector to bytes"""
        return embedding.astype(np.float32).tobytes()

    def _deserialize_embedding(self, blob: bytes) -> np.ndarray:
        """Deserialize bytes to embedding vector"""
        return np.frombuffer(blob, dtype=np.float32)

    def _memory_from_row(self, row: tuple) -> Memory:
        """Convert database row to Memory object.

        Tolerates the older 14-column layout as well as the newer 17-column
        layout that adds (content_fingerprint, access_count, last_accessed).
        """
        # The first 14 fields are stable across schemas
        (
            id_,
            category,
            subcategory,
            content,
            importance,
            timestamp,
            session_id,
            tags_json,
            context_json,
            related_files_json,
            status,
            verified,
            embedding_blob,
        ) = row[:13]

        # Parse timestamp
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        # Deserialize JSON fields
        tags = self._deserialize_list(tags_json)
        context = self._deserialize_dict(context_json)
        related_files = self._deserialize_list(related_files_json)

        # Deserialize embedding if present
        embedding_vector = None
        if embedding_blob:
            embedding_array = self._deserialize_embedding(embedding_blob)
            embedding_vector = embedding_array.tolist()

        # Optional newer columns (None when row predates the migration)
        access_count = row[14] if len(row) > 14 else 1
        last_accessed = row[15] if len(row) > 15 else None

        mem = Memory(
            id=id_,
            category=MemoryCategory(category),
            subcategory=subcategory,
            content=content,
            importance=MemoryImportance(importance),
            timestamp=timestamp,
            session_id=session_id,
            tags=tags,
            context=context,
            related_files=related_files,
            status=status,
            verified=verified,
            embedding_vector=embedding_vector,
        )
        # Attach the popularity fields as plain attributes so callers
        # that handle the new schema can read them; older code paths
        # ignore unknown attrs and keep working.
        try:
            mem.access_count = access_count or 1
            mem.last_accessed = last_accessed
        except Exception:
            pass
        return mem

    @staticmethod
    def _content_fingerprint(category, content: str) -> str:
        """Compute a stable fingerprint for dedup.

        Normalises whitespace and case so trivially-different copies
        ("Edit failed" vs "Edit failed.") collapse to the same key.
        """
        cat = getattr(category, "value", None) or str(category or "")
        normalized = _re.sub(r"\s+", " ", (content or "")).strip().lower()
        h = hashlib.sha1(f"{cat}\x00{normalized}".encode("utf-8")).hexdigest()
        return h

    async def store_memory(self, request: MemoryRequest) -> Memory:
        """Store a new memory — or, if an identical memory already exists
        in the same category, increment its access_count instead of
        creating a duplicate row.

        This collapses the "many copies of the same error" problem
        without losing useful signal: high-frequency memories surface
        first because we sort by access_count + importance.
        """
        if not self.embedding_model:
            await self.initialize()

        fingerprint = self._content_fingerprint(request.category, request.content)
        conn = sqlite3.connect(self.metadata_db)
        try:
            # Look for an existing active memory with the same fingerprint
            cursor = conn.execute(
                """
                SELECT id FROM memories
                WHERE content_fingerprint = ? AND status = 'active'
                ORDER BY id ASC LIMIT 1
                """,
                (fingerprint,),
            )
            existing = cursor.fetchone()
            if existing:
                memory_id = existing[0]
                # Bump counters + take the higher importance + merge tags
                cursor = conn.execute(
                    "SELECT importance, tags_json, related_files_json FROM memories WHERE id = ?",
                    (memory_id,),
                )
                row = cursor.fetchone()
                old_imp = row[0] or 0
                new_imp = max(old_imp, int(getattr(request.importance, "value", request.importance) or 0))
                merged_tags = self._merge_lists(
                    self._deserialize_list(row[1]), request.tags
                )
                merged_files = self._merge_lists(
                    self._deserialize_list(row[2]), request.related_files
                )
                conn.execute(
                    """
                    UPDATE memories
                    SET access_count = COALESCE(access_count, 1) + 1,
                        last_accessed = CURRENT_TIMESTAMP,
                        importance = ?,
                        tags_json = ?,
                        related_files_json = ?
                    WHERE id = ?
                    """,
                    (
                        new_imp,
                        self._serialize_list(merged_tags),
                        self._serialize_list(merged_files),
                        memory_id,
                    ),
                )
                conn.commit()
                cursor = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (memory_id,)
                )
                row = cursor.fetchone()
                return self._memory_from_row(row)

            # No duplicate — insert as usual
            embedding = self.embedding_model.encode(request.content)  # type:ignore
            embedding_blob = self._serialize_embedding(embedding)  # type:ignore
            session_id = request.session_id or str(uuid.uuid4())
            cursor = conn.execute(
                """
                INSERT INTO memories
                (category, subcategory, content, importance, session_id,
                 tags_json, context_json, related_files_json, embedding_vector,
                 content_fingerprint, access_count, last_accessed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
                (
                    request.category.value,
                    request.subcategory,
                    request.content,
                    request.importance.value,
                    session_id,
                    self._serialize_list(request.tags),
                    self._serialize_dict(request.context),
                    self._serialize_list(request.related_files),
                    embedding_blob,
                    fingerprint,
                ),
            )
            memory_id = cursor.lastrowid
            conn.commit()
            cursor = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
            row = cursor.fetchone()
            return self._memory_from_row(row)
        finally:
            conn.close()

    @staticmethod
    def _merge_lists(a, b):
        """Merge two lists preserving order, dropping duplicates."""
        seen = set()
        out = []
        for src in (a or [], b or []):
            for item in src:
                if item not in seen:
                    seen.add(item)
                    out.append(item)
        return out

    def dedupe_existing(self) -> Dict[str, int]:
        """One-shot pass that collapses duplicate active memories.

        Groups rows by ``content_fingerprint``; for each group with more
        than one row, keeps the oldest (lowest id), sums access counts
        from the others into it, merges tags + related_files, and marks
        the duplicates as ``status='archived'``.

        Returns ``{kept, archived, groups}``.
        """
        conn = sqlite3.connect(self.metadata_db)
        try:
            cursor = conn.execute(
                """
                SELECT content_fingerprint, COUNT(*)
                FROM memories
                WHERE status = 'active' AND content_fingerprint IS NOT NULL
                  AND content_fingerprint != ''
                GROUP BY content_fingerprint
                HAVING COUNT(*) > 1
                """
            )
            dup_groups = cursor.fetchall()

            archived = 0
            for fp, _count in dup_groups:
                cursor = conn.execute(
                    """
                    SELECT id, importance, tags_json, related_files_json,
                           COALESCE(access_count, 1)
                    FROM memories
                    WHERE content_fingerprint = ? AND status = 'active'
                    ORDER BY id ASC
                    """,
                    (fp,),
                )
                rows = cursor.fetchall()
                if len(rows) <= 1:
                    continue
                keep_id = rows[0][0]
                keep_imp = rows[0][1] or 0
                keep_tags = self._deserialize_list(rows[0][2])
                keep_files = self._deserialize_list(rows[0][3])
                total_access = 0
                for rid, imp, tags_j, files_j, ac in rows:
                    total_access += int(ac or 1)
                    if (imp or 0) > keep_imp:
                        keep_imp = imp
                    keep_tags = self._merge_lists(keep_tags, self._deserialize_list(tags_j))
                    keep_files = self._merge_lists(keep_files, self._deserialize_list(files_j))
                conn.execute(
                    """
                    UPDATE memories SET access_count = ?, importance = ?,
                        tags_json = ?, related_files_json = ?,
                        last_accessed = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        total_access,
                        keep_imp,
                        self._serialize_list(keep_tags),
                        self._serialize_list(keep_files),
                        keep_id,
                    ),
                )
                # Archive the others
                others = [str(r[0]) for r in rows[1:]]
                placeholders = ",".join("?" * len(others))
                conn.execute(
                    f"UPDATE memories SET status = 'archived' "
                    f"WHERE id IN ({placeholders})",
                    others,
                )
                archived += len(others)
            conn.commit()
            return {
                "groups": len(dup_groups),
                "archived": archived,
                "kept": len(dup_groups),
            }
        finally:
            conn.close()

    async def search_memories_advanced(self, request: MemorySearchRequest) -> Dict:
        """Variant returning a plain dict for the /memory/list endpoint.

        Returns ``{"memories": [...]}``.  Used by the UI's "All Memories"
        browser to get JSON-friendly rows including the new
        ``access_count`` / ``last_accessed`` columns.
        """
        results = await self.search_memories(request)
        out = []
        for r in results:
            mem = r.memory
            d = {
                "id": mem.id,
                "category": getattr(mem.category, "value", mem.category),
                "subcategory": mem.subcategory,
                "content": mem.content,
                "importance": getattr(mem.importance, "value", mem.importance),
                "timestamp": (
                    mem.timestamp.isoformat()
                    if hasattr(mem.timestamp, "isoformat")
                    else str(mem.timestamp)
                ),
                "session_id": mem.session_id,
                "tags": mem.tags or [],
                "related_files": mem.related_files or [],
                "status": mem.status,
                "access_count": getattr(mem, "access_count", 1),
                "last_accessed": getattr(mem, "last_accessed", None),
                "relevance_score": r.relevance_score,
                "match_reason": r.match_reason,
            }
            out.append(d)
        return {"memories": out}

    async def search_memories(self, request: MemorySearchRequest) -> List[MemoryResult]:
        """Search memories using semantic search and filters"""
        if not self.embedding_model:
            await self.initialize()

        conn = sqlite3.connect(self.metadata_db)

        # Build SQL query with filters
        sql_parts = ["SELECT * FROM memories WHERE status = 'active'"]
        params = []

        if not request.include_archived:
            sql_parts.append("AND status != 'archived'")

        if request.category:
            sql_parts.append("AND category = ?")
            params.append(request.category.value)

        if request.subcategory:
            sql_parts.append("AND subcategory = ?")
            params.append(request.subcategory)

        if request.min_importance:
            sql_parts.append("AND importance >= ?")
            params.append(request.min_importance.value)

        if request.recent_days:
            cutoff_date = datetime.now() - timedelta(days=request.recent_days)
            sql_parts.append("AND timestamp >= ?")
            params.append(cutoff_date.isoformat())

        # Execute base query
        sql = " ".join(sql_parts)
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        memories = [self._memory_from_row(row) for row in rows]

        # Apply semantic search if query provided
        if request.query and memories:
            query_embedding = self.embedding_model.encode(request.query)  # type:ignore

            results = []
            for memory in memories:
                if memory.embedding_vector:
                    memory_embedding = np.array(memory.embedding_vector)

                    # Calculate cosine similarity
                    similarity = np.dot(query_embedding, memory_embedding) / (
                        np.linalg.norm(query_embedding)
                        * np.linalg.norm(memory_embedding)
                    )

                    results.append(
                        MemoryResult(
                            memory=memory,
                            relevance_score=float(similarity),
                            match_reason="Semantic similarity",
                        )
                    )

            # Sort by relevance and limit
            results.sort(key=lambda x: x.relevance_score or 0, reverse=True)
            return results[: request.max_results]

        # Return without semantic ranking
        results = [
            MemoryResult(memory=memory, match_reason="Filter match")
            for memory in memories
        ]
        return results[: request.max_results]

    async def get_context_summary(
        self, session_id: Optional[str] = None
    ) -> ContextSummary:
        """Get contextual summary for new session"""
        if not self.embedding_model:
            await self.initialize()

        # Get recent progress updates
        recent_progress = await self.search_memories(
            MemorySearchRequest(
                category=MemoryCategory.PROGRESS, recent_days=30, max_results=5
            )
        )

        # Get key learnings (high importance)
        key_learnings = await self.search_memories(
            MemorySearchRequest(
                category=MemoryCategory.LEARNING,
                min_importance=MemoryImportance.HIGH,
                max_results=5,
            )
        )

        # Get user preferences
        user_preferences = await self.search_memories(
            MemorySearchRequest(category=MemoryCategory.PREFERENCE, max_results=10)
        )

        # Get important warnings (mistakes, debugging insights)
        important_warnings = await self.search_memories(
            MemorySearchRequest(
                category=MemoryCategory.MISTAKE,
                min_importance=MemoryImportance.MEDIUM,
                recent_days=60,
                max_results=5,
            )
        )

        return ContextSummary(
            recent_progress=[r.memory for r in recent_progress],
            key_learnings=[r.memory for r in key_learnings],
            user_preferences=[r.memory for r in user_preferences],
            important_warnings=[r.memory for r in important_warnings],
        )

    async def update_memory(self, memory_id: int, **updates) -> Optional[Memory]:
        """Update existing memory"""
        conn = sqlite3.connect(self.metadata_db)

        # Build update query
        set_parts = []
        params = []

        # Direct scalar fields
        for field, value in updates.items():
            if field in [
                "content",
                "category",
                "subcategory",
                "importance",
                "status",
                "verified",
            ]:
                set_parts.append(f"{field} = ?")
                if isinstance(value, Enum):
                    params.append(value.value)
                else:
                    params.append(value)

        # JSON-serialized list fields
        if "tags" in updates:
            set_parts.append("tags_json = ?")
            params.append(self._serialize_list(updates["tags"]))
        if "related_files" in updates:
            set_parts.append("related_files_json = ?")
            params.append(self._serialize_list(updates["related_files"]))
        if "context" in updates:
            set_parts.append("context_json = ?")
            params.append(self._serialize_dict(updates["context"]))

        # If content changed, recompute the fingerprint so dedup keeps working.
        if "content" in updates:
            cursor = conn.execute(
                "SELECT category FROM memories WHERE id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            cat = updates.get("category") or (row[0] if row else "")
            cat_val = cat.value if isinstance(cat, Enum) else cat
            set_parts.append("content_fingerprint = ?")
            params.append(self._content_fingerprint(cat_val, updates["content"]))

        if not set_parts:
            conn.close()
            return None

        params.append(memory_id)

        conn.execute(
            f"""
            UPDATE memories 
            SET {', '.join(set_parts)}
            WHERE id = ?
        """,
            params,
        )

        conn.commit()

        # Retrieve updated memory
        cursor = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        conn.close()

        return self._memory_from_row(row) if row else None

    def get_stats(self) -> MemoryStats:
        """Get memory system statistics"""
        conn = sqlite3.connect(self.metadata_db)

        # Total memories
        cursor = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'")
        total_memories = cursor.fetchone()[0]

        # By category
        cursor = conn.execute(
            """
            SELECT category, COUNT(*) 
            FROM memories 
            WHERE status = 'active' 
            GROUP BY category
        """
        )
        by_category = dict(cursor.fetchall())

        # By importance - convert integer keys to strings for Pydantic
        cursor = conn.execute(
            """
            SELECT importance, COUNT(*) 
            FROM memories 
            WHERE status = 'active' 
            GROUP BY importance
        """
        )
        by_importance_raw = cursor.fetchall()
        by_importance = {str(k): v for k, v in by_importance_raw}

        # Recent (last 7 days)
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        cursor = conn.execute(
            """
            SELECT COUNT(*) 
            FROM memories 
            WHERE status = 'active' AND timestamp >= ?
        """,
            (week_ago,),
        )
        recent_count = cursor.fetchone()[0]

        # Verified count
        cursor = conn.execute(
            """
            SELECT COUNT(*) 
            FROM memories 
            WHERE status = 'active' AND verified = TRUE
        """
        )
        verified_count = cursor.fetchone()[0]

        # Archived count
        cursor = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'archived'")
        archived_count = cursor.fetchone()[0]

        conn.close()

        return MemoryStats(
            total_memories=total_memories,
            by_category=by_category,
            by_importance=by_importance,
            recent_count=recent_count,
            verified_count=verified_count,
            archived_count=archived_count,
        )
