"""Hybrid analysis freshness gate tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.unit.mcp_harness import build_tools, run


def _payload(raw: str) -> Dict[str, Any]:
    return json.loads(raw)


@pytest.fixture
def hybrid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    return home


def _write_manifest(
    home: Path,
    *,
    local_revision: int,
    accepted_revision: int,
    indexed_revision: int,
) -> None:
    manifest = home / ".omnicode" / "workspaces" / "repo-a" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": "repo-a",
                "workspace_root_hash": "test",
                "client_id": "local-test",
                "local_revision": local_revision,
                "last_accepted_revision": accepted_revision,
                "last_indexed_revision": indexed_revision,
                "files": {},
                "pending": [],
            }
        ),
        encoding="utf-8",
    )


def test_hybrid_search_blocks_when_cloud_index_is_stale(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=5,
        accepted_revision=5,
        indexed_revision=4,
    )
    tools = build_tools({
        "/sync/status": {
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": 5,
            "indexed_revision": 4,
        },
        "/search": {"results": [], "total_results": 0},
    })

    payload = _payload(run(tools["omni_search"](
        query="cloud marker",
        mode="semantic",
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["stale"] is True
    assert payload["freshness"] == "stale"
    assert payload["required_revision"] == 5
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/sync/status" in captured
    assert "/search" not in captured


def test_hybrid_search_allows_fresh_cloud_index(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=5,
        accepted_revision=5,
        indexed_revision=5,
    )
    tools = build_tools({
        "/sync/status": {
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": 5,
            "indexed_revision": 5,
        },
        "/search": {
            "results": [
                {
                    "file_path": "tests/tmp_cloudsim_fresh.py",
                    "symbol_name": "fresh_marker",
                    "line_number": 1,
                    "relevance_score": 0.99,
                    "snippet": "fresh_marker",
                }
            ],
            "total_results": 1,
        },
    })

    payload = _payload(run(tools["omni_search"](
        query="cloud marker",
        mode="semantic",
        format="json",
        max_results=1,
    )))

    assert payload["ok"] is True
    assert payload["freshness"] == "fresh"
    assert payload["stale"] is False
    assert payload["local_revision"] == 5
    assert payload["indexed_revision"] == 5
    assert payload["results"][0]["file"] == "tests/tmp_cloudsim_fresh.py"
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/search" in captured


def test_hybrid_symbol_search_allows_exact_fresh_when_semantic_lags(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=8,
        accepted_revision=8,
        indexed_revision=7,
    )
    tools = build_tools({
        "/sync/status": {
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": 8,
            "indexed_revision": 7,
            "exact_indexed_revision": 8,
        },
        "/search/symbols": {
            "results": [
                {
                    "file_path": "django/core/handlers/base.py",
                    "symbol_name": "BaseHandler",
                    "line_start": 33,
                    "line_end": 33,
                    "relevance_score": 1.0,
                    "source": "exact_index",
                    "confidence": "high",
                    "why_matched": ["symbol:exact", "exact_index"],
                }
            ],
            "total_results": 1,
            "freshness": "exact_fresh",
            "semantic_stale": True,
            "exact_indexed_revision": 8,
        },
    })

    payload = _payload(run(tools["omni_search"](
        query="BaseHandler",
        mode="symbol",
        format="json",
        max_results=1,
    )))

    assert payload["ok"] is True
    assert payload["freshness"] == "exact_fresh"
    assert payload["freshness_mode"] == "exact"
    assert payload["stale"] is False
    assert payload["semantic_stale"] is True
    assert payload["exact_indexed_revision"] == 8
    assert payload["results"][0]["file"] == "django/core/handlers/base.py"
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/search/symbols" in captured


def test_hybrid_analysis_blocks_when_local_revision_is_unknown(
    hybrid_env: Path,
) -> None:
    tools = build_tools({
        "/sync/status": {
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": 2,
            "indexed_revision": 2,
        },
        "/search": {"results": [], "total_results": 0},
    })

    payload = _payload(run(tools["omni_search"](
        query="cloud marker",
        mode="semantic",
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["freshness"] == "unknown"
    assert payload["freshness_unknown"] is True
    assert payload["manifest_present"] is False
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/search" not in captured


def test_hybrid_search_blocks_when_cloud_status_unavailable(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=9,
        accepted_revision=8,
        indexed_revision=8,
    )
    tools = build_tools({
        "/sync/status": {
            "error": "Request failed with status 502",
            "error_type": "HTTPError",
            "status_code": 502,
        },
        "/search/text": {"results": [], "total_results": 0},
    })

    payload = _payload(run(tools["omni_search"](
        query="local-still-works",
        mode="auto",
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["freshness"] == "unavailable"
    assert payload["cloud_unavailable"] is True
    assert payload["backend_unreachable"] is True
    assert "Cloud backend is unavailable" in payload["error"]
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/sync/status" in captured
    assert "/search/text" not in captured


def test_hybrid_context_blocks_when_cloud_status_unavailable(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=9,
        accepted_revision=8,
        indexed_revision=8,
    )
    tools = build_tools({
        "/sync/status": {
            "error": "Cannot connect to FastAPI server - server may be down",
            "error_type": "ConnectionError",
        },
        "/read": {"symbols": []},
    })

    payload = _payload(run(tools["omni_context"](
        file="tests/tmp_cloudsim_cloud_down.py",
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["freshness"] == "unavailable"
    assert payload["cloud_unavailable"] is True
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/read" not in captured


def test_hybrid_context_and_impact_share_stale_gate(
    hybrid_env: Path,
) -> None:
    _write_manifest(
        hybrid_env,
        local_revision=8,
        accepted_revision=8,
        indexed_revision=7,
    )
    tools = build_tools({
        "/sync/status": {
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": 8,
            "indexed_revision": 7,
        },
        "/read": {"symbols": []},
        "/graph/impact": {},
        "/graph/risk": {},
        "/graph/related-tests": {},
    })

    context_payload = _payload(run(tools["omni_context"](
        task="inspect stale cloud",
        format="json",
    )))
    impact_payload = _payload(run(tools["omni_impact"](
        symbol="route_value",
        format="json",
    )))

    assert context_payload["ok"] is False
    assert context_payload["freshness"] == "stale"
    assert impact_payload["ok"] is False
    assert impact_payload["freshness"] == "stale"
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/read" not in captured
    assert "/graph/impact" not in captured
    assert "/graph/risk" not in captured
    assert "/graph/related-tests" not in captured
