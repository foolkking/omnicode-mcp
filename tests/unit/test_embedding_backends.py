"""Unit tests for embedding backends (P2 — Cloud / Hybrid mode)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from omnicode_core.embeddings.backend import (
    EmbeddingBackend,
    HybridBackend,
    RemoteOpenAIBackend,
    resolve_backend,
)


class _FakeBackend(EmbeddingBackend):
    """Records calls so we can assert which path was taken."""

    name = "fake"

    def __init__(self, dim: int = 4, fail: bool = False) -> None:
        self.dimension = dim
        self.calls: list[Any] = []
        self._fail = fail

    def encode(self, text):
        self.calls.append(("encode", text))
        if self._fail:
            raise RuntimeError("boom")
        if isinstance(text, str):
            return np.ones(self.dimension, dtype="float32")
        return np.ones((len(text), self.dimension), dtype="float32")

    def encode_query(self, text: str):
        self.calls.append(("encode_query", text))
        if self._fail:
            raise RuntimeError("boom")
        return np.ones(self.dimension, dtype="float32")


def test_remote_backend_requires_url():
    with pytest.raises(ValueError):
        RemoteOpenAIBackend(url="", api_key="k", model="m")


def test_remote_backend_requires_model():
    with pytest.raises(ValueError):
        RemoteOpenAIBackend(url="https://x", api_key="k", model="")


def test_remote_backend_parses_openai_response():
    """Patches urlopen to return a fake OpenAI /embeddings response."""

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return self._payload

    payload = json.dumps(
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ]
        }
    ).encode("utf-8")

    backend = RemoteOpenAIBackend(
        url="https://example.invalid/v1/embeddings",
        api_key="sk-x",
        model="text-embedding-3-small",
    )
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
        out = backend.encode(["hello", "world"])
    assert out.shape == (2, 3)
    assert backend.dimension == 3
    # Single string returns a 1-D array
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
        single = backend.encode("hello")
    assert single.shape == (3,)


def test_hybrid_uses_remote_for_query_local_for_index():
    local = _FakeBackend(dim=4)
    remote = _FakeBackend(dim=4)
    h = HybridBackend(local, remote)

    h.encode("indexing chunk")
    assert local.calls and not remote.calls

    h.encode_query("user search")
    assert remote.calls[-1][0] == "encode_query"


def test_hybrid_falls_back_to_local_on_remote_failure():
    local = _FakeBackend(dim=4)
    remote = _FakeBackend(dim=4, fail=True)
    h = HybridBackend(local, remote)

    out = h.encode_query("question")
    # Local must have served the call after the remote raised.
    assert ("encode_query", "question") in local.calls
    assert out.shape == (4,)


def test_resolve_backend_local_default(monkeypatch):
    monkeypatch.delenv("OMNICODE_EMBEDDING_BACKEND", raising=False)
    # We can't actually load the model in tests offline, so just inspect the
    # returned class. resolve_backend will try to instantiate, so monkey-patch
    # the local class to a stub.
    import omnicode_core.embeddings.backend as mod

    monkeypatch.setattr(mod, "LocalSentenceTransformerBackend", lambda m: _FakeBackend())
    out = resolve_backend("dummy")
    assert isinstance(out, _FakeBackend)


def test_resolve_backend_remote(monkeypatch):
    monkeypatch.setenv("OMNICODE_EMBEDDING_BACKEND", "remote")
    monkeypatch.setenv("OMNICODE_EMBEDDING_REMOTE_URL", "https://x")
    monkeypatch.setenv("OMNICODE_EMBEDDING_REMOTE_KEY", "sk-test")
    out = resolve_backend("text-embedding-3-small")
    assert isinstance(out, RemoteOpenAIBackend)


def test_resolve_backend_hybrid(monkeypatch):
    monkeypatch.setenv("OMNICODE_EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setenv("OMNICODE_EMBEDDING_REMOTE_URL", "https://x")
    monkeypatch.setenv("OMNICODE_EMBEDDING_REMOTE_KEY", "sk-test")
    import omnicode_core.embeddings.backend as mod

    monkeypatch.setattr(mod, "LocalSentenceTransformerBackend", lambda m: _FakeBackend())
    out = resolve_backend("any")
    assert isinstance(out, HybridBackend)


def test_resolve_backend_hybrid_without_url_falls_back(monkeypatch):
    monkeypatch.setenv("OMNICODE_EMBEDDING_BACKEND", "hybrid")
    monkeypatch.delenv("OMNICODE_EMBEDDING_REMOTE_URL", raising=False)
    monkeypatch.delenv("OMNICODE_EMBEDDING_REMOTE_KEY", raising=False)
    import omnicode_core.embeddings.backend as mod

    fake = _FakeBackend()
    monkeypatch.setattr(mod, "LocalSentenceTransformerBackend", lambda m: fake)
    out = resolve_backend("any")
    # The factory must not raise — it should degrade to local.
    assert out is fake
