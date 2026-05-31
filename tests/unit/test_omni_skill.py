"""Unit tests for the omni_skill MCP tool and its skill registry.

These cover the upgraded ``v2`` schema (when_to_use / tools_used /
does_execute / safety_notes / steps[].id/title/required/condition/
purpose), the ``format='json'`` contract, the keyword-based search,
and the safety guarantees (no auto-execution, no deprecated aliases).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

import pytest

from omnicode_adapters.mcp_server.high_level_tools import register_high_level_tools


# ---------------------------------------------------------------------------
# Helpers — capture the registered ``omni_skill`` callable.
# ---------------------------------------------------------------------------


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


async def _noop_make_request(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {"success": True, "result": {}}


@pytest.fixture(scope="module")
def skill_tool() -> Callable[..., str]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    fn = mcp.tools.get("omni_skill")
    assert fn is not None, "omni_skill was not registered"
    return fn


def _run_json(fn: Callable[..., Any], **kwargs: Any) -> Dict[str, Any]:
    """Call the tool with format='json' and parse the response."""
    out = asyncio.run(fn(format="json", **kwargs))
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. list — JSON contract.
# ---------------------------------------------------------------------------


def test_skill_list_json(skill_tool) -> None:
    payload = _run_json(skill_tool, action="list")
    assert payload["ok"] is True
    assert payload["action"] == "list"
    assert payload["count"] >= 3
    names = {s["name"] for s in payload["skills"]}
    for required in (
        "omni-impact-review", "omni-safe-refactor", "omni-test-coverage",
    ):
        assert required in names, f"missing builtin skill: {required}"


# ---------------------------------------------------------------------------
# 2. show — JSON contract for omni-safe-refactor.
# ---------------------------------------------------------------------------


def test_skill_show_json(skill_tool) -> None:
    payload = _run_json(skill_tool, action="show", name="omni-safe-refactor")
    assert payload["ok"] is True
    assert payload["action"] == "show"
    assert payload["name"] == "omni-safe-refactor"
    skill = payload["skill"]
    assert skill["name"] == "omni-safe-refactor"
    assert skill["does_execute"] is False
    # Step shape — every step must carry the new fields.
    for step in skill["steps"]:
        assert "id" in step, step
        assert "title" in step, step
        assert "tool" in step, step
        assert "args" in step, step
        assert "purpose" in step, step
        # required + condition may be absent for some, but should be
        # present on every step in the safe-refactor recipe.
        assert "required" in step, step
        assert "condition" in step, step


# ---------------------------------------------------------------------------
# 3. search — English query "safe refactor" must hit omni-safe-refactor.
# ---------------------------------------------------------------------------


def test_skill_search_safe_refactor(skill_tool) -> None:
    payload = _run_json(skill_tool, action="search", query="safe refactor")
    assert payload["ok"] is True
    assert payload["action"] == "search"
    names = [r["skill"]["name"] for r in payload["results"]]
    assert "omni-safe-refactor" in names, (
        f"safe refactor query missed the skill, got: {names}"
    )
    # Top result should be omni-safe-refactor (ranked first).
    assert names[0] == "omni-safe-refactor", (
        f"omni-safe-refactor should rank first, got: {names[0]}"
    )
    # Each result should carry score + why_matched.
    for r in payload["results"]:
        assert "score" in r
        assert "why_matched" in r
        assert isinstance(r["why_matched"], list)


# ---------------------------------------------------------------------------
# 4. search — Chinese query "安全重构" must also hit omni-safe-refactor.
# ---------------------------------------------------------------------------


def test_skill_search_chinese_safe_refactor(skill_tool) -> None:
    payload = _run_json(skill_tool, action="search", query="安全重构")
    assert payload["ok"] is True
    names = [r["skill"]["name"] for r in payload["results"]]
    assert "omni-safe-refactor" in names, (
        f"ZH 安全重构 query missed the skill, got: {names}"
    )


# ---------------------------------------------------------------------------
# 5. omni-safe-refactor — must contain context / impact / read steps.
# ---------------------------------------------------------------------------


def test_safe_refactor_contains_context_impact_read(skill_tool) -> None:
    payload = _run_json(skill_tool, action="show", name="omni-safe-refactor")
    tools = [step["tool"] for step in payload["skill"]["steps"]]
    for required_tool in ("omni_context", "omni_impact", "omni_read"):
        assert required_tool in tools, (
            f"omni-safe-refactor missing required step tool: {required_tool} "
            f"(got: {tools})"
        )


# ---------------------------------------------------------------------------
# 6. omni-safe-refactor — must contain preview / validate / apply / rollback,
#    AND rollback must be its own independent step.
# ---------------------------------------------------------------------------


def test_safe_refactor_contains_preview_validate_apply_rollback(skill_tool) -> None:
    payload = _run_json(skill_tool, action="show", name="omni-safe-refactor")
    steps = payload["skill"]["steps"]

    # Each of the four actions must appear as a distinct step's args.action.
    actions_seen: list = []
    rollback_step_ids: list = []
    for step in steps:
        if step.get("tool") == "omni_patch":
            act = step.get("args", {}).get("action")
            if act:
                actions_seen.append(act)
            if act == "rollback":
                rollback_step_ids.append(step.get("id"))

    for required in ("preview", "validate", "apply", "rollback"):
        assert required in actions_seen, (
            f"omni-safe-refactor missing omni_patch action: {required}; "
            f"saw: {actions_seen}"
        )
    # rollback should be a single, independent step (not just a comment).
    assert len(rollback_step_ids) >= 1, (
        "rollback must exist as its own omni_patch step"
    )


# ---------------------------------------------------------------------------
# 7. Every skill must carry when_to_use + tools_used.
# ---------------------------------------------------------------------------


def test_skill_has_when_to_use_and_tools_used(skill_tool) -> None:
    payload = _run_json(skill_tool, action="list")
    for skill in payload["skills"]:
        assert skill.get("when_to_use"), (
            f"skill {skill['name']} missing when_to_use"
        )
        assert isinstance(skill.get("tools_used"), list)
        assert skill["tools_used"], (
            f"skill {skill['name']} has empty tools_used"
        )
        assert "does_execute" in skill
        assert "safety_notes" in skill


# ---------------------------------------------------------------------------
# 8. omni_skill never marks any skill as auto-executing.
# ---------------------------------------------------------------------------


def test_skill_does_not_execute(skill_tool) -> None:
    payload = _run_json(skill_tool, action="list")
    for skill in payload["skills"]:
        assert skill["does_execute"] is False, (
            f"skill {skill['name']} declares does_execute=True — "
            "omni_skill must never auto-run recipes"
        )


# ---------------------------------------------------------------------------
# 9. No skill should reference a deprecated alias (omni_analyze /
#    omni_edit / omni_intelligence) in its steps or tools_used.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["omni_analyze", "omni_edit", "omni_intelligence"])
def test_skill_does_not_use_deprecated_aliases(skill_tool, alias: str) -> None:
    payload = _run_json(skill_tool, action="list")
    for skill in payload["skills"]:
        assert alias not in skill["tools_used"], (
            f"skill {skill['name']} lists deprecated alias {alias} "
            "in tools_used"
        )
        for step in skill["steps"]:
            assert step["tool"] != alias, (
                f"skill {skill['name']} step references deprecated "
                f"alias {alias}"
            )


# ---------------------------------------------------------------------------
# 10. Invalid action must return a structured error with allowed_actions.
# ---------------------------------------------------------------------------


def test_skill_invalid_action_returns_structured_error(skill_tool) -> None:
    payload = _run_json(skill_tool, action="frobnicate")
    assert payload["ok"] is False
    assert "error" in payload
    assert payload["action"] == "frobnicate"
    assert "allowed_actions" in payload
    for required in ("list", "search", "show"):
        assert required in payload["allowed_actions"]


# ---------------------------------------------------------------------------
# Bonus — search fallback must NOT return a dead-end on zero match.
# ---------------------------------------------------------------------------


def test_skill_search_zero_match_falls_back(skill_tool) -> None:
    payload = _run_json(skill_tool, action="search", query="xyzzy_no_such_skill")
    assert payload["ok"] is True
    # Loader fallback returns the full default list with score=0.
    assert payload["count"] >= 1
    # All scores 0 with why_matched=['fallback:default'].
    for r in payload["results"]:
        assert r["score"] == 0
        assert "fallback:default" in r["why_matched"]


# ---------------------------------------------------------------------------
# Bonus — text format still works (regression).
# ---------------------------------------------------------------------------


def test_skill_text_format_still_works(skill_tool) -> None:
    out = asyncio.run(skill_tool(action="list", format="text"))
    assert "omni-safe-refactor" in out
    assert "omni-impact-review" in out
    assert "omni-test-coverage" in out
