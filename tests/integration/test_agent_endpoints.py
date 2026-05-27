"""Integration tests for the agent REST endpoints (Wave 2, W2-2)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def test_sync_status_returns_indexed_counts(client):
    r = client.get("/index/sync-status")
    assert r.status_code == 200
    body = r.json()["result"]
    assert "indexed_files" in body
    assert "indexed_chunks" in body
    assert "working_dir" in body


def test_upsert_file_indexes_synthetic_content(client):
    body = {
        "file_path": "_agent_smoke.py",
        "content": "def hello():\n    return 42\n",
    }
    r = client.post("/index/upsert-file", json=body)
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["file_path"] == "_agent_smoke.py"
    assert isinstance(payload["chunks_indexed"], int)
    # Cleanup
    client.request("DELETE", "/index/file", json={"file_path": "_agent_smoke.py"})


def test_upsert_batch_handles_mixed_payload(client):
    body = {
        "files": [
            {"file_path": "_a.py", "content": "x = 1\n"},
            {"file_path": "_b.py", "content": "y = 2\n"},
        ]
    }
    r = client.post("/index/upsert-batch", json=body)
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["total_indexed"] >= 1
    assert "errors" in payload
    # Cleanup
    for fp in ("_a.py", "_b.py"):
        client.request("DELETE", "/index/file", json={"file_path": fp})


def test_upsert_rejects_path_traversal(client):
    """Sandbox must apply to agent endpoints too."""
    body = {
        "file_path": "../../../etc/passwd",
        "content": "evil",
    }
    r = client.post("/index/upsert-file", json=body)
    assert r.status_code == 403


def test_delete_file_returns_removed_count(client):
    # Index something so we can delete it back.
    client.post(
        "/index/upsert-file",
        json={"file_path": "_to_delete.py", "content": "z = 0\n"},
    )
    r = client.request(
        "DELETE", "/index/file", json={"file_path": "_to_delete.py"}
    )
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["file_path"] == "_to_delete.py"
    assert isinstance(body["removed"], int)
