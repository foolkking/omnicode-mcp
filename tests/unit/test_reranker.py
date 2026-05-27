"""Unit tests for the cross-encoder reranker (Wave 2 W2-9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from omnicode_core.search.reranker import (
    BGEReranker,
    NoOpReranker,
    Reranker,
    get_reranker,
)


# A minimal stand-in for `LegacySearchResult` so tests don't need the
# whole search engine.
@dataclass
class _Cand:
    docstring: str = ""
    signature: str = ""
    chunk_type: str = ""
    snippet: str = ""
    relevance_score: float = 0.0
    why_matched: List[str] = field(default_factory=list)


def test_noop_reranker_preserves_order():
    rr = NoOpReranker()
    items = [_Cand(docstring=f"x{i}") for i in range(3)]
    out = rr.rerank("anything", items)
    assert [id(x) for x in out] == [id(x) for x in items]


def test_reranker_is_abstract():
    with pytest.raises(NotImplementedError):
        Reranker().rerank("q", [])


def test_factory_returns_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("OMNICODE_RERANKER", raising=False)
    rr = get_reranker()
    assert isinstance(rr, NoOpReranker)


def test_factory_returns_bge_when_enabled(monkeypatch):
    monkeypatch.setenv("OMNICODE_RERANKER", "true")
    rr = get_reranker()
    assert isinstance(rr, BGEReranker)
    monkeypatch.delenv("OMNICODE_RERANKER", raising=False)


def test_factory_flips_back_to_noop_after_disable(monkeypatch):
    monkeypatch.setenv("OMNICODE_RERANKER", "true")
    assert isinstance(get_reranker(), BGEReranker)
    monkeypatch.delenv("OMNICODE_RERANKER", raising=False)
    assert isinstance(get_reranker(), NoOpReranker)


def test_bge_falls_back_when_model_missing(monkeypatch):
    """When sentence_transformers can't load the model, every rerank
    call must degrade to identity rather than raise."""
    rr = BGEReranker(model_name="this/does-not-exist-locally")
    # Make ensure_loaded fast: directly force the sentinel state.
    rr._model = "noop"

    items = [_Cand(docstring="a"), _Cand(docstring="b")]
    out = rr.rerank("q", items)
    assert [id(x) for x in out] == [id(x) for x in items]


def test_bge_skips_empty_candidate_list():
    rr = BGEReranker()
    assert rr.rerank("q", []) == []


def test_bge_uses_predict_results_to_reorder(monkeypatch):
    """Stub the model so we can verify the sort order without actually
    loading the cross-encoder weights."""

    class _StubModel:
        def predict(self, pairs):
            # Return higher score for the *second* item so the rerank
            # output should swap order.
            return [0.1, 0.9, 0.5]

    rr = BGEReranker(model_name="stub")
    rr._model = _StubModel()

    a = _Cand(docstring="alpha", relevance_score=0.5)
    b = _Cand(docstring="beta", relevance_score=0.5)
    c = _Cand(docstring="gamma", relevance_score=0.5)

    out = rr.rerank("q", [a, b, c])
    assert [x.docstring for x in out] == ["beta", "gamma", "alpha"]
    # All survivors gained the "reranked" tag.
    assert all("reranked" in x.why_matched for x in out)
    # Bi-encoder score was preserved on the side.
    assert all(getattr(x, "bi_encoder_score", None) == 0.5 for x in out)
    # Relevance score now reflects the cross-encoder.
    assert out[0].relevance_score == 0.9
    assert out[1].relevance_score == 0.5
    assert out[2].relevance_score == 0.1


def test_bge_drops_candidates_with_no_text():
    """Items where every text-bearing attribute is empty must not
    crash the predictor — they're filtered out before the call."""

    class _StubModel:
        def __init__(self):
            self.calls: list[list[list[str]]] = []

        def predict(self, pairs):
            self.calls.append(pairs)
            return [0.7] * len(pairs)

    rr = BGEReranker(model_name="stub")
    stub = _StubModel()
    rr._model = stub

    # Only one candidate has any text. The empty one must be skipped.
    a = _Cand(docstring="", signature="", chunk_type="", snippet="")
    b = _Cand(docstring="meaningful")
    out = rr.rerank("q", [a, b])
    assert len(stub.calls) == 1
    assert stub.calls[0] == [["q", "meaningful"]]
    # The reranker only ranked one candidate, so the other one falls
    # outside the reordered list — that's by design.
    assert out == [b]


def test_bge_predict_failure_falls_back():
    class _BoomModel:
        def predict(self, pairs):
            raise RuntimeError("kaboom")

    rr = BGEReranker(model_name="stub")
    rr._model = _BoomModel()

    items = [_Cand(docstring="a"), _Cand(docstring="b")]
    out = rr.rerank("q", items)
    assert out == items  # original order, no exception
