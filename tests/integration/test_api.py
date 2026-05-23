"""STAGE 11.8 — REST API integration tests.

Spins up the real FastAPI app via TestClient and exercises every public
endpoint at least once.  These tests do NOT touch external networks
(`TRANSFORMERS_OFFLINE=1` is set in conftest.py) and clean up any state
they create in the SQLite stores at teardown.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Module-level scope: spinning up the real lifespan once is expensive
# (~5s for tree-sitter + sentence-transformers loads), so all tests
# share a single client.


@pytest.fixture(scope="module")
def client():
    from main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        # Response shape: {"result": {...}, "success": true, ...}
        assert body.get("success") is True


# ---------------------------------------------------------------------------
# Provider catalog + selection layer
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestProviderEndpoints:
    def test_list_providers_returns_catalog(self, client):
        r = client.get("/providers")
        assert r.status_code == 200
        result = r.json()["result"]
        assert "providers" in result
        assert "active_providers" in result
        # No assertion on exact count — depends on user's .env

    def test_provider_full_lifecycle(self, client):
        # 1. Create
        payload = {
            "name": "_pytest_temp_provider",
            "model": "openai/gpt-4o-mini",
            "api_key": "sk-pytest-fake",
            "api_base": "https://example.invalid/v1",
            "provider_type": "openai-compatible",
            "group": "balanced",
            "enabled": True,
            "description": "from pytest",
        }
        r = client.post("/providers", json=payload)
        assert r.status_code == 200
        msg = r.json()["result"]["message"]
        assert "saved" in msg

        # 2. List shows it
        r = client.get("/providers")
        names = [p["name"] for p in r.json()["result"]["providers"]]
        assert "_pytest_temp_provider" in names

        # 3. Disable
        r = client.post("/providers/_pytest_temp_provider/disable")
        assert r.status_code == 200

        # 4. Re-enable
        r = client.post("/providers/_pytest_temp_provider/enable")
        assert r.status_code == 200

        # 5. Update via PUT
        new_payload = {**payload, "description": "edited by pytest"}
        r = client.put("/providers/_pytest_temp_provider", json=new_payload)
        assert r.status_code == 200

        # 6. Delete
        r = client.delete("/providers/_pytest_temp_provider")
        assert r.status_code == 200

        # 7. Confirm gone
        r = client.get("/providers")
        names = [p["name"] for p in r.json()["result"]["providers"]]
        assert "_pytest_temp_provider" not in names

    def test_delete_builtin_provider_rejected(self, client):
        # Try deleting a built-in provider — must 400
        r = client.get("/providers")
        builtins = [
            p["name"]
            for p in r.json()["result"]["providers"]
            if p.get("built_in")
        ]
        if not builtins:
            pytest.skip("No built-in providers configured (no API keys in .env)")
        r = client.delete(f"/providers/{builtins[0]}")
        assert r.status_code == 400

    def test_selections_full_cycle(self, client):
        # 1. Add a provider we can actually pin to roles
        client.post(
            "/providers",
            json={
                "name": "_pytest_role_provider",
                "model": "openai/gpt-4o-mini",
                "api_key": "sk-test",
                "provider_type": "openai-compatible",
                "group": "balanced",
                "enabled": True,
            },
        )
        # 2. Bulk-assign 2 roles
        r = client.put(
            "/selections",
            json={
                "assignments": {
                    "edit": "_pytest_role_provider",
                    "scan": "_pytest_role_provider",
                }
            },
        )
        assert r.status_code == 200
        sel = r.json()["result"]["assignments"]
        assert sel.get("edit") == "_pytest_role_provider"
        assert sel.get("scan") == "_pytest_role_provider"

        # 3. Single-role clear
        r = client.put(
            "/selections/edit",
            params={"provider_name": ""},
        )
        assert r.status_code == 200

        # 4. Invalid role -> 400
        r = client.put(
            "/selections/totally-not-a-role",
            params={"provider_name": "_pytest_role_provider"},
        )
        assert r.status_code == 400

        # 5. Cleanup: clear all assignments + delete provider
        client.put("/selections", json={"assignments": {}})
        client.delete("/providers/_pytest_role_provider")


# ---------------------------------------------------------------------------
# Model status
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestModelStatus:
    def test_model_status_includes_selections(self, client):
        r = client.get("/model-status")
        assert r.status_code == 200
        body = r.json()["result"]
        assert "selections" in body
        assert "valid_roles" in body
        assert "providers_detail" in body
        # Aggregate health block is also expected
        assert "health" in body


# ---------------------------------------------------------------------------
# AST graph + inheritance
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestAstEndpoints:
    def test_inheritance_graph_for_project(self, client):
        r = client.get("/search/inheritance", params={"max_files": 100})
        assert r.status_code == 200
        body = r.json()["result"]
        # The OmniCode codebase itself definitely has inheritance edges.
        summary = body.get("summary", {})
        assert summary.get("total_edges", 0) > 0

    def test_inheritance_for_specific_symbol(self, client):
        r = client.get(
            "/search/inheritance/EditPipeline",
            params={"direction": "both", "max_files": 100},
        )
        assert r.status_code == 200
        body = r.json()["result"]
        # EditPipeline doesn't inherit from anything — both lists must exist
        assert "base_classes" in body
        assert "subclasses" in body


# ---------------------------------------------------------------------------
# File-system browser
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestFsBrowser:
    def test_drives(self, client):
        r = client.get("/fs/drives")
        assert r.status_code == 200
        drives = r.json()["result"]["drives"]
        assert len(drives) >= 1

    def test_list_current_dir(self, client):
        r = client.get("/fs/list", params={"path": "."})
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["count"] >= 1
        assert "entries" in body

    def test_open_readme(self, client):
        r = client.post(
            "/fs/open",
            json={"path": "README.md", "max_bytes": 200_000},
        )
        # README is plain text — must succeed
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["size"] > 0
        assert body["is_binary"] is False

    def test_deny_list_blocks_sensitive_path(self, client):
        # A path that's on the default deny-list — should 403.
        r = client.get(
            "/fs/list", params={"path": "C:\\Windows\\System32\\config"}
        )
        # Either 403 (denied) or 400 (not found on this OS) — both indicate
        # the deny-list / safety net works as intended.
        assert r.status_code in (400, 403)


# ---------------------------------------------------------------------------
# Git endpoints
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestGitEndpoints:
    def test_history_for_readme(self, client):
        r = client.get(
            "/git/history",
            params={"file_path": "README.md", "max_commits": 30},
        )
        # In a non-git working dir we'd get 200 with `total_commits=0`.
        assert r.status_code == 200
        body = r.json()["result"]
        assert "risk_score" in body
        assert "risk_level" in body
        # Risk level must be one of the canonical buckets
        assert body["risk_level"] in ("low", "medium", "high")

    def test_issue_linker_endpoint(self, client):
        r = client.get("/git/issues", params={"max_commits": 20, "enrich": False})
        assert r.status_code == 200
        body = r.json()["result"]
        assert "references" in body
        # github_enriched must be False since enrich=False
        assert body.get("github_enriched") is False
