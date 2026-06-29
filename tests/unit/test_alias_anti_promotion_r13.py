"""Regression: deprecated aliases must never be promoted as primary
recommendations, even after audit-bundle.r13 added new alias-side
features (validate gate, force visibility, ai_edit JSON envelope,
analyze unknown alignment, intelligence resolution stamping).

Pinned by Round 4 + r13:
* Generic edit / safe-edit queries → no omni_edit.
* Generic impact / analyze queries → no omni_analyze.
* Generic understand / context queries → no omni_intelligence.
* Explicit alias-name queries → alias surfaces, but the modern
  replacement is also surfaced and the response carries 'deprecated'.
* Built-in skill recipes never use a deprecated alias.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _recommend_tools,
    register_high_level_tools,
)


_DEPRECATED_ALIASES = ("omni_analyze", "omni_edit", "omni_intelligence")


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_tools() -> Dict[str, Callable[..., Any]]:
    async def make_request(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        return {"success": True, "result": {}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    return mcp.tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. discover_tools — generic queries don't promote aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "edit a file safely",
    "modify code",
    "rewrite this function",
    "patch a bug",
    "safe refactor",
])
def test_discover_tools_generic_edit_query_does_not_promote_omni_edit(query: str) -> None:
    out = _recommend_tools(query)
    body_top = out.split("Default workflow")[0]
    assert "omni_edit" not in body_top, (
        f"query={query!r} must not promote omni_edit; got:\n{body_top}"
    )
    assert "omni_patch" in out


@pytest.mark.parametrize("query", [
    "impact analysis",
    "find callers",
    "blast radius",
    "what depends on this function",
])
def test_discover_tools_generic_impact_query_does_not_promote_omni_analyze(query: str) -> None:
    out = _recommend_tools(query)
    body_top = out.split("Default workflow")[0]
    assert "omni_analyze" not in body_top, (
        f"query={query!r} must not promote omni_analyze; got:\n{body_top}"
    )
    assert "omni_impact" in out


@pytest.mark.parametrize("query", [
    "understand this code",
    "give me context for this symbol",
    "explain what this does",
])
def test_discover_tools_generic_context_query_does_not_promote_omni_intelligence(query: str) -> None:
    out = _recommend_tools(query)
    body_top = out.split("Default workflow")[0]
    assert "omni_intelligence" not in body_top, (
        f"query={query!r} must not promote omni_intelligence; got:\n{body_top}"
    )
    assert "omni_context" in out


def test_discover_tools_does_not_promote_aliases_for_generic_queries() -> None:
    """Aggregate guard: empty query MUST NOT mention any alias as a
    primary recommendation."""
    out = _recommend_tools("")
    for alias in _DEPRECATED_ALIASES:
        assert alias not in out, f"{alias} surfaced for empty query"


# ---------------------------------------------------------------------------
# 2. Explicit alias queries surface the modern replacement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias,replacement", [
    ("omni_edit", "omni_patch"),
    ("omni_analyze", "omni_impact"),
    ("omni_intelligence", "omni_context"),
])
def test_explicit_alias_query_surfaces_modern_replacement(
    alias: str, replacement: str,
) -> None:
    out = _recommend_tools(alias)
    assert alias in out
    assert replacement in out
    assert "deprecated" in out.lower()


# ---------------------------------------------------------------------------
# 3. omni_skill recipes don't use a deprecated alias
# ---------------------------------------------------------------------------


def test_omni_skill_recipes_do_not_use_aliases() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_skill"](action="list", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    for skill in payload["skills"]:
        for tool in skill.get("tools_used", []):
            assert tool not in _DEPRECATED_ALIASES, (
                f"skill {skill['name']} lists deprecated alias {tool}"
            )
        for step in skill.get("steps", []):
            assert step.get("tool") not in _DEPRECATED_ALIASES, (
                f"skill {skill['name']} step references deprecated "
                f"alias {step.get('tool')}"
            )


# ---------------------------------------------------------------------------
# 4. r13 new feature flags don't break the anti-promotion guarantee
# ---------------------------------------------------------------------------


def test_r13_alias_features_dont_change_default_recommendation() -> None:
    """The new r13 alias features (alias.edit_validate_gate, etc.) are
    quality-of-life upgrades for callers who already use the alias.
    They MUST NOT cause the alias to surface in default recommendations."""
    out = _recommend_tools("")
    # Default tools should mention modern primaries
    assert "omni_patch" in out
    assert "omni_impact" in out
    assert "omni_context" in out
    # ... and never the aliases
    for alias in _DEPRECATED_ALIASES:
        assert alias not in out
