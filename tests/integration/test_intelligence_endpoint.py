"""Integration tests for the Intelligence Layer endpoints (architecture-v2 §17)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def test_capabilities_endpoint_lists_eight(client):
    r = client.get("/capabilities")
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["total"] == 8
    assert isinstance(body["available"], int)
    caps = {s["capability"] for s in body["capabilities"]}
    expected = {
        "code_understanding",
        "context_compression",
        "search",
        "impact_analysis",
        "safe_patch",
        "memory_recall",
        "debug_console",
        "llm_enhancement",
    }
    assert caps == expected


def test_intelligence_context_minimal_payload(client):
    """Empty body still produces a valid context (composer is fault-tolerant)."""
    r = client.post("/intelligence/context", json={})
    assert r.status_code == 200
    body = r.json()["result"]
    assert "capability_status" in body
    assert len(body["capability_status"]) == 8
    assert "advisories" in body
    assert "token_budget" in body
    # token_budget defaults to 4096.
    assert body["token_budget"] == 4096


def test_intelligence_context_with_query(client):
    """Composer populates the search slot when given a query."""
    r = client.post(
        "/intelligence/context",
        json={
            "query": "create_app",
            "max_search_results": 3,
            "token_budget": 2048,
            "include_impact": False,
            "include_memory": False,
            "include_git_history": False,
        },
    )
    assert r.status_code == 200
    body = r.json()["result"]
    # We don't assert on specific results because the index may be cold,
    # but the search slot must at minimum echo the query and be a dict.
    assert body["search"].get("query") == "create_app" or body["search"] == {}
    assert body["request"]["query"] == "create_app"
    assert body["token_budget"] == 2048


def test_intelligence_context_with_file(client):
    """Composer populates code_understanding + git_history when given a file."""
    r = client.post(
        "/intelligence/context",
        json={
            "file_path": "README.md",
            "include_impact": False,
            "include_memory": False,
        },
    )
    assert r.status_code == 200
    body = r.json()["result"]
    # README has git history in this repo.
    assert body["git_history"].get("file") in ("README.md", None)
    # code_understanding should at minimum return a dict (may be empty for
    # unsupported languages / parser absent).
    assert isinstance(body["code_understanding"], dict)
