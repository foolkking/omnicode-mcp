"""Regression tests for MCP local-to-cloud backend bridging."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace


def test_configure_backend_sets_url_and_auth_header(monkeypatch):
    import mcp_server

    old_base_url = mcp_server.FASTAPI_BASE_URL
    old_client = mcp_server.http_client
    monkeypatch.delenv("OMNICODE_FASTAPI_BASE_URL", raising=False)
    monkeypatch.delenv("OMNICODE_FASTAPI_TOKEN", raising=False)
    monkeypatch.delenv("OMNICODE_BACKEND_TOKEN", raising=False)
    monkeypatch.delenv("OMNICODE_API_KEY", raising=False)
    monkeypatch.delenv("OMNICODE_AGENT_TOKEN", raising=False)

    try:
        mcp_server.configure_backend("https://omnicode.example.com/", "tok_cloud")

        assert mcp_server.FASTAPI_BASE_URL == "https://omnicode.example.com"
        assert mcp_server._backend_headers()["X-API-Key"] == "tok_cloud"
        assert mcp_server.http_client is None
    finally:
        mcp_server.FASTAPI_BASE_URL = old_base_url
        mcp_server.http_client = old_client


def test_mcp_server_main_configures_stdio_cloud_bridge(monkeypatch):
    import mcp_server

    old_base_url = mcp_server.FASTAPI_BASE_URL
    old_client = mcp_server.http_client
    called = {}

    class FakeMcp:
        settings = SimpleNamespace(host="127.0.0.1", port=6790)

        def run(self, *, transport: str) -> None:
            called["transport"] = transport

    monkeypatch.setattr(mcp_server, "mcp", FakeMcp())
    monkeypatch.delenv("OMNICODE_FASTAPI_TOKEN", raising=False)

    try:
        mcp_server.main(
            [
                "--backend-url",
                "https://omnicode.example.com",
                "--backend-token",
                "tok_remote",
            ]
        )

        assert called == {"transport": "stdio"}
        assert mcp_server.FASTAPI_BASE_URL == "https://omnicode.example.com"
        assert mcp_server._backend_headers()["X-API-Key"] == "tok_remote"
    finally:
        mcp_server.FASTAPI_BASE_URL = old_base_url
        mcp_server.http_client = old_client


def test_configure_workspace_adds_backend_workspace_headers(monkeypatch, tmp_path):
    import mcp_server

    old_workspace_id = mcp_server.BACKEND_WORKSPACE_ID
    old_root = mcp_server.LOCAL_WORKSPACE_ROOT
    old_executor = mcp_server.EXECUTOR_MODE
    old_client = mcp_server.http_client
    monkeypatch.delenv("OMNICODE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("OMNICODE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OMNICODE_EXECUTOR_MODE", raising=False)

    try:
        mcp_server.configure_workspace(
            workspace=str(tmp_path),
            workspace_id="repo-a",
            executor="hybrid",
        )
        headers = mcp_server._backend_headers()
        assert headers["X-Omnicode-Workspace"] == "repo-a"
        assert headers["X-Omnicode-Executor"] == "hybrid"
        assert mcp_server.LOCAL_WORKSPACE_ROOT == str(tmp_path.resolve())
    finally:
        mcp_server.BACKEND_WORKSPACE_ID = old_workspace_id
        mcp_server.LOCAL_WORKSPACE_ROOT = old_root
        mcp_server.EXECUTOR_MODE = old_executor
        mcp_server.http_client = old_client


def test_mcp_cmd_forwards_transport_and_backend_args(monkeypatch):
    from omnicode_adapters.cli.commands import mcp_cmd

    captured = []
    fake_module = types.SimpleNamespace(main=lambda argv: captured.append(argv))
    monkeypatch.setitem(sys.modules, "mcp_server", fake_module)

    mcp_cmd.run(
        transport="sse",
        host="0.0.0.0",
        port=6790,
        mount_path="/mcp",
        auth="required",
        backend_url="https://omnicode.example.com",
        backend_token="tok_remote",
        workspace_id="repo-a",
        executor="hybrid",
    )

    assert captured == [
        [
            "--transport",
            "sse",
            "--auth",
            "required",
            "--host",
            "0.0.0.0",
            "--port",
            "6790",
            "--mount-path",
            "/mcp",
            "--backend-url",
            "https://omnicode.example.com",
            "--backend-token",
            "tok_remote",
            "--workspace-id",
            "repo-a",
            "--executor",
            "hybrid",
        ]
    ]


def test_cli_mcp_parser_dispatches_cloud_bridge_args(monkeypatch):
    from omnicode_adapters.cli import main as cli_main

    captured = {}
    fake_module = types.ModuleType("omnicode_adapters.cli.commands.mcp_cmd")
    fake_module.run = lambda **kwargs: captured.update(kwargs)
    monkeypatch.setitem(sys.modules, "omnicode_adapters.cli.commands.mcp_cmd", fake_module)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "omnicode",
            "mcp",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "6791",
            "--mount-path",
            "/mcp",
            "--auth",
            "auto",
            "--backend-url",
            "https://omnicode.example.com",
            "--backend-token",
            "tok_remote",
            "--workspace-id",
            "repo-a",
            "--executor",
            "hybrid",
        ],
    )

    cli_main.main()

    assert captured == {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 6791,
        "mount_path": "/mcp",
        "auth": "auto",
        "backend_url": "https://omnicode.example.com",
        "backend_token": "tok_remote",
        "workspace": None,
        "workspace_id": "repo-a",
        "executor": "hybrid",
    }
