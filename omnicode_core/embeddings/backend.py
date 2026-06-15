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
import inspect
from typing import Any, List, Optional, Sequence

from omnicode_core.embeddings.models import (
    apply_embedding_cache_env,
    embedding_model_config,
    embedding_status,
)

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


class UnavailableEmbeddingBackend(EmbeddingBackend):
    """Placeholder backend when the configured model cannot be loaded.

    Exact search/read/patch must remain available even when semantic embeddings
    are offline or not pre-downloaded. This backend lets the service start and
    pushes a structured error to the semantic call site instead of crashing the
    FastAPI lifespan.
    """

    name = "embedding-unavailable"

    def __init__(
        self,
        model_name: str,
        *,
        error: BaseException | str,
        dimension: Optional[int] = None,
    ) -> None:
        self.model_name = model_name
        self.error = error
        self.dimension = dimension

    def encode(self, text: str | Sequence[str]):
        status = embedding_status(
            self.model_name,
            loaded=False,
            dimension=self.dimension,
            error=self.error,
        )
        code = status.get("error_code") or "EMBEDDING_UNAVAILABLE"
        msg = status.get("error") or str(self.error)
        raise RuntimeError(f"{code}: {msg}")

    def status(self) -> dict:
        return embedding_status(
            self.model_name,
            loaded=False,
            dimension=self.dimension,
            error=self.error,
        )


# ---------------------------------------------------------------------------
# Local (offline) backend
# ---------------------------------------------------------------------------
class LocalSentenceTransformerBackend(EmbeddingBackend):
    name = "local-sentence-transformers"

    def __init__(
        self,
        model_name: str,
        *,
        cache_dir: Optional[str] = None,
        local_files_only: Optional[bool] = None,
        revision: Optional[str] = None,
        device: Optional[str] = None,
    ) -> None:
        _configure_torch_threads()
        from sentence_transformers import SentenceTransformer

        self._model_name = model_name
        self.cache_dir = cache_dir
        self.local_files_only = bool(local_files_only)
        self.revision = revision
        self.device = device
        apply_embedding_cache_env(cache_dir)
        kwargs = {}
        if cache_dir:
            kwargs["cache_folder"] = cache_dir
        if local_files_only is not None:
            kwargs["local_files_only"] = bool(local_files_only)
        if revision:
            kwargs["revision"] = revision
        if device:
            kwargs["device"] = device
        try:
            accepted = set(inspect.signature(SentenceTransformer).parameters)
            kwargs = {k: v for k, v in kwargs.items() if k in accepted}
        except Exception:
            pass
        try:
            self._model = SentenceTransformer(model_name, **kwargs)
        except TypeError:
            fallback = {
                k: v for k, v in kwargs.items()
                if k in {"cache_folder", "device"}
            }
            self._model = SentenceTransformer(model_name, **fallback)
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

    def status(self) -> dict:
        env_overrides = {
            "OMNICODE_EMBEDDING_CACHE_DIR": self.cache_dir or "",
            "OMNICODE_EMBEDDING_LOCAL_FILES_ONLY": (
                "true" if self.local_files_only else "false"
            ),
        }
        previous: dict[str, Optional[str]] = {}
        for key, value in env_overrides.items():
            previous[key] = os.environ.get(key)
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        try:
            return embedding_status(
                self._model_name,
                loaded=True,
                dimension=self.dimension,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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


def _build_local_backend(model_name: str, config: Any) -> EmbeddingBackend:
    """Instantiate local backend with backward-compatible monkeypatch support."""
    preflight = embedding_status(model_name)
    if config.local_files_only and not preflight.get("cached"):
        return UnavailableEmbeddingBackend(
            model_name,
            error=preflight.get("error") or "embedding model is not present in cache",
            dimension=preflight.get("dimension") or preflight.get("expected_dimension"),
        )
    try:
        return LocalSentenceTransformerBackend(
            model_name,
            cache_dir=config.cache_dir,
            local_files_only=config.local_files_only,
            revision=config.revision,
            device=config.device,
        )
    except TypeError:
        # Older tests/plugins monkeypatch LocalSentenceTransformerBackend with
        # a single-argument callable. Keep that compatibility while the real
        # implementation receives the deployment/cache/offline controls above.
        return LocalSentenceTransformerBackend(model_name)
    except Exception as exc:
        logger.warning("Local embedding backend unavailable: %s", exc)
        status = embedding_status(model_name, error=exc)
        return UnavailableEmbeddingBackend(
            model_name,
            error=exc,
            dimension=status.get("dimension") or status.get("expected_dimension"),
        )


def resolve_backend(model_name: str) -> EmbeddingBackend:
    """Build the backend based on env vars + the configured model name.

    Behaviour:

    * ``OMNICODE_EMBEDDING_BACKEND=remote`` → ``RemoteOpenAIBackend`` only.
      Requires ``OMNICODE_EMBEDDING_REMOTE_URL`` and
      ``OMNICODE_EMBEDDING_REMOTE_KEY``.
    * ``OMNICODE_EMBEDDING_BACKEND=hybrid`` → ``HybridBackend(local, remote)``.
    * anything else (including unset) → ``LocalSentenceTransformerBackend``.
    """
    config = embedding_model_config(model_name)
    model_name = config.model_name
    backend = _env("OMNICODE_EMBEDDING_BACKEND", "local").lower().strip()
    if backend == "remote":
        return RemoteOpenAIBackend(
            url=_env("OMNICODE_EMBEDDING_REMOTE_URL"),
            api_key=_env("OMNICODE_EMBEDDING_REMOTE_KEY"),
            model=model_name,
        )
    if backend == "hybrid":
        local = _build_local_backend(model_name, config)
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
    return _build_local_backend(model_name, config)


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
    "UnavailableEmbeddingBackend",
    "resolve_backend",
    "get_default_backend",
]
