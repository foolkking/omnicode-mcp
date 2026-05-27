"""Search-side helpers that don't fit in the legacy ``omnicode/search`` tree.

Currently exposes:

* :class:`Reranker` and friends — a cross-encoder rerank step that
  refines the bi-encoder + RRF ordering produced by ``SearchEngine``.
"""

from omnicode_core.search.reranker import (
    Reranker,
    NoOpReranker,
    BGEReranker,
    get_reranker,
)

__all__ = ["Reranker", "NoOpReranker", "BGEReranker", "get_reranker"]
