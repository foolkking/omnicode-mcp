from __future__ import annotations

from typing import Any

from omnicode_adapters.cli.commands import index_cmd


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, path: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", path, kwargs))
        if path == "/workspaces":
            return _Response({"ok": True})
        return _Response(
            {
                "success": True,
                "result": {
                    "background": True,
                    "job": {"job_id": "repo-a:1", "state": "running", "scope": "semantic"},
                },
            }
        )

    def get(self, path: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", path, kwargs))
        return _Response(
            {
                "success": True,
                "result": {
                    "workspace_id": "repo-a",
                    "state": "completed",
                    "job": {"job_id": "repo-a:1", "scope": "semantic"},
                },
            }
        )


def test_index_cli_passes_scope_to_bootstrap(monkeypatch, capsys) -> None:
    import httpx

    _Client.calls = []
    monkeypatch.setattr(httpx, "Client", _Client)

    index_cmd.run(
        backend_url="http://cloud",
        workspace_id="repo-a",
        scope="semantic",
        force=True,
        background=True,
    )

    assert _Client.calls == [
        (
            "POST",
            "/search/index",
            {
                "params": {
                    "force": True,
                    "background": True,
                    "scope": "semantic",
                    "workspace_id": "repo-a",
                },
                "headers": {"X-Omnicode-Workspace": "repo-a"},
            },
        )
    ]
    assert "Scope:" in capsys.readouterr().out


def test_index_cli_status_uses_status_endpoint(monkeypatch, capsys) -> None:
    import httpx

    _Client.calls = []
    monkeypatch.setattr(httpx, "Client", _Client)

    index_cmd.run(
        backend_url="http://cloud",
        workspace_id="repo-a",
        scope="semantic",
        status=True,
    )

    assert _Client.calls == [
        (
            "GET",
            "/search/index/status",
            {
                "params": {"workspace_id": "repo-a"},
                "headers": {"X-Omnicode-Workspace": "repo-a"},
            },
        )
    ]
    assert "Indexing status" in capsys.readouterr().out
