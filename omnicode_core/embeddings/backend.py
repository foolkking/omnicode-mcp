"""Embedding backend abstractions.

Three implementations:

* ``LocalSentenceTransformerBackend`` — wraps the existing offline model.
  Identical to what ``omnicode/search/engine.py`` did before this module.
* ``RemoteOpenAIBackend`` — hits an OpenAI-compatible ``/embeddings``
  endpoint (works with OpenAI, Together, Anthropic Workers AI proxy,
  llama.cpp, vLLM, etc.).
* ``HybridBackend`` — uses ``LocalSentenceTransformerBackend`` for
  ``encode_many`` (indexing) and ``RemoteOpenAIBackend`` for the
  single-vector ``encode_query`` call (search).

The engine code stays unchanged: it calls ``encode(text)``. The other
methods are conveniences for callers that want the split behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


def _configure_torch_threads() -> None:
    raw = os.environ.get("OMNICODE_EMBEDDING_TORCH_THREADS", "").strip()
    if not raw:
        return
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid OMNICODE_EMBEDDING_TORCH_THREADS=%r", raw)
        return
    if value <= 0:
        return
    try:
        import torch  # type: ignore[import-not-found]

        torch.set_num_threads(value)
        set_interop = getattr(torch, "set_num_interop_threads", None)
        if callable(set_interop):
            try:
                set_interop(value)
            except RuntimeError:
                # PyTorch only allows setting interop threads before parallel
                # work starts. The main thread cap still applies.
                pass
        logger.info("Embedding torch thread cap set to %d", value)
    except Exception as exc:
        logger.warning("Failed to configure torch thread cap: %s", exc)


class EmbeddingBackend:
    """Minimal interface every backend must satisfy."""

    name: str = "abstract"
    dimension: Optional[int] = None  # filled in by concrete backends

    def encode(self, text: str | Sequence[str]):  # pragma: no cover - abstract
        raise NotImplementedError

    # Convenience wrappers — concrete backends can override for batching.
    def encode_query(self, text: str):
        return self.encode(text)

    def encode_many(self, texts: Sequence[str]):
        return self.encode(list(texts))


# ---------------------------------------------------------------------------
# Local (offline) backend
# ---------------------------------------------------------------------------
class LocalSentenceTransformerBackend(EmbeddingBackend):
    name = "local-sentence-transformers"

    def __init__(self, model_name: str) -> None:
        _configure_torch_threads()
        from sentence_transformers import SentenceTransformer

        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        try:
            # sentence-transformers ≥3.0 renamed the method; fall back for
            # older versions so the package floor stays at the existing pin.
            if hasattr(self._model, "get_embedding_dimension"):
                self.dimension = int(self._model.get_embedding_dimension())
            else:
                self.dimension = int(self._model.get_sentence_embedding_dimension())
        except Exception:
            self.dimension = None
        logger.info("✅ Local embedding backend ready: %s (dim=%s)", model_name, self.dimension)

    def encode(self, text):
        return self._model.encode(text)


# ---------------------------------------------------------------------------
# Remote (OpenAI-compatible) backend
# ---------------------------------------------------------------------------
class RemoteOpenAIBackend(EmbeddingBackend):
    name = "remote-openai-compatible"

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
    ) -> None:
        if not url:
            raise ValueError("RemoteOpenAIBackend: url is required")
        if not model:
            raise ValueError("RemoteOpenAIBackend: model is required")
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        # We can't probe dimension without a request — left None.

    def _request(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        if self._api_key:
            req.add_header("Authorization", f"Bearer {self._api_key}")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def encode(self, text):
        if isinstance(text, str):
            inputs: List[str] = [text]
            single = True
        else:
            inputs = list(text)
            single = False

        data = self._request({"model": self._model, "input": inputs})
        # OpenAI shape: {"data":[{"embedding":[...], "index":0}, ...]}
        embeddings: List[List[float]] = []
        for item in sorted(data.get("data", []), key=lambda d: d.get("index", 0)):
            embeddings.append(item["embedding"])

        try:
            import numpy as np

            arr = np.array(embeddings, dtype="float32")
            if self.dimension is None and arr.ndim == 2 and arr.shape[1] > 0:
                self.dimension = arr.shape[1]
            return arr[0] if single else arr
        except ImportError:  # pragma: no cover - numpy is a hard dep
            return embeddings[0] if single else embeddings


# ---------------------------------------------------------------------------
# Hybrid backend
# ---------------------------------------------------------------------------
class HybridBackend(EmbeddingBackend):
    """Use local for indexing, remote for query-time."""

    name = "hybrid-local-remote"

    def __init__(self, local: EmbeddingBackend, remote: EmbeddingBackend) -> None:
        self._local = local
        self._remote = remote
        self.dimension = remote.dimension or local.dimension

    def encode(self, text):
        # Default behaviour — used by indexing in engine.encode(chunk.content)
        return self._local.encode(text)

    def encode_query(self, text: str):
        # Search-time: prefer remote for single-vector quality.
        try:
            vec = self._remote.encode_query(text)
            if self.dimension is None and hasattr(vec, "shape"):
                self.dimension = int(vec.shape[-1])
            return vec
        except Exception as exc:
            logger.warning("Hybrid: remote query failed (%s); falling back to local.", exc)
            return self._local.encode_query(text)

    def encode_many(self, texts):
        return self._local.encode_many(texts)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def resolve_backend(model_name: str) -> EmbeddingBackend:
    """Build the backend based on env vars + the configured model name.

    Behaviour:

    * ``OMNICODE_EMBEDDING_BACKEND=remote`` → ``RemoteOpenAIBackend`` only.
      Requires ``OMNICODE_EMBEDDING_REMOTE_URL`` and
      ``OMNICODE_EMBEDDING_REMOTE_KEY``.
    * ``OMNICODE_EMBEDDING_BACKEND=hybrid`` → ``HybridBackend(local, remote)``.
    * anything else (including unset) → ``LocalSentenceTransformerBackend``.
    """
    backend = _env("OMNICODE_EMBEDDING_BACKEND", "local").lower().strip()
    if backend == "remote":
        return RemoteOpenAIBackend(
            url=_env("OMNICODE_EMBEDDING_REMOTE_URL"),
            api_key=_env("OMNICODE_EMBEDDING_REMOTE_KEY"),
            model=model_name,
        )
    if backend == "hybrid":
        local = LocalSentenceTransformerBackend(model_name)
        try:
            remote = RemoteOpenAIBackend(
                url=_env("OMNICODE_EMBEDDING_REMOTE_URL"),
                api_key=_env("OMNICODE_EMBEDDING_REMOTE_KEY"),
                model=_env("OMNICODE_EMBEDDING_REMOTE_MODEL", model_name),
            )
            return HybridBackend(local, remote)
        except ValueError as exc:
            logger.warning(
                "Hybrid embedding requested but remote not configured (%s); "
                "falling back to local-only.",
                exc,
            )
            return local
    return LocalSentenceTransformerBackend(model_name)


_DEFAULT: Optional[EmbeddingBackend] = None


def get_default_backend(model_name: str) -> EmbeddingBackend:
    """Return a process-wide cached backend (lazy)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = resolve_backend(model_name)
    return _DEFAULT


__all__ = [
    "EmbeddingBackend",
    "LocalSentenceTransformerBackend",
    "RemoteOpenAIBackend",
    "HybridBackend",
    "resolve_backend",
    "get_default_backend",
]
