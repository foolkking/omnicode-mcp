"""LLM-off contract tests for deprecated ai_edit alias."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tests.unit.mcp_harness import build_tools, run


def test_omni_edit_ai_edit_rejects_llm_off_before_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_LLM_MODE", "off")
    monkeypatch.setenv("OMNICODE_LLM_ROUTER", "false")
    tools = build_tools({
        "/edit": {
            "success": True,
            "message": "should not be called",
        },
    })

    raw = run(tools["omni_edit"](
        action="ai_edit",
        file="missing.py",
        instructions="rewrite this with AI",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["action"] == "ai_edit"
    assert payload["llm_disabled"] is True
    assert payload["llm_mode"] == "off"
    assert payload["llm_router_enabled"] is False
    assert payload["error"] == "LLM editing is disabled"
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/edit" not in captured


def test_omni_edit_ai_edit_llm_off_takes_precedence_over_file_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_LLM_MODE", "off")
    tools = build_tools({})

    raw = run(tools["omni_edit"](
        action="ai_edit",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["llm_disabled"] is True
    assert payload["error"] == "LLM editing is disabled"
