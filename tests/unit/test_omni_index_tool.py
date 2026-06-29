from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

import pytest

from omnicode_adapters.mcp_server.high_level_tools import register_high_level_tools


class _ToolManagerStub:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}
        self._tool_manager = _ToolManagerStub()

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            self._tool_manager._tools[fn.__name__] = fn
            return fn

        return deco


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_omni_index_status_calls_backend_status() -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        captured.append((method, endpoint, kwargs))
        return {
            "result": {
                "workspace_id": "repo-a",
                "background": True,
                "state": "completed",
                "job": {"job_id": "repo-a:1", "scope": "semantic"},
            }
        }

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)

    raw = _run(
        mcp.tools["omni_index"](
            action="status",
            workspace_id="repo-a",
            format="json",
        )
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["contract_version"] == "index.v1"
    assert payload["action"] == "status"
    assert payload["state"] == "completed"
    assert captured == [
        (
            "GET",
            "/search/index/status",
            {
                "params": {"workspace_id": "repo-a"},
                "headers": {"X-Omnicode-Workspace": "repo-a"},
            },
        )
    ]


def test_omni_index_bootstrap_calls_backend_with_scope() -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        captured.append((method, endpoint, kwargs))
        return {
            "result": {
                "workspace_id": "repo-a",
                "background": True,
                "job": {
                    "job_id": "repo-a:2",
                    "workspace_id": "repo-a",
                    "state": "running",
                    "scope": "semantic",
                },
            }
        }

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)

    raw = _run(
        mcp.tools["omni_index"](
            action="bootstrap",
            scope="semantic",
            workspace_id="repo-a",
            force=True,
            background=True,
            format="json",
        )
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["action"] == "bootstrap"
    assert payload["scope"] == "semantic"
    assert payload["job"]["state"] == "running"
    assert captured == [
        (
            "POST",
            "/search/index",
            {
                "params": {
                    "workspace_id": "repo-a",
                    "force": True,
                    "background": True,
                    "scope": "semantic",
                },
                "headers": {"X-Omnicode-Workspace": "repo-a"},
            },
        )
    ]


def test_omni_index_rejects_invalid_scope() -> None:
    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("backend should not be called")

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)

    raw = _run(
        mcp.tools["omni_index"](
            action="bootstrap",
            scope="bad",
            workspace_id="repo-a",
            format="json",
        )
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["contract_version"] == "index.v1"
    assert "allowed_scopes" in payload


def test_omni_index_lsp_status_is_local_and_does_not_call_backend(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))

    class _Bridge:
        @staticmethod
        def status_snapshot(languages):
            assert languages == {"java", "scala"}
            return {
                "java": {"running": True},
                "scala": {"running": True},
            }

    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: _Bridge(),
    )

    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("backend should not be called")

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    payload = json.loads(_run(mcp.tools["omni_index"](
        action="status",
        scope="lsp",
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["source"] == "local_lsp_runtime"
    assert payload["lsp_ready"] is True
    assert payload["state"] == "ready"


def test_omni_index_lsp_bootstrap_reports_partial_failure(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))

    class _Bridge:
        @staticmethod
        async def bootstrap(languages):
            assert languages == {"java", "scala"}
            return {
                "ready": False,
                "languages": {
                    "java": {"ready": True},
                    "scala": {
                        "ready": False,
                        "error_code": "metals_unavailable",
                    },
                },
            }

    monkeypatch.setattr(
        "omnicode_core.lsp.bridge.get_lsp_bridge",
        lambda _root: _Bridge(),
    )

    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("backend should not be called")

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    payload = json.loads(_run(mcp.tools["omni_index"](
        action="bootstrap",
        scope="lsp",
        background=False,
        format="json",
    )))

    assert payload["ok"] is False
    assert payload["state"] == "degraded"
    assert payload["result"]["languages"]["scala"]["error_code"] == (
        "metals_unavailable"
    )


@pytest.mark.parametrize("action", ["pause", "resume", "retry"])
def test_omni_index_controls_semantic_background_job(action: str) -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def make_request(
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        captured.append((method, endpoint, kwargs))
        return {
            "result": {
                "workspace_id": "repo-a",
                "action": action,
                "state": "paused" if action == "pause" else "running",
                "job": {
                    "job_id": "repo-a:3",
                    "state": "paused" if action == "pause" else "running",
                },
            }
        }

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)

    payload = json.loads(_run(mcp.tools["omni_index"](
        action=action,
        scope="semantic",
        workspace_id="repo-a",
        format="json",
    )))

    assert payload["ok"] is True
    assert payload["action"] == action
    assert payload["backend_action"] == "POST /search/index/control"
    assert captured == [(
        "POST",
        "/search/index/control",
        {
            "params": {
                "workspace_id": "repo-a",
                "action": action,
            },
            "headers": {"X-Omnicode-Workspace": "repo-a"},
        },
    )]
