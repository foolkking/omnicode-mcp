"""Unit tests for discover_tools — Tool Intent Registry edition.

Drives the pure ``_recommend_tools`` helper directly so we don't need
to spin up a FastMCP instance.  Covers EN + ZH queries, fallback
behaviour, deprecated-alias handling, and the ``matcher`` switch.
"""

from __future__ import annotations

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _INTENT_REGISTRY,
    _TOOL_CATALOGUE,
    _recommend_tools,
)


# ---------------------------------------------------------------------------
# 1. Empty query — full default listing + workflow with rollback step.
# ---------------------------------------------------------------------------


def test_empty_query_lists_all_core_tools() -> None:
    out = _recommend_tools("")
    for tool in (
        "omni_search", "omni_read", "omni_impact", "omni_diagnostics",
        "omni_context", "omni_memory", "omni_patch", "omni_skill",
    ):
        assert tool in out, f"default listing missing {tool}"


def test_empty_query_does_not_recommend_deprecated_aliases() -> None:
    out = _recommend_tools("")
    assert "omni_analyze" not in out
    assert "omni_edit" not in out
    assert "omni_intelligence" not in out


def test_empty_query_includes_rollback_step() -> None:
    out = _recommend_tools("")
    assert "rollback" in out.lower()
    assert "preview" in out.lower()
    assert "validate" in out.lower()
    assert "apply" in out.lower()


# ---------------------------------------------------------------------------
# 2. English natural-language queries (regression tests).
# ---------------------------------------------------------------------------


def test_understand_before_edit_recommends_context_and_friends_en() -> None:
    out = _recommend_tools("I need to understand a function before editing it")
    assert "No tools matching" not in out
    for tool in ("omni_context", "omni_read", "omni_impact", "omni_search"):
        assert tool in out, f"{tool} should rank for understand-before-edit query"


def test_safe_edit_recommends_omni_patch_first_en() -> None:
    out = _recommend_tools(
        "I want to safely modify code with preview validate apply rollback"
    )
    assert "omni_patch" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_patch" in first_match
    lower = out.lower()
    for step in ("preview", "validate", "apply", "rollback"):
        assert step in lower


def test_references_query_recommends_omni_search_en() -> None:
    out = _recommend_tools("find all references of a function")
    assert "omni_search" in out
    assert "references" in out.lower()


def test_risk_query_recommends_omni_impact_en() -> None:
    out = _recommend_tools("analyze risk before changing a symbol")
    assert "omni_impact" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_impact" in first_match


# ---------------------------------------------------------------------------
# 3. Chinese natural-language queries (NEW).
# ---------------------------------------------------------------------------


def test_understand_before_edit_recommends_context_and_friends_zh() -> None:
    out = _recommend_tools("我想在修改函数前先理解它")
    assert "No tools matching" not in out
    assert "No direct keyword match" not in out
    for tool in ("omni_context", "omni_read", "omni_impact", "omni_search"):
        assert tool in out, f"{tool} should rank for ZH understand-before-edit query"


def test_safe_edit_recommends_omni_patch_first_zh() -> None:
    out = _recommend_tools("我要安全修改代码,先预览、验证、应用,必要时回滚")
    assert "omni_patch" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_patch" in first_match, (
        f"omni_patch should top the ZH safe-edit query, got: {first_match!r}"
    )
    # Pipeline rendering should still walk the four-step flow.
    lower = out.lower()
    for step in ("preview", "validate", "apply", "rollback"):
        assert step in lower


def test_references_query_recommends_omni_search_zh() -> None:
    out = _recommend_tools("查找这个函数的所有引用")
    assert "omni_search" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_search" in first_match


def test_risk_query_recommends_omni_impact_zh() -> None:
    out = _recommend_tools("修改前分析这个符号的影响范围和风险")
    assert "omni_impact" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_impact" in first_match


def test_diagnostics_query_recommends_omni_diagnostics_zh() -> None:
    out = _recommend_tools("检查这个文件有没有 lint 或类型错误")
    assert "omni_diagnostics" in out
    body_lines = out.splitlines()
    first_match = next(
        (line for line in body_lines if line.strip().startswith("• ")), ""
    )
    assert "omni_diagnostics" in first_match


# ---------------------------------------------------------------------------
# 4. Deprecated aliases — never first-class unless explicitly named.
# ---------------------------------------------------------------------------


def test_modify_query_does_not_recommend_deprecated_omni_edit() -> None:
    out = _recommend_tools("I want to modify code")
    assert "omni_patch" in out
    body_top = out.split("Default workflow")[0]
    assert "omni_edit" not in body_top, (
        "omni_edit must not surface for a generic 'modify' query"
    )


def test_explicit_alias_query_surfaces_modern_replacement() -> None:
    out = _recommend_tools("omni_edit")
    assert "omni_edit" in out
    assert "omni_patch" in out
    assert "deprecated" in out.lower()


# ---------------------------------------------------------------------------
# 5. Fallback — zero-match never returns a dead end.
# ---------------------------------------------------------------------------


def test_zero_match_falls_back_to_default_listing_en() -> None:
    out = _recommend_tools("xyzzy plover frobnicate")
    assert "omni_context" in out
    assert "omni_patch" in out
    assert (
        "Default workflow" in out
        or "Recommended flow" in out
        or "default tool listing" in out.lower()
    )


def test_zero_match_falls_back_to_default_listing_zh() -> None:
    """A nonsense CJK string with no keyword overlap → default listing."""
    out = _recommend_tools("駱駝抹茶咖啡因星雲")  # made-up CJK noise
    # Even without matching any keyword we must hand the AI the default
    # workflow rather than the legacy "No tools matching" dead-end.
    assert "omni_context" in out
    assert "omni_patch" in out
    assert "No tools matching" not in out


# ---------------------------------------------------------------------------
# 6. why_matched explanations.
# ---------------------------------------------------------------------------


def test_response_includes_why_matched_explanations() -> None:
    out = _recommend_tools(
        "I want to safely modify code with preview validate apply rollback"
    )
    assert "why_matched" in out
    assert "intent:" in out


# ---------------------------------------------------------------------------
# 7. matcher='embedding' is reserved and falls back to rule-based.
# ---------------------------------------------------------------------------


def test_matcher_embedding_falls_back_with_notice() -> None:
    out = _recommend_tools("find all references", matcher="embedding")
    # Still produces real recommendations …
    assert "omni_search" in out
    # … and tells the caller the embedding backend is reserved for later.
    assert "matcher='embedding'" in out
    assert "rule-based" in out.lower()


def test_matcher_default_is_rule() -> None:
    out_default = _recommend_tools("find all references")
    out_rule = _recommend_tools("find all references", matcher="rule")
    # Default should produce the same body as explicit matcher='rule'.
    assert out_default == out_rule


# ---------------------------------------------------------------------------
# 8. Registry sanity: catalogue and intents are non-empty + well-formed.
# ---------------------------------------------------------------------------


def test_intent_registry_has_required_intent_ids() -> None:
    ids = {i["id"] for i in _INTENT_REGISTRY}
    required = {
        "understand_before_edit",
        "safe_patch_flow",
        "find_references",
        "risk_analysis",
        "diagnostics_check",
        "memory_advisory",
        "workflow_recipe",
    }
    missing = required - ids
    assert not missing, f"missing intent IDs: {missing}"


def test_intent_records_carry_bilingual_patterns() -> None:
    for intent in _INTENT_REGISTRY:
        assert "patterns_en" in intent, intent["id"]
        assert "patterns_zh" in intent, intent["id"]
        assert "keywords_en" in intent, intent["id"]
        assert "keywords_zh" in intent, intent["id"]
        assert "why_label" in intent, intent["id"]
        assert intent["recommended_tools"], intent["id"]


def test_tool_catalogue_has_bilingual_keywords_for_core() -> None:
    for t in _TOOL_CATALOGUE:
        if t.get("deprecated"):
            continue
        # Every non-deprecated tool should have at least one ZH keyword
        # to support Chinese discovery (discover_tools itself is excluded
        # because its description is meta).
        if t["name"] == "discover_tools":
            continue
        assert t.get("keywords_zh"), (
            f"{t['name']} has no Chinese keywords — ZH discovery will miss it"
        )
