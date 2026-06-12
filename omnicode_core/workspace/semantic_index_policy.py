"""Shared semantic indexing policy for hybrid snapshot workspaces."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_SEMANTIC_EXTENSIONS = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".cxx",
        ".go",
        ".h",
        ".hh",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".rst",
        ".scala",
        ".sh",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
DEFAULT_SEMANTIC_FILENAMES = frozenset(
    {
        "dockerfile",
        "gemfile",
        "justfile",
        "makefile",
        "procfile",
        "rakefile",
    }
)
DEFAULT_SEMANTIC_MAX_FILE_BYTES = 500_000
DEFAULT_SEMANTIC_INITIAL_FILE_LIMIT = 2_000


def semantic_index_extensions() -> Optional[frozenset[str]]:
    raw = (os.environ.get("OMNICODE_SYNC_SEMANTIC_EXTENSIONS") or "").strip()
    if not raw:
        return DEFAULT_SEMANTIC_EXTENSIONS
    tokens = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if any(token in {"*", "all"} for token in tokens):
        return None
    if any(token in {"0", "false", "none", "off"} for token in tokens):
        return frozenset()
    normalized = []
    for token in tokens:
        normalized.append(token if token.startswith(".") else f".{token}")
    return frozenset(normalized)


def semantic_index_filenames() -> frozenset[str]:
    raw = (os.environ.get("OMNICODE_SYNC_SEMANTIC_FILENAMES") or "").strip()
    if not raw:
        return DEFAULT_SEMANTIC_FILENAMES
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def semantic_index_max_file_bytes() -> Optional[int]:
    raw = (os.environ.get("OMNICODE_SYNC_SEMANTIC_MAX_FILE_BYTES") or "").strip()
    if not raw:
        return DEFAULT_SEMANTIC_MAX_FILE_BYTES
    if raw.lower() in {"0", "none", "off", "unlimited"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SEMANTIC_MAX_FILE_BYTES
    return max(1, value)


def semantic_initial_file_limit() -> int:
    raw = (
        os.environ.get("OMNICODE_SYNC_SEMANTIC_INITIAL_FILE_LIMIT") or ""
    ).strip()
    if not raw:
        return DEFAULT_SEMANTIC_INITIAL_FILE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SEMANTIC_INITIAL_FILE_LIMIT
    return max(0, value)


def _metadata_int(metadata: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(metadata.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def semantic_initial_sync_skip_reason(metadata: dict[str, Any]) -> Optional[str]:
    if str(metadata.get("phase") or "").strip().lower() != "initial_sync":
        return None
    mode = (
        os.environ.get("OMNICODE_SYNC_SEMANTIC_INITIAL_MODE") or "auto"
    ).strip().lower()
    if mode in {"full", "all", "semantic"}:
        return None
    if mode in {"off", "none", "exact_only", "exact-only"}:
        return "initial_sync_exact_only"
    limit = semantic_initial_file_limit()
    if limit <= 0:
        return None
    files_seen = _metadata_int(metadata, "files_seen", "files_pushed")
    if files_seen > limit:
        return "initial_sync_large_repo_exact_only"
    return None


def content_bytes(content: str) -> int:
    return len(content.encode("utf-8", errors="replace"))


def semantic_index_decision(
    path: str,
    content: str,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    initial_skip_reason = semantic_initial_sync_skip_reason(metadata or {})
    if initial_skip_reason:
        return False, initial_skip_reason
    extensions = semantic_index_extensions()
    if extensions == frozenset():
        return False, "semantic_indexing_disabled"
    filename = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if (
        extensions is not None
        and suffix not in extensions
        and filename not in semantic_index_filenames()
    ):
        return False, "extension_not_enabled"
    max_bytes = semantic_index_max_file_bytes()
    if max_bytes is not None and content_bytes(content) > max_bytes:
        return False, "file_too_large"
    return True, "included"


def semantic_index_policy_payload() -> dict[str, Any]:
    extensions = semantic_index_extensions()
    return {
        "extensions": ["*"] if extensions is None else sorted(extensions),
        "filenames": sorted(semantic_index_filenames()),
        "max_file_bytes": semantic_index_max_file_bytes(),
        "initial_sync_mode": (
            os.environ.get("OMNICODE_SYNC_SEMANTIC_INITIAL_MODE") or "auto"
        ).strip().lower(),
        "initial_sync_file_limit": semantic_initial_file_limit(),
    }


def semantic_coverage_for_batch(
    *,
    files_enqueued: int,
    files_skipped: int,
    skip_reasons: dict[str, int],
    deletes: int,
) -> str:
    if any(reason.startswith("initial_sync") for reason in skip_reasons):
        return "exact_only_initial_sync"
    if files_enqueued > 0 and files_skipped > 0:
        return "filtered"
    if files_enqueued > 0:
        return "selected_files"
    if files_skipped > 0:
        return "filtered_empty"
    if deletes > 0:
        return "deletes_only"
    return "unchanged"


def merge_semantic_coverages(coverages: set[str]) -> str:
    cleaned = {item for item in coverages if item and item != "unknown"}
    if not cleaned:
        return "unknown"
    if "exact_only_initial_sync" in cleaned:
        return (
            "partial_after_exact_only"
            if len(cleaned - {"exact_only_initial_sync"}) > 0
            else "exact_only_initial_sync"
        )
    if "filtered" in cleaned:
        return "filtered"
    if "selected_files" in cleaned:
        return "selected_files"
    if "filtered_empty" in cleaned:
        return "filtered_empty"
    if "deletes_only" in cleaned:
        return "deletes_only"
    return sorted(cleaned)[0]

