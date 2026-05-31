"""Contract tests for the unified deprecated-alias JSON envelope (audit-bundle.r9, P2).

The three deprecated aliases — omni_analyze / omni_edit / omni_intelligence —
must, under format='json', return a common envelope:

    {
      "deprecated": true,
      "alias": "<alias_name>",
      "replacement": "<modern_tool>",
      "use_instead": "<example>",
      "handler_version": "...audit-bundle.r9",
      "contract_version": "alias.compat.v1"
    }

Mapping:
  omni_analyze      -> omni_impact
  omni_edit         -> omni_patch
  omni_intelligence -> omni_context

The alias contract must NOT leak into the core expected_contract_versions
audit surface (tools_with_json_stamp / _CONTRACT_VERSIONS).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _ALIAS_COMPAT_CONTRACT,
    _ALIAS_REPLACEMENTS,
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _TOOLS_WITH_JSON_STAMP,
    _alias_envelope,
    register_high_level_tools,
)


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_tools(route_payload: Dict[str, Any]) -> Dict[str, Any]:
    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        for key, val in route_payload.items():
            if key in endpoint:
                return val
        return {"result": {}, "success": True}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    return mcp.tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Pure helper: _alias_envelope stamps the common fields.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias,replacement",
    [
        ("omni_analyze", "omni_impact"),
        ("omni_edit", "omni_patch"),
        ("omni_intelligence", "omni_context"),
    ],
)
def test_alias_envelope_helper_sets_common_fields(alias: str, replacement: str) -> None:
    env = _alias_envelope(alias, {"ok": True})
    assert env["deprecated"] is True
    assert env["alias"] == alias
    assert env["replacement"] == replacement
    assert env["use_instead"]
    assert env["handler_version"] == _HANDLER_VERSION
    assert env["contract_version"] == _ALIAS_COMPAT_CONTRACT


def test_alias_replacement_mapping() -> None:
    assert _ALIAS_REPLACEMENTS == {
        "omni_analyze": "omni_impact",
        "omni_edit": "omni_patch",
        "omni_intelligence": "omni_context",
    }


# ---------------------------------------------------------------------------
# Alias contract must stay OUT of the core audit surface.
# ---------------------------------------------------------------------------


def test_alias_contract_not_in_core_contract_versions() -> None:
    for alias in ("omni_analyze", "omni_edit", "omni_intelligence"):
        assert alias not in _CONTRACT_VERSIONS
    assert _ALIAS_COMPAT_CONTRACT not in _CONTRACT_VERSIONS.values()


def test_alias_not_in_tools_with_json_stamp() -> None:
    for alias in ("omni_analyze", "omni_edit", "omni_intelligence"):
        assert alias not in _TOOLS_WITH_JSON_STAMP


# ---------------------------------------------------------------------------
# omni_analyze JSON envelope.
# ---------------------------------------------------------------------------


def test_omni_analyze_json_envelope() -> None:
    tools = _build_tools({
        "/search/symbols/relations": {
            "result": {
                "callers": {"count": 2, "names": ["a", "b"]},
                "callees": {"count": 1, "names": ["c"]},
                "total_edges": 3,
            }
        }
    })
    raw = _run(tools["omni_analyze"](symbol="foo", analysis="impact", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_analyze"
    assert payload["replacement"] == "omni_impact"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT
    assert payload["handler_version"] == _HANDLER_VERSION


def test_omni_analyze_text_still_human_readable() -> None:
    tools = _build_tools({
        "/search/symbols/relations": {
            "result": {"callers": {"count": 0}, "callees": {"count": 0}}
        }
    })
    raw = _run(tools["omni_analyze"](symbol="foo", analysis="impact", format="text"))
    assert "Impact analysis" in raw
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# ---------------------------------------------------------------------------
# omni_intelligence JSON envelope.
# ---------------------------------------------------------------------------


def test_omni_intelligence_json_envelope() -> None:
    tools = _build_tools({
        "/intelligence/context": {
            "success": True,
            "result": {
                "token_estimate": 100,
                "advisories": [],
                "impact": {},
            },
        }
    })
    raw = _run(tools["omni_intelligence"](symbol="foo", task="understand", token_budget=2000))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_intelligence"
    assert payload["replacement"] == "omni_context"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT


def test_omni_intelligence_failure_envelope() -> None:
    tools = _build_tools({
        "/intelligence/context": {"success": False, "error": "boom"}
    })
    raw = _run(tools["omni_intelligence"](symbol="foo"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_intelligence"
    assert "boom" in payload["error"]


# ---------------------------------------------------------------------------
# No alias returns a traceback / raw exception string.
# ---------------------------------------------------------------------------


def test_aliases_do_not_leak_traceback_on_error() -> None:
    # make_request raises → alias must still produce a clean JSON envelope.
    async def boom(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("backend exploded")

    mcp = _MCPStub()
    register_high_level_tools(mcp, boom)
    tools = mcp.tools

    raw = _run(tools["omni_intelligence"](symbol="foo"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["deprecated"] is True
    assert "Traceback" not in payload["error"]
