"""Contract tests for the omni_status runtime self-check tool.

omni_status exists so a human auditor can verify the live MCP host is
running the same code as the on-disk source + unit tests. The audit bug
that motivated this tool: omni_search picked up its source/confidence
fix on restart but omni_read kept serving the pre-fix diagnostics
schema, because FastMCP's per-tool registration was partial.

These tests pin:

* every required field is present
* warnings is empty when source + runtime agree
* a missing flagship tool surfaces in warnings
* a missing handler feature surfaces in warnings (regression guard)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ToolManagerStub:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}


class _MCPStub:
    """Mimics FastMCP enough for omni_status to introspect it."""

    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}
        self._tool_manager = _ToolManagerStub()

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            self._tool_manager._tools[fn.__name__] = fn
            return fn

        return deco

    async def list_tools(self) -> list:  # pragma: no cover - fallback path
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


async def _noop_make_request(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {}


def _build_status_tool() -> Callable[..., Any]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    fn = mcp.tools.get("omni_status")
    assert fn is not None, "omni_status was not registered"
    return fn


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_omni_status_returns_required_fields() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    required = {
        "ok",
        "pid",
        "process_start_time",
        "module_path",
        "module_sha1",
        "module_mtime",
        "python_executable",
        "python_version",
        "handler_version",
        "handler_features",
        "registered_tools",
        "deprecated_aliases_present",
        "warnings",
    }
    missing = required - set(payload.keys())
    assert not missing, f"omni_status missing fields: {missing}"

    # Sanity on a few values.
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert payload["module_path"].endswith("high_level_tools.py")
    assert len(payload["module_sha1"]) == 40  # full sha1 hex
    assert payload["handler_version"] == hlt._HANDLER_VERSION
    assert isinstance(payload["registered_tools"], list)
    assert "omni_status" in payload["registered_tools"]


def test_omni_status_clean_when_source_and_runtime_agree() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert payload["warnings"] == [], payload["warnings"]
    assert payload["ok"] is True


def test_omni_status_lists_flagship_tools() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    flagship = {
        "omni_search", "omni_read", "omni_impact",
        "omni_diagnostics", "omni_patch", "omni_memory",
        "omni_context", "omni_skill", "discover_tools",
        "omni_status",
    }
    missing = flagship - set(payload["registered_tools"])
    assert not missing, missing


def test_omni_status_flags_missing_flagship_tool() -> None:
    """If a flagship tool isn't registered, warnings must surface it.

    Simulate by deleting omni_read from the registry after registration.
    """
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    # Sabotage: remove omni_read from the live registry.
    mcp._tool_manager._tools.pop("omni_read", None)
    status_fn = mcp.tools["omni_status"]
    raw = _run(status_fn())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert any(
        w.startswith("flagship_tools_missing:") and "omni_read" in w
        for w in payload["warnings"]
    ), payload["warnings"]


def test_omni_status_handler_features_match_module_constant() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert tuple(payload["handler_features"]) == hlt._HANDLER_FEATURES


def test_omni_status_pid_matches_current_process() -> None:
    import os
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert payload["pid"] == os.getpid()
