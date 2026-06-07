"""Unit tests for the local-agent /sync client."""

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


def test_is_excluded_default_dirs() -> None:
    assert _is_excluded(".git/index", ()) is True
    assert _is_excluded("node_modules/foo/bar.js", ()) is True
    assert _is_excluded(".data/snapshots/x.snapshot", ()) is True
    assert _is_excluded("src/main.py", ()) is False


def test_is_excluded_extra_pattern() -> None:
    assert _is_excluded("docs/internal.md", ("docs/",)) is True
    assert _is_excluded("docs/public.md", ()) is False


def test_is_binary_path() -> None:
    assert _is_binary_path("img.png") is True
    assert _is_binary_path("data.SQLITE") is True
    assert _is_binary_path("src/main.py") is False


def test_content_hash_stable_across_calls() -> None:
    a = _content_hash("hello\nworld\n")
    b = _content_hash("hello\nworld\n")
    assert a == b
    assert a != _content_hash("hello\nWorld\n")


def _client_with_handler(handler, **overrides) -> AgentClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url="http://test")
    workspace = overrides.pop("workspace", Path.cwd())
    if "manifest_path" not in overrides and overrides.get("workspace_id"):
        overrides["manifest_path"] = Path(workspace) / ".agent-manifest.json"
    return AgentClient(
        remote="http://test",
        token=overrides.pop("token", "k1"),
        workspace=workspace,
        client=inner,
        **overrides,
    )


def _sync_status_response(revision: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": revision,
            "indexed_revision": revision,
        },
    )


def _sync_batch_response(revision: int = 1, files: int = 1, deletes: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "workspace_id": "repo-a",
            "accepted_revision": revision,
            "indexed_revision": revision,
            "files_accepted": files,
            "deletes_accepted": deletes,
        },
    )


def test_push_file_sends_sync_batch_with_relative_path(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    calls: list[tuple[str, dict[str, str], dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.startswith("/index/")
        if request.url.path == "/sync/status":
            return _sync_status_response(0)
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, dict(request.headers), body))
        return _sync_batch_response(1)

    client = _client_with_handler(handler, workspace=tmp_path, workspace_id="repo-a")
    result = client.push_file("hello.py")

    assert result.pushed == 1
    assert result.errors == []
    assert result.accepted_revision == 1
    assert result.indexed_revision == 1
    assert result.sync_protocol == "/sync/batch"
    assert len(calls) == 1
    path, headers, body = calls[0]
    assert path == "/sync/batch"
    assert headers["x-omnicode-workspace"] == "repo-a"
    assert body["client_revision"] == 1
    assert body["files"][0]["path"] == "hello.py"
    assert body["files"][0]["hash"].startswith("sha256:")
    assert "file_path" not in body["files"][0]
    assert "C:\\" not in body["files"][0]["path"]


def test_push_file_records_local_manifest_after_sync_ack(tmp_path: Path) -> None:
    target = tmp_path / "hello.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(10)
        return _sync_batch_response(11)

    client = _client_with_handler(
        handler,
        workspace=tmp_path,
        workspace_id="repo-a",
        manifest_path=manifest_path,
    )
    result = client.push_file("hello.py")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.accepted_revision == 11
    assert data["workspace_id"] == "repo-a"
    assert data["local_revision"] == 11
    assert data["last_accepted_revision"] == 11
    assert data["last_indexed_revision"] == 11
    assert data["pending"] == []
    assert data["files"]["hello.py"]["hash"].startswith("sha256:")
    assert data["files"]["hello.py"]["last_uploaded_revision"] == 11


def test_push_file_requires_workspace_id(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {request.url.path}")

    client = _client_with_handler(handler, workspace=tmp_path)
    result = client.push_file("hello.py")

    assert result.pushed == 0
    assert result.errors == ["workspace_id is required for /sync"]


def test_push_file_skips_excluded_paths(tmp_path: Path) -> None:
    target = tmp_path / ".git" / "config"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    client = _client_with_handler(lambda r: httpx.Response(500), workspace=tmp_path)
    result = client.push_file(".git/config")
    assert result.skipped == 1
    assert result.pushed == 0


def test_push_file_skips_binary(tmp_path: Path) -> None:
    target = tmp_path / "logo.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = _client_with_handler(lambda r: httpx.Response(500), workspace=tmp_path)
    result = client.push_file("logo.png")
    assert result.skipped == 1


def test_push_file_skips_oversized(tmp_path: Path) -> None:
    target = tmp_path / "huge.py"
    target.write_text("x" * 5000, encoding="utf-8")
    client = _client_with_handler(
        lambda r: httpx.Response(500),
        workspace=tmp_path,
        max_file_bytes=100,
    )
    result = client.push_file("huge.py")
    assert result.skipped == 1


def test_push_batch_sends_one_sync_request(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "evil").write_text("x", encoding="utf-8")
    seen_batches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.startswith("/index/")
        if request.url.path == "/sync/status":
            return _sync_status_response(4)
        body = json.loads(request.content.decode("utf-8"))
        seen_batches.append(body)
        return _sync_batch_response(5, files=2)

    client = _client_with_handler(handler, workspace=tmp_path, workspace_id="repo-a")
    result = client.push_batch(["a.py", "b.py", ".git/evil"])

    assert result.pushed == 2
    assert result.skipped == 1
    assert result.accepted_revision == 5
    assert len(seen_batches) == 1
    assert {f["path"] for f in seen_batches[0]["files"]} == {"a.py", "b.py"}
    assert all("file_path" not in f for f in seen_batches[0]["files"])


def test_push_batch_chunks_by_file_count(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    seen_batches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(4)
        body = json.loads(request.content.decode("utf-8"))
        seen_batches.append(body)
        return _sync_batch_response(
            revision=body["client_revision"],
            files=len(body["files"]),
        )

    client = _client_with_handler(
        handler,
        workspace=tmp_path,
        workspace_id="repo-a",
        batch_max_files=2,
    )
    result = client.push_batch(["a.py", "b.py", "c.py"])

    assert result.pushed == 3
    assert result.errors == []
    assert result.accepted_revision == 6
    assert len(seen_batches) == 2
    assert [len(batch["files"]) for batch in seen_batches] == [2, 1]
    assert [batch["client_revision"] for batch in seen_batches] == [5, 6]


def test_push_batch_preserves_zero_files_accepted(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(10)
        return _sync_batch_response(revision=11, files=0)

    client = _client_with_handler(
        handler,
        workspace=tmp_path,
        workspace_id="repo-a",
    )
    result = client.push_batch(["a.py"])

    assert result.pushed == 0
    assert result.accepted_revision == 11
    assert result.errors == []


def test_delete_file_uses_sync_delete_entry(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.startswith("/index/")
        if request.url.path == "/sync/status":
            return _sync_status_response(7)
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _sync_batch_response(8, files=0, deletes=1)

    client = _client_with_handler(handler, workspace=tmp_path, workspace_id="repo-a")
    result = client.delete_file("gone.py")

    assert result.deleted == 1
    assert result.accepted_revision == 8
    assert seen["method"] == "POST"
    assert seen["path"] == "/sync/batch"
    assert seen["body"]["deletes"] == [{"path": "gone.py"}]


def test_delete_file_updates_local_manifest(tmp_path: Path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(20)
        body = json.loads(request.content.decode("utf-8"))
        return _sync_batch_response(
            body["client_revision"],
            files=len(body["files"]),
            deletes=len(body["deletes"]),
        )

    client = _client_with_handler(
        handler,
        workspace=tmp_path,
        workspace_id="repo-a",
        manifest_path=manifest_path,
    )
    assert client.push_file("gone.py").accepted_revision == 21
    target.unlink()
    assert client.delete_file("gone.py").accepted_revision == 22

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["local_revision"] == 22
    assert "gone.py" not in data["files"]


def test_post_retries_then_succeeds(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x", encoding="utf-8")
    attempts = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(0)
        attempts["posts"] += 1
        if attempts["posts"] < 2:
            raise httpx.ConnectError("simulated", request=request)
        return _sync_batch_response(1)

    client = _client_with_handler(handler, workspace=tmp_path, workspace_id="repo-a")
    client._max_retries = 3  # type: ignore[attr-defined]
    result = client.push_file("x.py")
    assert result.pushed == 1
    assert attempts["posts"] == 2


def test_post_retries_exhausted_records_error(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return _sync_status_response(0)
        raise httpx.ConnectError("nope", request=request)

    client = _client_with_handler(handler, workspace=tmp_path, workspace_id="repo-a")
    client._max_retries = 1  # type: ignore[attr-defined]
    result = client.push_file("x.py")
    assert result.pushed == 0
    assert result.errors and "nope" in result.errors[0]


def test_health_returns_true_when_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    assert client.health() is True


def test_health_returns_false_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = _client_with_handler(handler)
    assert client.health() is False


def test_sync_status_parses_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sync/status"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "workspace_id": "repo-a",
                "accepted_revision": 5,
                "indexed_revision": 5,
                "indexed_files": 5,
                "indexed_chunks": 42,
            },
        )

    client = _client_with_handler(handler, workspace_id="repo-a")
    out = client.sync_status()
    assert out["indexed_files"] == 5
    assert out["indexed_chunks"] == 42


def test_sync_status_requires_workspace_id() -> None:
    client = _client_with_handler(lambda r: httpx.Response(500))
    assert client.sync_status() == {"error": "workspace_id is required for /sync"}


def test_remote_url_required() -> None:
    with pytest.raises(ValueError):
        AgentClient(remote="", token="x")
