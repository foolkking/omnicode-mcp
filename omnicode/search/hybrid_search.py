import logging
from typing import Any, Dict, List

from pydantic import BaseModel

logger = logging.getLogger(__name__)

class SearchResult(BaseModel):
    chunk_id: str
    file_path: str
    content: str
    score: float
    chunk_type: str

class HybridSearchEngine:
    """
    Implements Reciprocal Rank Fusion (RRF) to combine semantic and keyword search.
    """
    def __init__(self, vector_store, keyword_searcher):
        self.vector_store = vector_store
        self.keyword_searcher = keyword_searcher

    def _rrf(self, lists: List[List[Dict[str, Any]]], k: int = 60) -> List[SearchResult]:
        """
        Reciprocal Rank Fusion algorithm.
        Score = sum(1 / (k + rank))
        """
        rrf_scores = {}
        items_dict = {}

        for lst in lists:
            for rank, item in enumerate(lst):
                item_id = item['chunk_id']
                if item_id not in rrf_scores:
                    rrf_scores[item_id] = 0.0
                    items_dict[item_id] = item

                rrf_scores[item_id] += 1.0 / (k + rank + 1)

        # Sort by RRF score descending
        sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for item_id, score in sorted_items:
            data = items_dict[item_id]
            results.append(SearchResult(
                chunk_id=item_id,
                file_path=data.get('file_path', ''),
                content=data.get('content', ''),
                score=score,
                chunk_type=data.get('chunk_type', '')
            ))

        return results

    async def search(self, query: str, top_k: int = 10, metadata_filter: Dict[str, Any] = None) -> List[SearchResult]:
        """
        Perform hybrid search combining semantic (vector) and keyword search.
        """
        # 1. Get semantic results
        semantic_results = await self.vector_store.search(query, top_k=top_k*2, metadata_filter=metadata_filter)

        # 2. Get keyword results (BM25 or similar)
        keyword_results = await self.keyword_searcher.search(query, top_k=top_k*2, metadata_filter=metadata_filter)

        # 3. Combine with RRF
        combined = self._rrf([semantic_results, keyword_results])

        return combined[:top_k]
