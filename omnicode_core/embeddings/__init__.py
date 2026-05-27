"""Pluggable embedding backends (P2 — Cloud / Hybrid mode).

The default is the in-process ``sentence-transformers`` model used by
``omnicode/search/engine.py``.  Cloud mode is opt-in via env var:

```
OMNICODE_EMBEDDING_BACKEND=remote          # use HTTP API
OMNICODE_EMBEDDING_REMOTE_URL=https://...  # OpenAI-compatible /embeddings
OMNICODE_EMBEDDING_REMOTE_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small     # served by the remote
```

Hybrid mode = use the remote backend for query-time embeddings (where
latency dominates and 1-vector calls are cheap) but keep the local one
for indexing (where the offline model wins on cost). Toggle via
``OMNICODE_EMBEDDING_BACKEND=hybrid``.
"""

from omnicode_core.embeddings.backend import (
    EmbeddingBackend,
    HybridBackend,
    LocalSentenceTransformerBackend,
    RemoteOpenAIBackend,
    get_default_backend,
    resolve_backend,
)

__all__ = [
    "EmbeddingBackend",
    "LocalSentenceTransformerBackend",
    "RemoteOpenAIBackend",
    "HybridBackend",
    "get_default_backend",
    "resolve_backend",
]
