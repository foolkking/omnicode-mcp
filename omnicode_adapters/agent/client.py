"""Stateless HTTP client for the OmniCode local-agent (Wave 2, W2-2).

Pure synchronous code on top of ``httpx`` so the watcher loop can call
into it from a thread executor without juggling event loops, AND the
unit tests can drive it against a fake transport.

Responsibilities:

* Read files off the local disk and POST them as JSON to
  ``/index/upsert-file``  /  ``/index/upsert-batch``.
* DELETE  ``/index/file`` for removed paths.
* Carry the bearer token (or X-API-Key) on every request.
* Retry transient failures with exponential backoff.

What it deliberately does NOT do:
* Apply patches on the local side. That stays in PatchManager — agent
  is sync-only for the index payload, not a patch loop.
* Long-poll the server for inbound changes. Pull mode is parked in
  Wave 2 W2-? for after we figure out conflict semantics.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

# Files we never push — common build/junk dirs and binary blobs we
# can't usefully chunk anyway. The watcher applies its own filter on
# top of this so it doesn't burn syscalls in the first place.
_DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".data/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "dist/",
    "build/",
)

_BINARY_EXTS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z",
        ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
        ".faiss", ".db", ".sqlite", ".sqlite3",
    }
)


@dataclass
class AgentResult:
    """Aggregate outcome of an agent push cycle."""

    pushed: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def merge(self, other: "AgentResult") -> "AgentResult":
        self.pushed += other.pushed
        self.deleted += other.deleted
        self.skipped += other.skipped
        self.errors.extend(other.errors)
        self.elapsed_ms += other.elapsed_ms
        return self

    def to_dict(self) -> dict:
        return {
            "pushed": self.pushed,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "elapsed_ms": self.elapsed_ms,
        }


def _is_binary_path(rel: str) -> bool:
    return Path(rel).suffix.lower() in _BINARY_EXTS


def _is_excluded(rel: str, extra: Sequence[str] = ()) -> bool:
    rel_norm = rel.replace("\\", "/")
    candidates = list(_DEFAULT_EXCLUDES) + list(extra)
    return any(
        rel_norm == pat.rstrip("/")
        or rel_norm.startswith(pat)
        for pat in candidates
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


class AgentClient:
    """HTTP client that talks to the remote ``/index/...`` endpoints."""

    def __init__(
        self,
        remote: str,
        token: Optional[str] = None,
        workspace: Optional[Path] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_file_bytes: int = 1_000_000,
        excludes: Sequence[str] = (),
    ) -> None:
        if not remote:
            raise ValueError("remote is required")
        self._remote = remote.rstrip("/")
        self._token = token or ""
        self._workspace = Path(workspace).resolve() if workspace else Path.cwd()
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_file_bytes = max_file_bytes
        self._excludes = tuple(excludes)
        self._client = client or httpx.Client(
            base_url=self._remote, timeout=timeout
        )

    # ------------------------------------------------------------ helpers
    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"X-API-Key": self._token}
        return {}

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._workspace)).replace(
                "\\", "/"
            )
        except ValueError:
            # Path was supplied relative to cwd already — best effort.
            return str(path).replace("\\", "/")

    def _read_text(self, rel: str) -> Optional[str]:
        full = self._workspace / rel
        if not full.is_file():
            return None
        try:
            if full.stat().st_size > self._max_file_bytes:
                logger.debug("agent: skipping oversized %s", rel)
                return None
        except OSError:
            return None
        try:
            return full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("agent: failed to read %s — %s", rel, exc)
            return None

    def _post(self, path: str, json: dict) -> httpx.Response:
        last_exc: Optional[Exception] = None
        backoff = 0.5
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._client.post(path, json=json, headers=self._headers())
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    logger.info(
                        "agent: %s %s failed (%s); retry %d/%d in %.1fs",
                        "POST",
                        path,
                        exc,
                        attempt,
                        self._max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
        # All attempts failed
        raise last_exc  # type: ignore[misc]

    def _request_delete(self, path: str, json: dict) -> httpx.Response:
        # httpx.Client.delete doesn't take a JSON body; use request().
        return self._client.request(
            "DELETE", path, json=json, headers=self._headers()
        )

    # ------------------------------------------------------------ public API
    def health(self) -> bool:
        """Probe the remote /health endpoint. Returns True iff 200."""
        try:
            r = self._client.get("/health", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    def push_file(self, path: str | Path) -> AgentResult:
        """Upload a single file body."""
        result = AgentResult()
        started = time.monotonic()

        rel = self._rel(Path(path)) if not isinstance(path, str) else path
        if _is_excluded(rel, self._excludes) or _is_binary_path(rel):
            result.skipped = 1
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        text = self._read_text(rel)
        if text is None:
            result.skipped = 1
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        body = {
            "file_path": rel,
            "content": text,
            "content_hash": _content_hash(text),
        }
        try:
            r = self._post("/index/upsert-file", body)
            if r.status_code == 200:
                result.pushed = 1
            else:
                result.errors.append(
                    f"{rel}: HTTP {r.status_code} — {r.text[:200]}"
                )
        except Exception as exc:
            result.errors.append(f"{rel}: {exc}")

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def push_batch(self, paths: Iterable[str | Path]) -> AgentResult:
        """Upload many files in one HTTP round-trip.

        Skips excluded / binary / oversized files. Returns the
        aggregate result so the caller can log a one-line summary.
        """
        result = AgentResult()
        started = time.monotonic()
        files: list[dict] = []

        for path in paths:
            rel = self._rel(Path(path)) if not isinstance(path, str) else path
            if _is_excluded(rel, self._excludes) or _is_binary_path(rel):
                result.skipped += 1
                continue
            text = self._read_text(rel)
            if text is None:
                result.skipped += 1
                continue
            files.append(
                {
                    "file_path": rel,
                    "content": text,
                    "content_hash": _content_hash(text),
                }
            )

        if not files:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        try:
            r = self._post("/index/upsert-batch", {"files": files})
            if r.status_code == 200:
                payload = r.json().get("result", {})
                result.pushed = payload.get("total_indexed", 0)
                for entry in payload.get("errors", []) or []:
                    result.errors.append(
                        f"{entry.get('file_path')}: {entry.get('error')}"
                    )
            else:
                result.errors.append(
                    f"batch: HTTP {r.status_code} — {r.text[:200]}"
                )
        except Exception as exc:
            result.errors.append(f"batch: {exc}")

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def delete_file(self, path: str | Path) -> AgentResult:
        """Tell the remote to drop ``path`` from its index."""
        result = AgentResult()
        started = time.monotonic()
        rel = self._rel(Path(path)) if not isinstance(path, str) else path
        try:
            r = self._request_delete("/index/file", {"file_path": rel})
            if r.status_code == 200:
                result.deleted = 1
            else:
                result.errors.append(
                    f"{rel}: HTTP {r.status_code} — {r.text[:200]}"
                )
        except Exception as exc:
            result.errors.append(f"{rel}: {exc}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def sync_status(self) -> dict:
        """Read the remote index headline. Used by the agent on
        startup to decide whether a full re-push is warranted."""
        try:
            r = self._client.get("/index/sync-status", headers=self._headers())
            if r.status_code == 200:
                return r.json().get("result", {})
            return {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


__all__ = ["AgentClient", "AgentResult"]
