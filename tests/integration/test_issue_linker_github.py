"""STAGE 11.x — Issue Linker GitHub enrichment integration test (STAGE 5.5).

Spins up a tiny in-process HTTP server that mimics GitHub's
``/repos/{owner}/{repo}/issues/{number}`` endpoint, then patches
``urllib.request.urlopen`` so the IssueLinker hits our mock instead of the
real github.com.  Verifies the enrichment fields (state, title, author,
labels, url) flow through correctly.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

import pytest

from omnicode.git_context.issue_linker import IssueLinker, IssueReference


# ---------------------------------------------------------------------------
# Mock GitHub HTTP server
# ---------------------------------------------------------------------------
class _FakeGitHubHandler(BaseHTTPRequestHandler):
    payloads: Dict[int, Dict[str, Any]] = {}
    expected_token: str = "test-token-xyz"

    def log_message(self, *_args, **_kwargs) -> None:  # silence noisy default
        return

    def do_GET(self) -> None:
        # Parse /repos/<owner>/<repo>/issues/<n>
        parts = [p for p in self.path.split("/") if p]
        if len(parts) < 5 or parts[0] != "repos" or parts[3] != "issues":
            self.send_error(404)
            return
        try:
            number = int(parts[4])
        except ValueError:
            self.send_error(404)
            return
        # Verify the Authorization header is forwarded.
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_error(401, "missing bearer token")
            return
        if auth != f"Bearer {self.expected_token}":
            self.send_error(401, "wrong token")
            return

        payload = self.payloads.get(number)
        if payload is None:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_github_server():
    """Spin up an in-process HTTP mock and return its base URL."""
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHubHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Pre-populate the fake issue payloads we'll assert against.
    _FakeGitHubHandler.payloads = {
        42: {
            "state": "open",
            "title": "Crash on startup with empty .env",
            "user": {"login": "alice"},
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "html_url": f"{base_url}/octo/repo/issues/42",
        },
        7: {
            "state": "closed",
            "title": "Add support for Ollama",
            "user": {"login": "bob"},
            "labels": [{"name": "enhancement"}],
            "html_url": f"{base_url}/octo/repo/issues/7",
        },
    }

    yield base_url

    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_linker_with_mock(monkeypatch, tmp_path, fake_base_url, *, owner_repo=("octo", "repo")):
    """Construct a linker that thinks the local mock is api.github.com."""
    linker = IssueLinker(
        str(tmp_path),
        github_token=_FakeGitHubHandler.expected_token,
        enable_network=True,
    )
    # Force the linker to skip the (real) git remote parsing
    linker._owner_repo = owner_repo

    # Patch urlopen so api.github.com requests are redirected to our mock.
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, *args, **kwargs):
        if isinstance(req, urllib.request.Request):
            url = req.full_url
            headers = dict(req.headers)
        else:
            url = req
            headers = {}
        # Translate api.github.com → localhost:port
        if "api.github.com" in url:
            url = url.replace("https://api.github.com", fake_base_url)
            new_req = urllib.request.Request(url)
            for k, v in headers.items():
                new_req.add_header(k, v)
            return real_urlopen(new_req, *args, **kwargs)
        return real_urlopen(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return linker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestIssueLinkerGitHubEnrichment:
    def test_enrich_populates_state_title_author_labels_url(
        self, monkeypatch, tmp_path, fake_github_server
    ):
        linker = _make_linker_with_mock(monkeypatch, tmp_path, fake_github_server)
        refs = [
            IssueReference(raw="#42", kind="github", identifier="#42", number=42),
            IssueReference(raw="GH-7", kind="github_alt", identifier="GH-7", number=7),
        ]
        out = linker.enrich_with_github(refs)
        by_id = {r.identifier: r for r in out}
        assert by_id["#42"].state == "open"
        assert by_id["#42"].title == "Crash on startup with empty .env"
        assert by_id["#42"].author == "alice"
        assert "bug" in by_id["#42"].labels
        assert "p1" in by_id["#42"].labels
        assert by_id["#42"].url and "/issues/42" in by_id["#42"].url
        # Closing reference still gets enriched
        assert by_id["GH-7"].state == "closed"
        assert by_id["GH-7"].author == "bob"

    def test_unknown_issue_returns_404_no_crash(
        self, monkeypatch, tmp_path, fake_github_server
    ):
        linker = _make_linker_with_mock(monkeypatch, tmp_path, fake_github_server)
        refs = [
            IssueReference(
                raw="#9999", kind="github", identifier="#9999", number=9999
            ),
        ]
        # 404 should NOT raise — linker swallows it and leaves state=None
        out = linker.enrich_with_github(refs)
        assert out[0].state is None
        assert out[0].title is None

    def test_wrong_token_disables_network_for_subsequent(
        self, monkeypatch, tmp_path, fake_github_server
    ):
        linker = _make_linker_with_mock(monkeypatch, tmp_path, fake_github_server)
        # Override the linker's token with a bad one.
        linker.github_token = "wrong-token"
        refs = [
            IssueReference(raw="#42", kind="github", identifier="#42", number=42),
            IssueReference(raw="#7", kind="github", identifier="#7", number=7),
        ]
        out = linker.enrich_with_github(refs)
        # No enrichment happened
        assert out[0].state is None
        assert out[1].state is None
        # First failure flips off the network so no extra calls happen
        assert linker.enable_network is False

    def test_jira_references_are_skipped_no_github_call(
        self, monkeypatch, tmp_path, fake_github_server
    ):
        """Non-GitHub kinds (jira, gitlab_mr, ado) must not trigger HTTP calls."""
        linker = _make_linker_with_mock(monkeypatch, tmp_path, fake_github_server)
        refs = [
            IssueReference(
                raw="ABC-123", kind="jira", identifier="ABC-123",
                project="ABC", number=123,
            ),
            IssueReference(raw="!17", kind="gitlab_mr", identifier="!17", number=17),
            IssueReference(raw="AB#9", kind="ado", identifier="AB#9", number=9),
        ]
        out = linker.enrich_with_github(refs)
        # Nothing got enriched; linker is still network-enabled.
        for r in out:
            assert r.state is None and r.title is None
        assert linker.enable_network is True
