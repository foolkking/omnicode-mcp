"""Contract tests for discover_tools JSON stamping + audit rule.

Pinned by the audit-bundle update:

* discover_tools(format="json") returns handler_version + contract_version
* discover_tools' contract_version is exactly "discover.v1"
* omni_status.expected_contract_versions includes discover_tools
* every tool in _CONTRACT_VERSIONS must also be listed in
  _TOOLS_WITH_JSON_STAMP, otherwise omni_status.warnings flags it as
  ``json_stamp_unsupported:<tool>``

The last guard is the audit rule: any flagship tool that ships a
contract_version MUST also be reachable via format="json", otherwise the
auditor cannot validate the live binding for that tool.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    _TOOLS_WITH_JSON_STAMP,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim
# ---------------------------------------------------------------------------


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

    async def list_tools(self) -> List[Any]:  # pragma: no cover
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


async def _noop_make_request(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {}


def _build_tools() -> Dict[str, Callable[..., Any]]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    return mcp.tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. discover_tools(format="json") returns handler_version
# ---------------------------------------------------------------------------


def test_discover_tools_json_includes_handler_version() -> None:
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query="", format="json"))
    payload = json.loads(raw)
    assert payload.get("handler_version") == _HANDLER_VERSION


def test_discover_tools_json_with_query_includes_handler_version() -> None:
    """Stamping must work in both ranked and default branches."""
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query="find references", format="json"))
    payload = json.loads(raw)
    assert payload.get("handler_version") == _HANDLER_VERSION


# ---------------------------------------------------------------------------
# 2. discover_tools contract_version is exactly "discover.v1"
# ---------------------------------------------------------------------------


def test_discover_tools_json_contract_version_is_discover_v1() -> None:
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query="", format="json"))
    payload = json.loads(raw)
    assert payload.get("contract_version") == "discover.v1"


def test_discover_tools_contract_version_in_registry() -> None:
    assert _CONTRACT_VERSIONS["discover_tools"] == "discover.v1"


# ---------------------------------------------------------------------------
# 3. discover_tools JSON has the structured fields a caller actually needs
# ---------------------------------------------------------------------------


def test_discover_tools_json_default_listing_shape() -> None:
    """No query → mode='default' with default_tools + default_pipeline."""
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query="", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["mode"] == "default"
    assert payload["default_tools"], "default tool list must not be empty"
    assert payload["default_pipeline"], "default pipeline must not be empty"
    # Every default tool entry has name + desc.
    for entry in payload["default_tools"]:
        assert entry.get("name") and entry.get("desc")


def test_discover_tools_json_ranked_listing_shape() -> None:
    """A meaningful query → mode='ranked' with results + why_matched."""
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query="find references", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["mode"] == "ranked"
    assert payload["results"], "ranked results must not be empty"
    top = payload["results"][0]
    assert "score" in top
    assert "why_matched" in top
    assert isinstance(top["why_matched"], list)


def test_discover_tools_json_zero_match_falls_back_to_default() -> None:
    """A query with no matches → mode='no_match', still returns the listing."""
    tools = _build_tools()
    raw = _run(tools["discover_tools"](
        query="zzz_unrelated_to_everything_xyz", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["mode"] == "no_match"
    assert payload["default_tools"]
    assert payload["default_pipeline"]


def test_discover_tools_text_format_unchanged() -> None:
    """Default format='text' still returns the human-readable listing."""
    tools = _build_tools()
    raw = _run(tools["discover_tools"](query=""))
    assert isinstance(raw, str)
    assert "OmniCode tools" in raw or "📦" in raw
    # And the text variant is NOT JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# ---------------------------------------------------------------------------
# 4. omni_status.expected_contract_versions includes discover_tools
# ---------------------------------------------------------------------------


def test_omni_status_expected_contract_versions_includes_discover_tools() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    expected = payload.get("expected_contract_versions", {})
    assert expected.get("discover_tools") == "discover.v1"


def test_omni_status_lists_discover_tools_in_json_stamp() -> None:
    """The audit rule's allowlist must include discover_tools."""
    tools = _build_tools()
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    assert "discover_tools" in payload.get("tools_with_json_stamp", [])


# ---------------------------------------------------------------------------
# 5. Audit rule: contract registry ⊆ JSON-stamp registry; mismatch warns.
# ---------------------------------------------------------------------------


def test_every_contract_tool_supports_json_stamp() -> None:
    """Static guard — no flagship tool may carry a contract_version
    without also being in _TOOLS_WITH_JSON_STAMP."""
    contract_set = set(_CONTRACT_VERSIONS)
    json_stamp_set = set(_TOOLS_WITH_JSON_STAMP)
    missing = contract_set - json_stamp_set
    assert not missing, (
        f"tools with contract_version but no JSON stamp support: {missing}"
    )


def test_omni_status_warns_when_json_stamp_missing(monkeypatch) -> None:
    """If a future tool slips into _CONTRACT_VERSIONS without
    json-stamp support, omni_status must surface it in warnings."""
    # Inject a fake tool only into the contract registry, not the
    # json-stamp allowlist.
    fake_contracts = dict(hlt._CONTRACT_VERSIONS)
    fake_contracts["future_text_only_tool"] = "future.v1"
    monkeypatch.setattr(hlt, "_CONTRACT_VERSIONS", fake_contracts)

    tools = _build_tools()
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert any(
        w == "json_stamp_unsupported:future_text_only_tool"
        for w in payload["warnings"]
    ), payload["warnings"]


def test_omni_status_no_json_stamp_warning_in_clean_state() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    bad = [
        w for w in payload.get("warnings", [])
        if w.startswith("json_stamp_unsupported:")
    ]
    assert not bad, bad
