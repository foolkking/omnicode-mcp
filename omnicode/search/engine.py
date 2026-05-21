import os
import json
import logging
import time
from typing import List, Dict, Any, Optional
import numpy as np

from omnicode.ast_engine.parser import UnifiedASTParser
from omnicode.ast_engine.chunker import ASTChunker
from omnicode.search.vector_store import VectorStore
from omnicode.search.hybrid_search import HybridSearchEngine
from omnicode.search.directory_lister import DirectoryLister
from omnicode.search.models import SearchRequest

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
            "last_indexed": "never",
            "index_size": 0
        }

    async def initialize(self) -> None:
        """Initialize the embedding model and verify DB files"""
        logger.info("Initializing Semantic Search Engine...")
        if self.embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("✅ sentence-transformers all-MiniLM-L6-v2 loaded successfully")
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
            
            db_file = os.path.join(self.db_dir, "vector_store.db")
            if os.path.exists(db_file):
                self.stats["index_size"] = os.path.getsize(db_file)
        except Exception as e:
            logger.warning(f"Failed to load search stats from DB: {e}")

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
        """Scan working directory, parse all source files, and index them"""
        logger.info(f"Indexing codebase in {self.working_dir}...")
        valid_extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".cpp", ".h", ".cc", ".c"}
        
        for root, dirs, files in os.walk(self.working_dir):
            # Skip hidden folders and caches
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".venv", "build", "dist"}]
            
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in valid_extensions:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.working_dir)
                    try:
                        await self.update_file(rel_path)
                    except Exception as e:
                        logger.warning(f"Failed to index file {rel_path}: {e}")

        logger.info("Codebase indexing completed successfully.")

    async def list_symbols_in_file(self, file_path: str) -> dict:
        """Extract all top level code structures using Tree-sitter"""
        full_path = os.path.abspath(os.path.join(self.working_dir, file_path))
        if not os.path.exists(full_path):
            return {"error": f"File not found: {file_path}", "symbols": []}

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            return {"error": f"Could not read file: {e}", "symbols": []}

        language = os.path.splitext(file_path)[1].lstrip(".") or "python"
        tree = self.ast_parser.parse(content, language)
        
        symbols = []
        if tree:
            root = tree.root_node
            for i, child in enumerate(root.children):
                if child.type in ['function_definition', 'class_definition', 'function_declaration', 'method_definition']:
                    symbols.append({
                        "name": f"symbol_{i}", # simplistic placeholder fallback
                        "type": child.type,
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1
                    })

        return {
            "file_path": file_path,
            "symbols": symbols,
            "count": len(symbols)
        }

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
