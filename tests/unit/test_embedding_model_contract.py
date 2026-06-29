from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from omnicode_core.embeddings.models import embedding_status


def _write_complete_model_cache(cache: Path, model_name: str) -> None:
    safe = "models--" + model_name.replace("/", "--")
    snapshot = cache / safe / "snapshots" / "test-revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "modules.json").write_text("[]", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights")


def test_embedding_defaults_choose_cloud_model_in_hybrid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    status = embedding_status()

    assert status["model"] == "BAAI/bge-small-en-v1.5"
    assert status["cache_dir"] == str(tmp_path / "state" / "models")


def test_embedding_status_defaults_to_state_dir_cache_and_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OMNICODE_EMBEDDING_CACHE_DIR", raising=False)
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", raising=False)

    status = embedding_status()

    assert status["cache_dir"] == str(tmp_path / "state" / "models")
    assert status["local_files_only"] is True
    assert status["available"] is False
    assert status["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"
    assert "omnicode models pull" in " ".join(status["next_actions"])


def test_embedding_status_allows_explicit_online_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "false")

    status = embedding_status()

    assert status["cache_dir"] == str(tmp_path / "state" / "models")
    assert status["local_files_only"] is False
    assert status["available"] is True
    assert status["error_code"] is None


def test_embedding_status_reports_missing_local_files_only_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    status = embedding_status()

    assert status["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert status["dimension"] == 384
    assert status["local_files_only"] is True
    assert status["available"] is False
    assert status["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"
    assert status["download_required"] is True
    assert status["next_actions"]


def test_embedding_status_detects_cached_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "cache"
    _write_complete_model_cache(cache, "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(cache))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    status = embedding_status()

    assert status["cached"] is True
    assert status["available"] is True
    assert status["dimension"] == 384
    assert status["error_code"] is None


def test_embedding_status_rejects_incomplete_cached_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "cache"
    snapshot = (
        cache
        / "models--intfloat--e5-small-v2"
        / "snapshots"
        / "partial-revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "modules.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "intfloat/e5-small-v2")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(cache))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    status = embedding_status()

    assert status["cached"] is False
    assert status["available"] is False
    assert status["download_required"] is True
    assert status["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"


def test_embedding_status_explicit_cache_dir_overrides_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_cache = tmp_path / "env-cache"
    explicit_cache = tmp_path / "explicit-cache"
    _write_complete_model_cache(
        explicit_cache,
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(env_cache))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    status = embedding_status(
        "sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(explicit_cache),
        revision="test-revision",
        device="cpu",
    )

    assert status["cache_dir"] == str(explicit_cache)
    assert status["cached"] is True
    assert status["available"] is True
    assert status["revision"] == "test-revision"
    assert status["device"] == "cpu"


def test_pull_model_reports_requested_cache_and_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "model-cache"

    class FakeSentenceTransformer:
        def __init__(self, model_name: str, **kwargs) -> None:
            self.model_name = model_name
            self.kwargs = kwargs
            _write_complete_model_cache(
                cache,
                "sentence-transformers/all-MiniLM-L6-v2",
            )

        def get_embedding_dimension(self) -> int:
            return 384

    monkeypatch.setitem(
        __import__("sys").modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )

    from omnicode_core.embeddings.models import pull_model

    status = pull_model(
        "sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(cache),
        revision="abc123",
        device="cpu",
    )

    assert status["cache_dir"] == str(cache)
    assert status["cached"] is True
    assert status["available"] is True
    assert status["revision"] == "abc123"
    assert status["device"] == "cpu"
    assert status["dimension"] == 384


def test_resolve_backend_missing_offline_model_does_not_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")
    monkeypatch.delenv("OMNICODE_EMBEDDING_BACKEND", raising=False)

    from omnicode_core.embeddings.backend import (
        UnavailableEmbeddingBackend,
        resolve_backend,
    )

    backend = resolve_backend("sentence-transformers/all-MiniLM-L6-v2")
    assert isinstance(backend, UnavailableEmbeddingBackend)
    assert backend.status()["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"


def test_resolve_backend_offline_cache_miss_skips_model_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")
    monkeypatch.delenv("OMNICODE_EMBEDDING_BACKEND", raising=False)

    import omnicode_core.embeddings.backend as mod

    def _should_not_load(*_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("SentenceTransformer should not be instantiated")

    monkeypatch.setattr(mod, "LocalSentenceTransformerBackend", _should_not_load)

    backend = mod.resolve_backend("sentence-transformers/all-MiniLM-L6-v2")
    assert isinstance(backend, mod.UnavailableEmbeddingBackend)
    assert backend.status()["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"


def test_default_backend_retries_after_unavailable(monkeypatch) -> None:
    import omnicode_core.embeddings.backend as mod

    class ReadyBackend(mod.EmbeddingBackend):
        name = "ready"
        dimension = 384

        def __init__(self, model_name: str) -> None:
            self._model_name = model_name

        def encode(self, text):  # pragma: no cover - not needed here
            return [0.0] * 384

    calls: list[str] = []

    def fake_resolve(model_name: str):
        calls.append(model_name)
        if len(calls) == 1:
            return mod.UnavailableEmbeddingBackend(
                model_name,
                error="missing model",
                dimension=384,
            )
        return ReadyBackend(model_name)

    monkeypatch.setattr(mod, "_DEFAULT", None)
    monkeypatch.setattr(mod, "resolve_backend", fake_resolve)

    first = mod.get_default_backend("sentence-transformers/all-MiniLM-L6-v2")
    second = mod.get_default_backend("sentence-transformers/all-MiniLM-L6-v2")

    assert isinstance(first, mod.UnavailableEmbeddingBackend)
    assert isinstance(second, ReadyBackend)
    assert calls == [
        "sentence-transformers/all-MiniLM-L6-v2",
        "sentence-transformers/all-MiniLM-L6-v2",
    ]


def test_default_backend_reloads_when_model_changes(monkeypatch) -> None:
    import omnicode_core.embeddings.backend as mod

    class ReadyBackend(mod.EmbeddingBackend):
        name = "ready"

        def __init__(self, model_name: str) -> None:
            self._model_name = model_name
            self.dimension = 384 if "MiniLM" in model_name else 768

        def encode(self, text):  # pragma: no cover - not needed here
            return [0.0] * int(self.dimension or 0)

    calls: list[str] = []

    def fake_resolve(model_name: str):
        calls.append(model_name)
        return ReadyBackend(model_name)

    monkeypatch.setattr(mod, "_DEFAULT", None)
    monkeypatch.setattr(mod, "resolve_backend", fake_resolve)

    first = mod.get_default_backend("sentence-transformers/all-MiniLM-L6-v2")
    second = mod.get_default_backend("sentence-transformers/all-mpnet-base-v2")

    assert isinstance(first, ReadyBackend)
    assert isinstance(second, ReadyBackend)
    assert first is not second
    assert second.dimension == 768
    assert calls == [
        "sentence-transformers/all-MiniLM-L6-v2",
        "sentence-transformers/all-mpnet-base-v2",
    ]
