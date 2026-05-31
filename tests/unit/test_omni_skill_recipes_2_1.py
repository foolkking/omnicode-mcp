"""Contract tests for omni_skill builtin recipes 2.1.0 (audit-bundle.r8).

Pinned by the audit:

* omni-safe-refactor includes a memory_advisory step
* omni-safe-refactor mentions patch.v2's path guard, validate gate, and
  force=True risk
* omni-impact-review includes an omni_search(mode='references') step
* omni-test-coverage has a no-test fallback (suggest_new_test)
* every show response includes top-level next_actions
* every error path includes next_actions + allowed_actions
* every recipe declares failure_policy on every step
* every recipe declares success_criteria
* no recipe references a deprecated alias (omni_analyze / omni_edit /
  omni_intelligence)
* every recipe lists recipe_for_handler_features
* contract_version stays skill.v2 (manifest update only)
* recipe versions are 2.1.0 across the board
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    register_high_level_tools,
)
from omnicode_core.skills import get_skill_loader


# ---------------------------------------------------------------------------
# FastMCP shim — needed for the show / error tests that go through the
# registered tool function.
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


def _build_skill_tool() -> Callable[..., Any]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    fn = mcp.tools.get("omni_skill")
    assert fn is not None, "omni_skill was not registered"
    return fn


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers — load recipes directly from disk so tests don't depend on the
# loader's caching behaviour.
# ---------------------------------------------------------------------------


_BUILTIN_DIR = (
    Path(__file__).parent.parent.parent
    / "omnicode_core" / "skills" / "builtin"
)

_DEPRECATED_ALIASES = ("omni_analyze", "omni_edit", "omni_intelligence")


def _load_recipe(name: str) -> Dict[str, Any]:
    path = _BUILTIN_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def safe_refactor() -> Dict[str, Any]:
    return _load_recipe("omni-safe-refactor")


@pytest.fixture(scope="module")
def impact_review() -> Dict[str, Any]:
    return _load_recipe("omni-impact-review")


@pytest.fixture(scope="module")
def test_coverage() -> Dict[str, Any]:
    return _load_recipe("omni-test-coverage")


@pytest.fixture(scope="module")
def all_recipes(safe_refactor, impact_review, test_coverage) -> List[Dict[str, Any]]:
    return [safe_refactor, impact_review, test_coverage]


# ---------------------------------------------------------------------------
# 1. omni-safe-refactor includes a memory_advisory step
# ---------------------------------------------------------------------------


def test_skill_safe_refactor_includes_memory_advisory(safe_refactor) -> None:
    step_ids = [s["id"] for s in safe_refactor["steps"]]
    assert "memory_advisory" in step_ids, step_ids
    mem_step = next(s for s in safe_refactor["steps"] if s["id"] == "memory_advisory")
    assert mem_step["tool"] == "omni_memory"
    assert mem_step["args"].get("action") == "advisory"
    # Must consume the canonical memory.v2 fields.
    fields = mem_step.get("consume_fields", [])
    assert "advisory.action_items" in fields
    assert "referenced_memories" in fields
    assert "memory_count" in fields
    # And it must show up in tools_used.
    assert "omni_memory" in safe_refactor["tools_used"]


# ---------------------------------------------------------------------------
# 2. patch.v2 path guard / validate gate / force=True notes
# ---------------------------------------------------------------------------


def test_skill_safe_refactor_mentions_patch_v2_path_guard(safe_refactor) -> None:
    blob = json.dumps(safe_refactor, ensure_ascii=False).lower()
    assert "path guard" in blob or "path-guard" in blob, "path guard not mentioned"
    assert "absolute path" in blob, "absolute path policy not mentioned"
    assert ".." in blob, "'..' traversal not mentioned"


def test_skill_safe_refactor_mentions_apply_validate_gate(safe_refactor) -> None:
    blob = json.dumps(safe_refactor, ensure_ascii=False).lower()
    assert "validate gate" in blob or "apply_validate_gate" in blob, (
        "apply's internal validate gate is not surfaced"
    )
    # And the apply step itself must reference it.
    apply_step = next(s for s in safe_refactor["steps"] if s["id"] == "apply")
    purpose = apply_step["purpose"].lower()
    assert "validate" in purpose


def test_skill_safe_refactor_warns_about_force_true(safe_refactor) -> None:
    blob = json.dumps(safe_refactor, ensure_ascii=False).lower()
    assert "force=true" in blob or "force = true" in blob, (
        "force=True escape hatch not documented"
    )
    assert "force_reason" in blob, "force_reason requirement not documented"
    # And the apply step's purpose must say "DO NOT" or "must only" about force.
    apply_step = next(s for s in safe_refactor["steps"] if s["id"] == "apply")
    purpose = apply_step["purpose"].lower()
    assert "do not" in purpose or "must only" in purpose or "user has explicitly approved" in purpose


def test_skill_safe_refactor_warns_about_unsafe_legacy_session(safe_refactor) -> None:
    blob = json.dumps(safe_refactor, ensure_ascii=False).lower()
    assert "unsafe_legacy_session" in blob


# ---------------------------------------------------------------------------
# 3. omni-impact-review includes an omni_search(mode='references') step
# ---------------------------------------------------------------------------


def test_skill_impact_review_includes_references_step(impact_review) -> None:
    step_ids = [s["id"] for s in impact_review["steps"]]
    assert "references" in step_ids, step_ids
    ref_step = next(s for s in impact_review["steps"] if s["id"] == "references")
    assert ref_step["tool"] == "omni_search"
    assert ref_step["args"]["mode"] == "references"
    assert "omni_search" in impact_review["tools_used"]


def test_skill_impact_review_explains_suggested_tests_use(impact_review) -> None:
    """suggested_tests / suggested_commands consumption must be documented."""
    impact_step = next(s for s in impact_review["steps"] if s["id"] == "impact")
    assert "suggested_tests" in impact_step.get("consume_fields", [])
    assert "suggested_commands" in impact_step.get("consume_fields", [])
    # And there's post_step_guidance telling AI when to run them.
    guidance = impact_step.get("post_step_guidance") or []
    joined = " ".join(guidance).lower()
    assert "suggested" in joined and ("targeted" in joined or "pytest" in joined)


# ---------------------------------------------------------------------------
# 4. omni-test-coverage has a no-test fallback
# ---------------------------------------------------------------------------


def test_skill_test_coverage_has_no_test_fallback(test_coverage) -> None:
    step_ids = [s["id"] for s in test_coverage["steps"]]
    assert "suggest_new_test" in step_ids, step_ids
    fb = next(s for s in test_coverage["steps"] if s["id"] == "suggest_new_test")
    cond = (fb.get("condition") or "").lower()
    assert "empty" in cond and "search" in cond, cond
    produces = " ".join(fb.get("produces") or []).lower()
    assert "coverage_gap" in produces


def test_skill_test_coverage_does_not_default_to_full_pytest(test_coverage) -> None:
    """Full pytest sweep should NOT be the only / first recommendation."""
    blob = json.dumps(test_coverage, ensure_ascii=False).lower()
    # success_criteria / safety_notes must explicitly de-prioritise the
    # full sweep.
    sc = " ".join(test_coverage.get("success_criteria") or []).lower()
    sn = " ".join(test_coverage.get("safety_notes") or []).lower()
    combined = sc + " " + sn
    assert "targeted" in combined, combined
    assert "last resort" in combined or "only fall back" in combined or "does not fall back" in combined


# ---------------------------------------------------------------------------
# 5. show responses include next_actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "omni-safe-refactor",
    "omni-impact-review",
    "omni-test-coverage",
])
def test_skill_show_responses_include_next_actions(name: str) -> None:
    """Live test through the omni_skill tool — show response must
    surface the recipe's next_actions field."""
    fn = _build_skill_tool()
    raw = _run(fn(action="show", name=name, format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    skill = payload["skill"]
    actions = skill.get("next_actions")
    assert isinstance(actions, list) and actions, (name, skill.keys())
    # And the contract fields ride along.
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_skill"]


# ---------------------------------------------------------------------------
# 6. error paths include next_actions
# ---------------------------------------------------------------------------


def test_skill_error_paths_include_next_actions() -> None:
    fn = _build_skill_tool()
    # Illegal action.
    bad_action = json.loads(_run(fn(action="illegal_action", format="json")))
    assert bad_action["ok"] is False
    assert bad_action.get("allowed_actions") == ["list", "search", "show"]
    assert bad_action.get("next_actions"), bad_action

    # Unknown skill name.
    not_found = json.loads(_run(fn(action="show", name="not-exist-skill", format="json")))
    assert not_found["ok"] is False
    assert not_found.get("allowed_actions") == ["list", "search", "show"]
    assert not_found.get("next_actions"), not_found

    # Missing query for search.
    missing_q = json.loads(_run(fn(action="search", format="json")))
    assert missing_q["ok"] is False
    assert missing_q.get("next_actions"), missing_q

    # Stamp present on every error.
    for payload in (bad_action, not_found, missing_q):
        assert payload["handler_version"] == _HANDLER_VERSION
        assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_skill"]


# ---------------------------------------------------------------------------
# 7. failure_policy on every step
# ---------------------------------------------------------------------------


def test_skill_recipes_have_failure_policy(all_recipes) -> None:
    for recipe in all_recipes:
        for step in recipe["steps"]:
            fp = step.get("failure_policy")
            assert isinstance(fp, dict), (recipe["name"], step["id"])
            assert fp.get("on_error"), (recipe["name"], step["id"])
            assert fp["on_error"] in {
                "abort_recipe",
                "continue_with_reduced_confidence",
                "continue_with_warning",
                "skip_optional",
                "manual_inspection_required",
            }, fp


# ---------------------------------------------------------------------------
# 8. success_criteria on every recipe
# ---------------------------------------------------------------------------


def test_skill_recipes_have_success_criteria(all_recipes) -> None:
    for recipe in all_recipes:
        sc = recipe.get("success_criteria")
        assert isinstance(sc, list) and sc, recipe["name"]
        # Every entry is a non-empty string.
        for entry in sc:
            assert isinstance(entry, str) and entry.strip()


# ---------------------------------------------------------------------------
# 9. no deprecated aliases in any recipe
# ---------------------------------------------------------------------------


def test_skill_safe_refactor_uses_no_deprecated_aliases(all_recipes) -> None:
    for recipe in all_recipes:
        blob = json.dumps(recipe, ensure_ascii=False).lower()
        for alias in _DEPRECATED_ALIASES:
            assert alias not in blob, (recipe["name"], alias)
        # Also check tools_used and per-step tool fields.
        for tool in recipe.get("tools_used", []):
            assert tool not in _DEPRECATED_ALIASES, (recipe["name"], tool)
        for step in recipe["steps"]:
            assert step.get("tool") not in _DEPRECATED_ALIASES, (
                recipe["name"], step["id"]
            )


# ---------------------------------------------------------------------------
# 10. recipe_for_handler_features field present
# ---------------------------------------------------------------------------


def test_skill_recipe_for_handler_features_present(all_recipes) -> None:
    flag_set = set(hlt._HANDLER_FEATURES)
    for recipe in all_recipes:
        rfhf = recipe.get("recipe_for_handler_features")
        assert isinstance(rfhf, list) and rfhf, recipe["name"]
        # Every flag the recipe lists must actually exist in the bundle
        # (otherwise the recipe is targeting features that aren't shipped).
        unknown = [f for f in rfhf if f not in flag_set]
        assert not unknown, (recipe["name"], unknown)


def test_skill_safe_refactor_targets_patch_v2_features(safe_refactor) -> None:
    rfhf = set(safe_refactor["recipe_for_handler_features"])
    required = {
        "patch.workspace_path_guard",
        "patch.apply_validate_gate",
        "patch.structured_validation",
    }
    assert required <= rfhf, rfhf


def test_skill_impact_review_targets_impact_v2(impact_review) -> None:
    rfhf = set(impact_review["recipe_for_handler_features"])
    assert "impact.boundary_contracts" in rfhf
    assert "search.source_confidence" in rfhf


# ---------------------------------------------------------------------------
# 11. contract_version stays skill.v2
# ---------------------------------------------------------------------------


def test_skill_contract_version_remains_skill_v2() -> None:
    assert _CONTRACT_VERSIONS["omni_skill"] == "skill.v2"
    fn = _build_skill_tool()
    raw = _run(fn(action="list", format="json"))
    payload = json.loads(raw)
    assert payload["contract_version"] == "skill.v2"


def test_skill_handler_version_is_r8() -> None:
    # r8 is the floor that introduced the 2.1.0 recipes; later audit rounds
    # bump _HANDLER_VERSION but must keep the skill.recipes_2_1 features.
    # Use a numeric round comparison so r10+ doesn't trip on lexicographic
    # ordering (string "r10" < "r8" alphabetically).
    import re as _re
    m = _re.search(r"\.r(\d+)$", _HANDLER_VERSION)
    assert m, f"unexpected handler_version shape: {_HANDLER_VERSION}"
    assert int(m.group(1)) >= 8
    flags = set(hlt._HANDLER_FEATURES)
    assert "skill.recipes_2_1" in flags
    assert "skill.workflow_contract_alignment" in flags


# ---------------------------------------------------------------------------
# 12. all recipe versions are 2.1.0
# ---------------------------------------------------------------------------


def test_skill_recipe_versions_are_2_1_0(all_recipes) -> None:
    for recipe in all_recipes:
        assert recipe["version"] == "2.1.0", (recipe["name"], recipe["version"])


# ---------------------------------------------------------------------------
# Bonus — top-level next_actions on every recipe (not just on show).
# ---------------------------------------------------------------------------


def test_skill_recipes_top_level_next_actions(all_recipes) -> None:
    for recipe in all_recipes:
        actions = recipe.get("next_actions")
        assert isinstance(actions, list) and actions, recipe["name"]


# ---------------------------------------------------------------------------
# Bonus — show response includes contract-relevant fields surfaced via
# the loader's to_dict() pass-through.
# ---------------------------------------------------------------------------


def test_skill_show_surfaces_success_criteria_and_handler_features() -> None:
    """The Skill dataclass must propagate the new top-level fields all
    the way to the show response."""
    fn = _build_skill_tool()
    raw = _run(fn(action="show", name="omni-safe-refactor", format="json"))
    payload = json.loads(raw)
    skill = payload["skill"]
    assert skill.get("success_criteria"), skill.keys()
    assert skill.get("recipe_for_handler_features"), skill.keys()
    assert skill.get("next_actions"), skill.keys()
