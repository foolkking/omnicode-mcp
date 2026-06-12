"""Large-repo hybrid benchmark against a live Django snapshot backend.

These tests are intentionally not part of the normal unit suite. Run them with
an already-started benchmark backend, for example:

    pytest tests/benchmarks -m large_repo

Environment overrides:
    OMNICODE_BENCH_BACKEND_URL=http://127.0.0.1:6819
    OMNICODE_BENCH_WORKSPACE_ID=django-cleanroom-bench
    OMNICODE_BENCH_REPO=C:/omnicode-sim/benchmark-repos/django

For a fully self-contained clean-room run, prefer:

    python scripts/benchmark_large_repo_hybrid.py \
        --repo C:/omnicode-sim/benchmark-repos/django \
        --state-dir C:/omnicode-sim/state-bench-django \
        --workspace-id django-cleanroom-bench \
        --reset-state
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests


pytestmark = pytest.mark.large_repo


@dataclass(frozen=True)
class BenchConfig:
    backend_url: str
    workspace_id: str
    repo: Path


def _config() -> BenchConfig:
    return BenchConfig(
        backend_url=os.environ.get(
            "OMNICODE_BENCH_BACKEND_URL",
            "http://127.0.0.1:6819",
        ).rstrip("/"),
        workspace_id=os.environ.get(
            "OMNICODE_BENCH_WORKSPACE_ID",
            "django-cleanroom-bench",
        ),
        repo=Path(
            os.environ.get(
                "OMNICODE_BENCH_REPO",
                "C:/omnicode-sim/benchmark-repos/django",
            )
        ),
    )


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    **kwargs: Any,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=timeout,
        **kwargs,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed_ms


@pytest.fixture(scope="module")
def bench() -> BenchConfig:
    cfg = _config()
    if not cfg.repo.exists():
        pytest.skip(f"Django benchmark repo not found: {cfg.repo}")
    try:
        body, _elapsed = _request_json("GET", f"{cfg.backend_url}/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Django benchmark backend unavailable at {cfg.backend_url}: {exc}")
    status = body.get("result", {}).get("status")
    if status != "healthy":
        pytest.skip(f"Django benchmark backend is not healthy: {status!r}")
    return cfg


def _workspace_headers(cfg: BenchConfig, *, min_revision: int | None = None) -> dict[str, str]:
    headers = {"X-Omnicode-Workspace": cfg.workspace_id}
    if min_revision is not None:
        headers["X-Omnicode-Min-Revision"] = str(min_revision)
    return headers


def _sync_status(cfg: BenchConfig) -> tuple[dict[str, Any], float]:
    return _request_json(
        "GET",
        f"{cfg.backend_url}/sync/status",
        params={"workspace_id": cfg.workspace_id},
        timeout=5,
    )


def _assert_workspace_relative(path: str) -> None:
    assert path
    assert ":\\" not in path
    assert ":/" not in path
    assert not path.startswith("/")
    assert ".." not in Path(path).parts


def test_django_snapshot_status_is_observable(bench: BenchConfig) -> None:
    status, elapsed_ms = _sync_status(bench)
    file_count = len(
        [
            path
            for path in bench.repo.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ]
    )

    assert elapsed_ms < 1000
    assert status["ok"] is True
    assert status["accepted_revision"] >= status["indexed_revision"]
    assert status["snapshot_store"]["files"] >= 6000
    assert status["snapshot_ready"] is True
    assert status["exact_index_ready"] is True
    assert status["exact_index"]["files"] >= 6000
    assert status["exact_index"]["symbols"] > 0
    assert "line_fts_available" in status["exact_index"]
    assert status["recommended_query_mode"] in {"exact_first", "semantic_first"}
    assert status["query_mode_reason"] in {
        "exact_only_initial_sync",
        "semantic_full",
    }
    assert status["exact_query_safe"] is True
    assert status["strict_semantic_safe"] is bool(status["semantic_index_ready"])
    assert status["index_readiness_contract"]["schema_version"] == "index_readiness.v1"
    assert file_count >= 6000
    assert status.get("last_index_error") in (None, "")


@pytest.mark.parametrize(
    ("symbol", "expected_file"),
    [
        ("BaseHandler", "django/core/handlers/base.py"),
        ("URLResolver", "django/urls/resolvers.py"),
        ("QuerySet", "django/db/models/query.py"),
        ("Model", "django/db/models/base.py"),
        ("MiddlewareMixin", "django/utils/deprecation.py"),
    ],
)
def test_django_exact_symbols_bootstrap_from_snapshot(
    bench: BenchConfig,
    symbol: str,
    expected_file: str,
) -> None:
    body, elapsed_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search/symbols",
        headers=_workspace_headers(bench),
        params={"query": symbol, "fuzzy": "false", "max_results": 3},
        timeout=15,
    )

    result = body["result"]
    first = result["results"][0]
    assert elapsed_ms < 10_000
    assert body["success"] is True
    assert result["snapshot_store_used"] is True
    assert result["snapshot_fast_path"] is True
    assert first["file_path"] == expected_file
    assert first["symbol_name"] == symbol
    assert "symbol:exact" in first["why_matched"]
    assert first["source"] == "snapshot_store"
    _assert_workspace_relative(first["file_path"])


def test_django_text_search_bootstraps_from_snapshot(bench: BenchConfig) -> None:
    body, elapsed_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search/text",
        headers=_workspace_headers(bench),
        params={
            "query": "class BaseHandler:",
            "file_pattern": "*.py",
            "case_sensitive": "true",
            "max_results": 3,
            "context_lines": 1,
        },
        timeout=15,
    )

    result = body["result"]
    first = result["results"][0]
    assert elapsed_ms < 10_000
    assert body["success"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["line_content"] == "class BaseHandler:"
    assert first["source"] in {"snapshot_mirror", "snapshot_store"}
    _assert_workspace_relative(first["file_path"])


def test_django_semantic_search_boosts_exact_snapshot_symbol(
    bench: BenchConfig,
) -> None:
    body, elapsed_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search",
        headers=_workspace_headers(bench),
        json={"query": "BaseHandler", "search_type": "semantic", "max_results": 5},
        timeout=30,
    )

    result = body["result"]
    first = result["results"][0]
    assert elapsed_ms < 10_000
    assert body["success"] is True
    assert result["snapshot_store_used"] is True
    assert result["snapshot_exact_boost"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["symbol_name"] == "BaseHandler"
    assert first["source"] == "snapshot_store"
    assert "semantic:exact_boost" in first["why_matched"]
    _assert_workspace_relative(first["file_path"])


def test_django_semantic_search_boosts_natural_language_lexical_overlap(
    bench: BenchConfig,
) -> None:
    body, elapsed_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search",
        headers=_workspace_headers(bench),
        json={
            "query": "request middleware chain",
            "search_type": "semantic",
            "max_results": 5,
        },
        timeout=30,
    )

    result = body["result"]
    first = result["results"][0]
    assert elapsed_ms < 10_000
    assert body["success"] is True
    assert result["snapshot_store_used"] is True
    assert result["snapshot_lexical_boost"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] == "snapshot_store"
    assert "semantic:lexical_boost" in first["why_matched"]
    _assert_workspace_relative(first["file_path"])


def test_django_strict_freshness_blocks_stale_analysis(bench: BenchConfig) -> None:
    status, _elapsed = _sync_status(bench)
    required = max(
        int(status["accepted_revision"]),
        int(status["indexed_revision"]),
    ) + 1

    search, search_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search/symbols",
        headers=_workspace_headers(bench, min_revision=required),
        params={"query": "BaseHandler", "fuzzy": "false", "max_results": 3},
        timeout=5,
    )
    context, context_ms = _request_json(
        "POST",
        f"{bench.backend_url}/intelligence/context",
        headers={
            **_workspace_headers(bench, min_revision=required),
            "Content-Type": "application/json",
        },
        json={
            "file_path": "django/core/handlers/base.py",
            "symbol": "BaseHandler",
            "query": "BaseHandler",
            "token_budget": 2000,
            "include_memory": False,
        },
        timeout=5,
    )
    impact, impact_ms = _request_json(
        "GET",
        f"{bench.backend_url}/graph/impact",
        headers=_workspace_headers(bench, min_revision=required),
        params={"symbol": "BaseHandler", "depth": 2, "max_files": 200},
        timeout=5,
    )

    for payload, elapsed_ms in (
        (search, search_ms),
        (context, context_ms),
        (impact, impact_ms),
    ):
        assert elapsed_ms < 1000
        assert payload["ok"] is False
        assert payload["success"] is False
        assert payload["stale"] is True
        assert payload["error"] == "Cloud index is stale"


def test_django_exact_only_status_blocks_strict_semantic(
    bench: BenchConfig,
) -> None:
    status, _elapsed = _sync_status(bench)
    if status.get("semantic_initial_exact_only") is not True:
        pytest.skip("workspace is not in exact-only initial-sync mode")

    required = int(status["accepted_revision"])
    payload, elapsed_ms = _request_json(
        "POST",
        f"{bench.backend_url}/search",
        headers=_workspace_headers(bench, min_revision=required),
        json={"query": "BaseHandler", "search_type": "semantic", "max_results": 5},
        timeout=10,
    )

    assert elapsed_ms < 1000
    assert payload["ok"] is False
    assert payload["success"] is False
    assert payload["stale"] is True
    assert payload["freshness"] == "exact_fresh"
    assert payload["recommended_query_mode"] == "exact_first"
    assert payload["query_mode_reason"] == "exact_only_initial_sync"
    assert payload["exact_query_safe"] is True
    assert payload["strict_semantic_safe"] is False


def test_django_status_stays_responsive_during_snapshot_text_search(
    bench: BenchConfig,
) -> None:
    search_result: dict[str, Any] = {}

    def _run_search() -> None:
        try:
            body, elapsed_ms = _request_json(
                "POST",
                f"{bench.backend_url}/search/text",
                headers=_workspace_headers(bench),
                params={
                    "query": "class BaseHandler:",
                    "file_pattern": "*.py",
                    "case_sensitive": "true",
                    "max_results": 3,
                    "context_lines": 1,
                },
                timeout=15,
            )
            search_result["body"] = body
            search_result["elapsed_ms"] = elapsed_ms
        except Exception as exc:  # pragma: no cover - reported below
            search_result["error"] = repr(exc)

    thread = threading.Thread(target=_run_search, daemon=True)
    thread.start()
    time.sleep(0.1)

    samples: list[float] = []
    for _idx in range(5):
        status, elapsed_ms = _sync_status(bench)
        assert status["ok"] is True
        samples.append(elapsed_ms)
        time.sleep(0.2)

    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in search_result, search_result.get("error")
    assert search_result["body"]["success"] is True
    assert max(samples) < 1000
