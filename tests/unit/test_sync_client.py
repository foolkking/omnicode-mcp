"""Tests for the /sync protocol client."""

from __future__ import annotations

import json

import httpx
import pytest

from omnicode_core.workspace.sync_client import SyncClient
from omnicode_core.workspace.sync_queue import SyncBatch, SyncDelete, SyncFile


def _client(handler, **kwargs) -> SyncClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url="http://cloud")
    return SyncClient(
        remote="http://cloud",
        workspace_id=kwargs.pop("workspace_id", "repo-a"),
        token=kwargs.pop("token", "tok"),
        executor=kwargs.pop("executor", "hybrid"),
        client_id=kwargs.pop("client_id", "local-1"),
        client=inner,
        **kwargs,
    )


def _batch() -> SyncBatch:
    return SyncBatch(
        client_id="local-1",
        base_revision=1,
        client_revision=2,
        files=[
            SyncFile(
                path="src/app.py",
                hash="sha256:abc",
                size=11,
                mtime_ms=123,
                encoding="utf-8",
                content="print(1)\n",
            )
        ],
        deletes=[SyncDelete(path="src/old.py")],
    )


def test_push_batch_posts_sync_payload_and_headers() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"ok": True, "accepted_revision": 2, "indexed_revision": 1},
        )

    result = _client(handler).push_batch(_batch())

    assert result.ok is True
    assert result.accepted_revision == 2
    assert result.indexed_revision == 1
    assert captured["path"] == "/sync/batch"
    assert captured["headers"]["x-api-key"] == "tok"
    assert captured["headers"]["x-omnicode-workspace"] == "repo-a"
    assert captured["headers"]["x-omnicode-executor"] == "hybrid"
    assert captured["headers"]["x-omnicode-client"] == "local-1"
    assert captured["body"]["client_revision"] == 2
    assert captured["body"]["files"][0]["path"] == "src/app.py"
    assert captured["body"]["deletes"][0]["path"] == "src/old.py"


def test_delete_batch_uses_sync_batch_endpoint() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "accepted_revision": 3})

    result = _client(handler).delete_batch(["src/old.py"])

    assert result.ok is True
    assert captured["body"]["files"] == []
    assert captured["body"]["deletes"] == [{"path": "src/old.py"}]


def test_status_and_capabilities_parse_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/status":
            return httpx.Response(200, json={"ok": True, "accepted_revision": 4})
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"ok": True, "mode": "hybrid"})
        return httpx.Response(404, json={"ok": False, "error": "missing"})

    client = _client(handler)

    assert client.status().payload["accepted_revision"] == 4
    assert client.capabilities().payload["mode"] == "hybrid"


def test_barrier_ready_and_stale_fields() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "ready": False,
                "stale": True,
                "accepted_revision": 42,
                "indexed_revision": 40,
            },
        )

    result = _client(handler).barrier(
        min_revision=42, paths=["src/app.py"], wait_ms=500,
    )

    assert result.ok is True
    assert result.ready is False
    assert result.stale is True
    assert result.accepted_revision == 42
    assert result.indexed_revision == 40
    assert captured["body"] == {
        "min_revision": 42,
        "paths": ["src/app.py"],
        "wait_ms": 500,
    }


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_errors_mark_cloud_unavailable(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": False, "error": "no"})

    result = _client(handler).push_batch(_batch())

    assert result.ok is False
    assert result.cloud_unavailable is True
    assert result.retryable is False
    assert result.error == "no"


def test_network_error_is_retryable_and_preserves_queue_semantics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    result = _client(handler).push_batch(_batch())

    assert result.ok is False
    assert result.retryable is True
    assert result.cloud_unavailable is True
    assert "down" in (result.error or "")


def test_remote_and_workspace_are_required() -> None:
    with pytest.raises(ValueError):
        SyncClient(remote="", workspace_id="repo-a")
    with pytest.raises(ValueError):
        SyncClient(remote="http://cloud", workspace_id="")
