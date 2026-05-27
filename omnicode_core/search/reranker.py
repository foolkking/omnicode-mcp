"""Cross-encoder reranker (Wave 2 W2-9).

The shipped search pipeline is a bi-encoder + RRF combo: every chunk is
embedded once at index time and the query is embedded once at search
time. That's fast and good for *recall*, but the final ordering is
noisy because the embeddings were never aware of the query.

A cross-encoder fixes that. It scores each (query, candidate) pair
jointly, which is ~10× more expensive per item but only runs over the
top-N candidates the bi-encoder picked. End-to-end latency goes up by
~50-200 ms on CPU for a top-50 → top-K rerank, in exchange for much
better top-3 ordering on complex queries.

Design:

* ``Reranker`` — abstract base.
* ``NoOpReranker`` — passes results through unchanged. Used when the
  feature is disabled (``OMNICODE_RERANKER`` unset / ``"false"``).
* ``BGEReranker`` — wraps ``sentence_transformers.CrossEncoder`` with
  a BGE reranker model (default ``BAAI/bge-reranker-v2-m3``).

When the reranker reorders results, every promoted item gets the tag
``"reranked"`` appended to its ``why_matched`` list so AI editors can
explain the ordering.
"""

from __future__ import annotations

import logging
import os
from typing import List, Sequence

logger = logging.getLogger(__name__)


class Reranker:
    """Abstract reranker interface."""

    name: str = "abstract"

    def rerank(
        self, query: str, candidates: Sequence
    ) -> List:  # pragma: no cover - abstract
        """Return ``candidates`` reordered by relevance to ``query``.

        Must NOT mutate the input sequence; returns a new list.
        """
        raise NotImplementedError


class NoOpReranker(Reranker):
    """Identity reranker — keeps the input order. The default fallback."""

    name = "noop"

    def rerank(self, query: str, candidates: Sequence) -> List:
        return list(candidates)


class BGEReranker(Reranker):
    """Cross-encoder reranker using a BGE model.

    Defaults to ``BAAI/bge-reranker-v2-m3`` (≈560 MB). Override via
    ``OMNICODE_RERANKER_MODEL`` env var to swap in any HuggingFace
    cross-encoder.

    Lazily loads the model on first ``rerank()`` call so a no-op
    invocation (e.g. an empty candidate list) never pays the load
    cost.
    """

    name = "bge-cross-encoder"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.environ.get(
            "OMNICODE_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"
        )
        self._model = None  # type: ignore[assignment]

    def _ensure_loaded(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._model_name)
            logger.info(
                "✅ Reranker loaded: %s (cross-encoder)", self._model_name
            )
        except Exception as exc:
            logger.warning(
                "Reranker load failed (%s); falling back to no-op.",
                exc,
            )
            self._model = "noop"  # sentinel — used to skip future attempts

    def rerank(self, query: str, candidates: Sequence) -> List:
        if not candidates:
            return list(candidates)

        self._ensure_loaded()
        if self._model == "noop" or self._model is None:
            return list(candidates)

        # Build the (query, text) pairs. Falling back gracefully if a
        # candidate has no usable text gives the rest of the pipeline
        # robustness against odd index entries.
        pairs: list[list[str]] = []
        kept: list = []
        for c in candidates:
            text = self._candidate_text(c)
            if not text:
                continue
            pairs.append([query, text])
            kept.append(c)

        if not pairs:
            return list(candidates)

        try:
            scores = self._model.predict(pairs)
        except Exception as exc:
            logger.warning("Reranker predict failed (%s); keeping order.", exc)
            return list(candidates)

        # Annotate why_matched + relevance_score with the new ordering.
        scored = list(zip(scores, kept))
        scored.sort(key=lambda x: float(x[0]), reverse=True)

        out: list = []
        for new_score, c in scored:
            existing = list(getattr(c, "why_matched", []) or [])
            if "reranked" not in existing:
                existing.append("reranked")
            try:
                # Keep the bi-encoder score around for debugging via
                # the optional `bi_encoder_score` attribute. Not all
                # callers persist it, so this is best-effort.
                if hasattr(c, "relevance_score") and not hasattr(c, "bi_encoder_score"):
                    c.bi_encoder_score = c.relevance_score  # type: ignore[attr-defined]
                c.relevance_score = float(new_score)  # type: ignore[attr-defined]
                c.why_matched = existing  # type: ignore[attr-defined]
            except Exception:
                # Pydantic-style frozen models — leave the attrs alone
                # and just rely on the order we returned.
                pass
            out.append(c)

        return out

    @staticmethod
    def _candidate_text(c) -> str:
        """Best-effort text extraction so the cross-encoder has something
        meaningful to score against the query."""
        for attr in ("docstring", "signature", "chunk_type", "snippet", "content"):
            value = getattr(c, attr, None)
            if value:
                return str(value)[:1000]
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_DEFAULT: Reranker | None = None


def _flag_enabled() -> bool:
    raw = os.environ.get("OMNICODE_RERANKER", "").lower().strip()
    return raw in ("1", "true", "yes", "on")


def get_reranker() -> Reranker:
    """Return the process-wide default reranker.

    * ``OMNICODE_RERANKER=true`` → ``BGEReranker`` (lazy load).
    * unset / false                → ``NoOpReranker``.

    Re-reads the env var every call so toggling the flag (e.g. via the
    Web Console) takes effect without a restart, while the loaded
    cross-encoder model is cached after the first real use.
    """
    global _DEFAULT
    if _flag_enabled():
        if not isinstance(_DEFAULT, BGEReranker):
            _DEFAULT = BGEReranker()
        return _DEFAULT
    if not isinstance(_DEFAULT, NoOpReranker):
        _DEFAULT = NoOpReranker()
    return _DEFAULT


__all__ = [
    "Reranker",
    "NoOpReranker",
    "BGEReranker",
    "get_reranker",
]
