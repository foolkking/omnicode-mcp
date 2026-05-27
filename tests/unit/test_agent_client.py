"""Unit tests for the local-agent HTTP client (Wave 2, W2-2)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnicode_adapters.agent.client import (
    AgentClient,
    _content_hash,
    _is_binary_path,
    _is_excluded,
)


# ---------------------------------------------------------------------------
# Path / content helpers
# ---------------------------------------------------------------------------
def test_is_excluded_default_dirs():
    assert _is_excluded(".git/index", ()) is True
    assert _is_excluded("node_modules/foo/bar.js", ()) is True
    assert _is_excluded(".data/snapshots/x.snapshot", ()) is True
    assert _is_excluded("src/main.py", ()) is False


def test_is_excluded_extra_pattern():
    assert _is_excluded("docs/internal.md", ("docs/",)) is True
    assert _is_excluded("docs/public.md", ()) is False


def test_is_binary_path():
    assert _is_binary_path("img.png") is True
    assert _is_binary_path("data.SQLITE") is True
    assert _is_binary_path("src/main.py") is False


def test_content_hash_stable_across_calls():
    a = _content_hash("hello\nworld\n")
    b = _content_hash("hello\nworld\n")
    assert a == b
    assert a != _content_hash("hello\nWorld\n")


# ---------------------------------------------------------------------------
# Client behaviour
# ---------------------------------------------------------------------------
def _client_with_handler(handler, **overrides):
    """Build an AgentClient backed by a custom MockTransport."""
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url="http://test")
    return AgentClient(
        remote="http://test",
        token=overrides.pop("token", "k1"),
        workspace=overrides.pop("workspace", Path.cwd()),
        client=inner,
        **overrides,
    )


def test_push_file_sends_json_with_path_and_hash(tmp_path: Path):
    target = tmp_path / "hello.py"
    target.write_text("print('hi')\n", encoding="utf-8")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"result": {"file_path": "hello.py", "chunks_indexed": 3}},
        )

    client = _client_with_handler(handler, workspace=tmp_path)
    result = client.push_file("hello.py")
    assert result.pushed == 1
    assert not result.errors
    assert captured["path"] == "/index/upsert-file"
    assert captured["headers"].get("x-api-key") == "k1"
    assert captured["body"]["file_path"] == "hello.py"
    assert captured["body"]["content"].startswith("print(")
    assert len(captured["body"]["content_hash"]) == 64  # sha256 hex


def test_push_file_skips_excluded_paths(tmp_path: Path):
    target = tmp_path / ".git" / "config"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    client = _client_with_handler(
        lambda r: httpx.Response(500), workspace=tmp_path
    )
    result = client.push_file(".git/config")
    assert result.skipped == 1
    assert result.pushed == 0


def test_push_file_skips_binary(tmp_path: Path):
    target = tmp_path / "logo.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = _client_with_handler(
        lambda r: httpx.Response(500), workspace=tmp_path
    )
    result = client.push_file("logo.png")
    assert result.skipped == 1


def test_push_file_skips_oversized(tmp_path: Path):
    target = tmp_path / "huge.py"
    target.write_text("x" * 5000, encoding="utf-8")
    client = _client_with_handler(
        lambda r: httpx.Response(500),
        workspace=tmp_path,
        max_file_bytes=100,
    )
    result = client.push_file("huge.py")
    assert result.skipped == 1


def test_push_batch_sends_one_request(tmp_path: Path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "evil").write_text("x", encoding="utf-8")

    seen_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "result": {
                    "indexed": [{"file_path": "a.py"}, {"file_path": "b.py"}],
                    "errors": [],
                    "total_indexed": 2,
                    "total_errors": 0,
                }
            },
        )

    client = _client_with_handler(handler, workspace=tmp_path)
    result = client.push_batch(["a.py", "b.py", ".git/evil"])
    assert result.pushed == 2
    assert result.skipped == 1
    assert len(seen_calls) == 1
    assert {f["file_path"] for f in seen_calls[0]["files"]} == {"a.py", "b.py"}


def test_delete_file_uses_delete_method(tmp_path: Path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"result": {"file_path": "gone.py", "removed": 4}}
        )

    client = _client_with_handler(handler, workspace=tmp_path)
    result = client.delete_file("gone.py")
    assert result.deleted == 1
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/index/file"
    assert seen["body"] == {"file_path": "gone.py"}


def test_post_retries_then_succeeds(tmp_path: Path):
    target = tmp_path / "x.py"
    target.write_text("x", encoding="utf-8")

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(
            200, json={"result": {"chunks_indexed": 1}}
        )

    client = _client_with_handler(handler, workspace=tmp_path)
    # Shorten retry delay so the test is fast.
    client._max_retries = 3  # type: ignore[attr-defined]
    result = client.push_file("x.py")
    assert result.pushed == 1
    assert attempts["n"] == 2


def test_post_retries_exhausted_records_error(tmp_path: Path):
    target = tmp_path / "x.py"
    target.write_text("x", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    client = _client_with_handler(handler, workspace=tmp_path)
    client._max_retries = 1  # type: ignore[attr-defined]
    result = client.push_file("x.py")
    assert result.pushed == 0
    assert result.errors and "nope" in result.errors[0]


def test_health_returns_true_when_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    assert client.health() is True


def test_health_returns_false_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = _client_with_handler(handler)
    assert client.health() is False


def test_sync_status_parses_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "indexed_files": 5,
                    "indexed_chunks": 42,
                    "embedding_model": "fake",
                    "working_dir": "/tmp/x",
                }
            },
        )

    client = _client_with_handler(handler)
    out = client.sync_status()
    assert out["indexed_files"] == 5
    assert out["indexed_chunks"] == 42


def test_remote_url_required():
    with pytest.raises(ValueError):
        AgentClient(remote="", token="x")
