"""Embedding model catalogue and deployment helpers.

The semantic/vector stack is optional for the current production gate.  These
helpers make that explicit: model status is observable without loading a model,
and loading can be forced to local cache only for offline/cloud deployments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SUPPORTED_EMBEDDING_MODELS = {
    "sentence-transformers/all-MiniLM-L6-v2": {
        "dimension": 384,
        "recommended_for": ["local", "small-cloud"],
    },
    "BAAI/bge-small-en-v1.5": {
        "dimension": 384,
        "recommended_for": ["cloud", "hybrid"],
    },
    "intfloat/e5-small-v2": {
        "dimension": 384,
        "recommended_for": ["local", "cloud", "retrieval"],
    },
    "sentence-transformers/all-mpnet-base-v2": {
        "dimension": 768,
        "recommended_for": ["quality", "cloud"],
    },
}

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_CLOUD_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_DIRNAME = "models"


@dataclass(frozen=True)
class EmbeddingModelConfig:
    model_name: str
    cache_dir: Optional[str]
    local_files_only: bool
    revision: Optional[str]
    device: Optional[str]
    preload: bool


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def default_embedding_cache_dir() -> str:
    """Stable model cache root used when deployment does not configure one."""
    state_dir = _env("OMNICODE_STATE_DIR").strip()
    if state_dir:
        return str(Path(state_dir).expanduser() / DEFAULT_CACHE_DIRNAME)
    return str(Path.home() / ".omnicode" / DEFAULT_CACHE_DIRNAME)


def default_local_files_only() -> bool:
    """Do not download models implicitly during service startup."""
    raw = _env("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY")
    if _falsey(raw):
        return False
    if _truthy(raw):
        return True
    return True


def default_embedding_model(*, deployment_mode: Optional[str] = None) -> str:
    mode = (
        deployment_mode
        or _env("OMNICODE_MODE")
        or _env("OMNICODE_EXECUTOR_MODE")
        or "local"
    ).strip().lower()
    if mode in {"cloud", "hybrid", "cloud-index"}:
        return DEFAULT_CLOUD_MODEL
    return DEFAULT_LOCAL_MODEL


def embedding_model_config(
    model_name: Optional[str] = None,
    *,
    deployment_mode: Optional[str] = None,
    cache_dir: Optional[str] = None,
    local_files_only: Optional[bool] = None,
    revision: Optional[str] = None,
    device: Optional[str] = None,
    preload: Optional[bool] = None,
) -> EmbeddingModelConfig:
    chosen = (
        model_name
        or _env("OMNICODE_EMBEDDING_MODEL")
        or _env("EMBEDDING_MODEL")
        or default_embedding_model(deployment_mode=deployment_mode)
    )
    resolved_cache_dir = (
        cache_dir
        or _env("OMNICODE_EMBEDDING_CACHE_DIR")
        or _env("SENTENCE_TRANSFORMERS_HOME")
        or _env("HF_HUB_CACHE")
        or _env("HF_HOME")
        or default_embedding_cache_dir()
    )
    return EmbeddingModelConfig(
        model_name=chosen,
        cache_dir=resolved_cache_dir,
        local_files_only=(
            bool(local_files_only)
            if local_files_only is not None
            else default_local_files_only()
        ),
        revision=(
            revision
            if revision is not None
            else (_env("OMNICODE_EMBEDDING_REVISION") or None)
        ),
        device=(
            device
            if device is not None
            else (_env("OMNICODE_EMBEDDING_DEVICE") or None)
        ),
        preload=(
            bool(preload)
            if preload is not None
            else _truthy(_env("OMNICODE_EMBEDDING_PRELOAD"))
        ),
    )


def apply_embedding_cache_env(cache_dir: Optional[str]) -> None:
    if not cache_dir:
        return
    path = str(Path(cache_dir).expanduser())
    os.environ.setdefault("HF_HOME", path)
    os.environ.setdefault("HF_HUB_CACHE", str(Path(path) / "hub"))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", path)


def _cache_candidates(model_name: str, cache_dir: Optional[str]) -> list[Path]:
    if not cache_dir:
        roots = [
            Path(os.environ.get("SENTENCE_TRANSFORMERS_HOME", "")),
            Path(os.environ.get("HF_HUB_CACHE", "")),
            Path(os.environ.get("HF_HOME", "")) / "hub",
            Path.home() / ".cache" / "huggingface" / "hub",
        ]
    else:
        root = Path(cache_dir).expanduser()
        roots = [root, root / "hub"]
    safe = "models--" + model_name.replace("/", "--")
    plain = model_name.replace("/", "_")
    candidates: list[Path] = []
    for root in roots:
        if not str(root):
            continue
        candidates.extend([
            root / safe,
            root / plain,
            root / model_name,
        ])
    return candidates


_WEIGHT_FILENAMES = {
    "model.safetensors",
    "pytorch_model.bin",
    "tf_model.h5",
    "model.onnx",
    "openvino_model.xml",
}


def _has_model_weight(path: Path) -> bool:
    for item in path.rglob("*"):
        if item.is_file() and item.name in _WEIGHT_FILENAMES:
            return True
    return False


def _is_complete_model_snapshot(path: Path) -> bool:
    if not path.is_dir():
        return False
    has_config = (path / "config.json").is_file()
    has_sentence_transformer_config = (
        (path / "modules.json").is_file()
        or (path / "config_sentence_transformers.json").is_file()
    )
    return has_config and has_sentence_transformer_config and _has_model_weight(path)


def _is_complete_model_cache(path: Path) -> bool:
    if not path.exists():
        return False
    snapshots = path / "snapshots"
    if snapshots.is_dir():
        return any(_is_complete_model_snapshot(snapshot) for snapshot in snapshots.iterdir())
    return _is_complete_model_snapshot(path)


def model_cached(model_name: str, cache_dir: Optional[str]) -> bool:
    return any(_is_complete_model_cache(path) for path in _cache_candidates(model_name, cache_dir))


def embedding_status(
    model_name: Optional[str] = None,
    *,
    deployment_mode: Optional[str] = None,
    cache_dir: Optional[str] = None,
    local_files_only: Optional[bool] = None,
    revision: Optional[str] = None,
    device: Optional[str] = None,
    preload: Optional[bool] = None,
    loaded: Optional[bool] = None,
    dimension: Optional[int] = None,
    error: Optional[BaseException | str] = None,
) -> dict[str, Any]:
    config = embedding_model_config(
        model_name,
        deployment_mode=deployment_mode,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        revision=revision,
        device=device,
        preload=preload,
    )
    catalog = SUPPORTED_EMBEDDING_MODELS.get(config.model_name, {})
    cached = model_cached(config.model_name, config.cache_dir)
    expected_dim = int(catalog.get("dimension") or 0) or None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    if error is not None:
        error_message = str(error)
        if config.local_files_only and not cached:
            error_code = "EMBEDDING_MODEL_NOT_FOUND"
        else:
            error_code = error.__class__.__name__ if not isinstance(error, str) else "EMBEDDING_ERROR"
    elif config.local_files_only and not cached:
        error_code = "EMBEDDING_MODEL_NOT_FOUND"
        error_message = "embedding model is not present in the configured cache"

    available = bool(loaded) or (cached if config.local_files_only else True)
    if error_code:
        available = False
    return {
        "model": config.model_name,
        "dimension": dimension or expected_dim,
        "expected_dimension": expected_dim,
        "cache_dir": config.cache_dir,
        "local_files_only": config.local_files_only,
        "revision": config.revision,
        "device": config.device,
        "preload": config.preload,
        "cached": cached,
        "loaded": bool(loaded),
        "available": available,
        "download_required": bool(not cached),
        "error_code": error_code,
        "error": error_message,
        "supported": config.model_name in SUPPORTED_EMBEDDING_MODELS,
        "next_actions": (
            [
                (
                    "omnicode models pull --model "
                    f"{config.model_name}"
                    + (f" --cache-dir {config.cache_dir}" if config.cache_dir else "")
                )
            ]
            if error_code == "EMBEDDING_MODEL_NOT_FOUND"
            else []
        ),
    }


def pull_model(
    model_name: str,
    *,
    cache_dir: Optional[str] = None,
    revision: Optional[str] = None,
    device: Optional[str] = None,
) -> dict[str, Any]:
    if model_name not in SUPPORTED_EMBEDDING_MODELS:
        raise ValueError(
            "Unsupported embedding model. Supported models: "
            + ", ".join(sorted(SUPPORTED_EMBEDDING_MODELS))
        )
    apply_embedding_cache_env(cache_dir)
    from sentence_transformers import SentenceTransformer

    kwargs: dict[str, Any] = {}
    if cache_dir:
        kwargs["cache_folder"] = str(Path(cache_dir).expanduser())
    if revision:
        kwargs["revision"] = revision
    if device:
        kwargs["device"] = device
    model = SentenceTransformer(model_name, **kwargs)
    dim = None
    try:
        if hasattr(model, "get_embedding_dimension"):
            dim = int(model.get_embedding_dimension())
        elif hasattr(model, "get_sentence_embedding_dimension"):
            dim = int(model.get_sentence_embedding_dimension())
    except Exception:
        dim = None
    return embedding_status(
        model_name,
        cache_dir=cache_dir,
        revision=revision,
        device=device,
        loaded=True,
        dimension=dim,
    )


__all__ = [
    "DEFAULT_CLOUD_MODEL",
    "DEFAULT_LOCAL_MODEL",
    "SUPPORTED_EMBEDDING_MODELS",
    "EmbeddingModelConfig",
    "apply_embedding_cache_env",
    "default_embedding_model",
    "default_embedding_cache_dir",
    "default_local_files_only",
    "embedding_model_config",
    "embedding_status",
    "model_cached",
    "pull_model",
]
