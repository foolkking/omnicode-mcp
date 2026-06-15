"""Large-repo hybrid benchmark against a live Kafka snapshot backend.

Run with an already-started benchmark backend:

    pytest tests/benchmarks/test_kafka_large_repo_hybrid.py -m large_repo

Environment overrides:
    OMNICODE_BENCH_BACKEND_URL=http://127.0.0.1:6819
    OMNICODE_BENCH_WORKSPACE_ID=kafka-cleanroom-bench
    OMNICODE_BENCH_REPO=C:/omnicode-sim/benchmark-repos/kafka

For a self-contained clean-room run, prefer:

    python scripts/benchmark_large_repo_hybrid.py \
        --repo C:/omnicode-sim/benchmark-repos/kafka \
        --state-dir .tmp_benchmarks/state-kafka \
        --workspace-id kafka-cleanroom-bench \
        --symbol ReplicaManager \
        --expected-file core/src/main/scala/kafka/server/ReplicaManager.scala \
        --text-query "class ReplicaManager" \
        --text-file-pattern "*.scala" \
        --reset-state
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests


pytestmark = pytest.mark.large_repo

KAFKA_SYMBOL = "ReplicaManager"
KAFKA_FILE = "core/src/main/scala/kafka/server/ReplicaManager.scala"
KAFKA_TEXT = "class ReplicaManager"


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
            "kafka-cleanroom-bench",
        ),
        repo=Path(
            os.environ.get(
                "OMNICODE_BENCH_REPO",
                "C:/omnicode-sim/benchmark-repos/kafka",
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
) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=timeout,
        **kwargs,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    payload = response.json()
    if response.status_code < 400:
        response.raise_for_status()
    return payload, elapsed_ms, response.status_code


@pytest.fixture(scope="module")
def bench() -> BenchConfig:
    cfg = _config()
    if not cfg.repo.exists():
        pytest.skip(f"Kafka benchmark repo not found: {cfg.repo}")
    try:
        body, _elapsed, _status_code = _request_json(
            "GET",
            f"{cfg.backend_url}/health",
            timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"Kafka benchmark backend unavailable at {cfg.backend_url}: {exc}")
    status = body.get("result", {}).get("status")
    if status != "healthy":
        pytest.skip(f"Kafka benchmark backend is not healthy: {status!r}")
    return cfg


def _workspace_headers(cfg: BenchConfig, *, min_revision: int | None = None) -> dict[str, str]:
    headers = {"X-Omnicode-Workspace": cfg.workspace_id}
    if min_revision is not None:
        headers["X-Omnicode-Min-Revision"] = str(min_revision)
    return headers


def _sync_status(cfg: BenchConfig) -> tuple[dict[str, Any], float]:
    body, elapsed_ms, _status_code = _request_json(
        "GET",
        f"{cfg.backend_url}/sync/status",
        params={"workspace_id": cfg.workspace_id},
        timeout=5,
    )
    return body, elapsed_ms


def _assert_workspace_relative(path: str) -> None:
    assert path
    assert ":\\" not in path
    assert ":/" not in path
    assert not path.startswith("/")
    assert ".." not in Path(path).parts


def test_kafka_snapshot_status_is_observable(bench: BenchConfig) -> None:
    status, elapsed_ms = _sync_status(bench)

    assert elapsed_ms < 1000
    assert status["ok"] is True
    assert status["snapshot_store"]["files"] >= 7000
    assert status["snapshot_ready"] is True
    assert status["exact_index_ready"] is True
    assert status["exact_index"]["files"] >= 7000
    assert status["exact_index"]["symbols"] > 0
    assert status["exact_index"]["line_fts_available"] is False
    assert status["recommended_query_mode"] == "exact_first"
    assert status["query_mode_reason"] == "exact_only_initial_sync"
    assert status["exact_query_safe"] is True
    assert status["strict_semantic_safe"] is False
    assert status["index_readiness_contract"]["schema_version"] == "index_readiness.v1"


def test_kafka_exact_symbol_prefers_class_over_companion_object(
    bench: BenchConfig,
) -> None:
    body, elapsed_ms, status_code = _request_json(
        "POST",
        f"{bench.backend_url}/search/symbols",
        headers=_workspace_headers(bench),
        params={"query": KAFKA_SYMBOL, "fuzzy": "false", "max_results": 5},
        timeout=15,
    )

    result = body["result"]
    first = result["results"][0]
    assert status_code == 200
    assert elapsed_ms < 3000
    assert body["success"] is True
    assert result["snapshot_fast_path"] is True
    assert first["file_path"] == KAFKA_FILE
    assert first["symbol_name"] == KAFKA_SYMBOL
    assert first["symbol_type"] == "class"
    assert first["line_start"] == 154
    assert first["signature"].startswith("class ReplicaManager")
    assert first["source"] == "exact_index"
    _assert_workspace_relative(first["file_path"])


def test_kafka_text_search_finds_class_declaration(bench: BenchConfig) -> None:
    body, elapsed_ms, status_code = _request_json(
        "POST",
        f"{bench.backend_url}/search/text",
        headers=_workspace_headers(bench),
        params={
            "query": KAFKA_TEXT,
            "file_pattern": "*.scala",
            "case_sensitive": "true",
            "max_results": 3,
            "context_lines": 1,
        },
        timeout=15,
    )

    first = body["result"]["results"][0]
    assert status_code == 200
    assert elapsed_ms < 5000
    assert first["file_path"] == KAFKA_FILE
    assert first["line_number"] == 154
    assert first["line_content"].startswith("class ReplicaManager")
    assert first["source"] in {"snapshot_mirror", "snapshot_store", "exact_index"}
    _assert_workspace_relative(first["file_path"])


def test_kafka_semantic_not_ready_returns_exact_fallback(
    bench: BenchConfig,
) -> None:
    body, elapsed_ms, status_code = _request_json(
        "POST",
        f"{bench.backend_url}/search",
        headers=_workspace_headers(bench),
        json={"query": KAFKA_SYMBOL, "search_type": "semantic", "max_results": 5},
        timeout=30,
    )

    result = body["result"]
    first = result["results"][0]
    assert status_code == 409
    assert elapsed_ms < 5000
    assert body["success"] is False
    assert result["ok"] is False
    assert result["error_code"] == "SEMANTIC_INDEX_NOT_READY"
    assert result["fallback_used"] is True
    assert result["capabilities_missing"] == ["search.semantic"]
    assert first["file_path"] == KAFKA_FILE
    assert first["symbol_name"] == KAFKA_SYMBOL
    assert first["line_start"] == 154
    assert first["rank_reason"] == "exact_symbol_before_semantic"
    _assert_workspace_relative(first["file_path"])


def test_kafka_context_uses_exact_snapshot_anchor(bench: BenchConfig) -> None:
    status, _elapsed = _sync_status(bench)
    body, elapsed_ms, status_code = _request_json(
        "POST",
        f"{bench.backend_url}/intelligence/context",
        headers=_workspace_headers(
            bench,
            min_revision=int(status["accepted_revision"]),
        ),
        json={
            "file_path": KAFKA_FILE,
            "symbol": KAFKA_SYMBOL,
            "query": KAFKA_SYMBOL,
            "include_memory": False,
            "include_git_history": False,
            "max_search_results": 5,
        },
        timeout=30,
    )

    result = body["result"]
    first = result["search"]["results"][0]
    assert status_code == 200
    assert elapsed_ms < 5000
    assert result["snapshot_exact_symbol"] is True
    assert result["freshness"] == "exact_fresh"
    assert first["file"] == KAFKA_FILE
    assert first["symbol"] == KAFKA_SYMBOL
    assert first["start_line"] == 154
    assert result["context_quality"]["primary_anchor"] == "snapshot_exact_symbol"
