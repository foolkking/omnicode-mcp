"""Shared FastMCP test harness for high-level MCP tool contract tests."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List

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


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def build_tools_from_request(
    make_request: Callable[..., Any],
) -> Dict[str, Callable[..., Any]]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    return dict(mcp.tools)


def build_tools(
    routes: Dict[str, Any],
    *,
    route_key_fallback: bool = False,
    intelligence_context_success: bool = False,
) -> Dict[str, Any]:
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        payload = routes.get(endpoint)
        if payload is None and route_key_fallback:
            key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
            payload = routes.get(key)
        if payload is None:
            return {"result": {}}
        if callable(payload):
            payload = payload(method, endpoint, kwargs)
        if intelligence_context_success and endpoint == "/intelligence/context":
            return {"success": True, "result": payload}
        return {"result": payload}

    tools: Dict[str, Any] = build_tools_from_request(make_request)
    tools["__captured__"] = captured
    return tools


def build_tools_with_route_keys(routes: Dict[str, Any]) -> Dict[str, Any]:
    return build_tools(routes, route_key_fallback=True)


def build_tools_with_intelligence_context(routes: Dict[str, Any]) -> Dict[str, Any]:
    return build_tools(routes, intelligence_context_success=True)
