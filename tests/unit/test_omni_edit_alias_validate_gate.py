"""Contract tests for the omni_edit deprecated-alias validate gate
(audit-bundle.r13, P0-A close).

Round 4 found omni_edit(action="apply") was bypassing the patch.v2
``apply_validate_gate`` — it could write invalid Python with ok=true and
no audit fields. This file pins the post-fix behaviour:

* apply runs ``_do_validate`` first (same helper omni_patch uses)
* validation_passed=false → ok=false, no /patch/apply call, no session
* force=True without force_reason → ok=false (gate still holds)
* force=True with force_reason → write goes through, but the response
  carries validation_passed=false + validation_bypassed=true +
  force/force_reason + a ⚠️ warning as next_actions[0]
* the apply success envelope lifts session_id / rollback_available /
  validation_passed / validation_bypassed / force / force_reason /
  diff metadata to the top level for shape-parity with omni_patch v2
* contract_version stays alias.compat.v1
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _ALIAS_COMPAT_CONTRACT,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim + scripted backend
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


def _build_tools(routes: Dict[str, Any]) -> Dict[str, Callable[..., Any]]:
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        if endpoint in routes:
            payload = routes[endpoint]
        else:
            payload = None
            key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
            if key in routes:
                payload = routes[key]
        if payload is None:
            return {"result": {}}
        if callable(payload):
            payload = payload(method, endpoint, kwargs)
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _bad_python_routes() -> Dict[str, Any]:
    """Backend returns a hard syntax error from /patch/validate."""
    return {
        "/patch/validate": {
            "success": False,
            "issues": [
                {
                    "source": "ruff",
                    "severity": "error",
                    "line": 1,
                    "column": 9,
                    "rule": "E999",
                    "message": "SyntaxError: invalid syntax",
                },
            ],
            "tools_run": ["ruff"],
            "tools_skipped": [],
        },
        "/patch/apply": {
            "success": True,
            "session_id": "should-never-appear",
            "lines_added": 1,
            "lines_removed": 0,
        },
    }


def _good_python_routes() -> Dict[str, Any]:
    return {
        "/patch/validate": {"success": True, "issues": [], "tools_run": ["ruff"]},
        "/patch/apply": {
            "success": True,
            "session_id": "sess-r13",
            "rollback_available": True,
            "lines_added": 1,
            "lines_removed": 0,
            "original_hash": "h1",
            "new_hash": "h2",
            "diff": "+def good():\n+    return 1\n",
        },
    }


_BAD_CONTENT = "def bad(:\n    pass\n"
_GOOD_CONTENT = "def good():\n    return 1\n"


# ---------------------------------------------------------------------------
# 1. apply inherits the validate gate
# ---------------------------------------------------------------------------


def test_omni_edit_alias_inherits_validate_gate() -> None:
    """omni_edit(action='apply') must call /patch/validate before
    /patch/apply, exactly like omni_patch."""
    tools = _build_tools(_good_python_routes())
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_GOOD_CONTENT,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT

    captured = tools["__captured__"]
    # validate must have been called
    assert "/patch/validate" in captured
    # ... and apply must have been called too
    assert "/patch/apply" in captured


# ---------------------------------------------------------------------------
# 2. invalid Python is BLOCKED on apply (the P0-A bug)
# ---------------------------------------------------------------------------


def test_omni_edit_alias_blocks_invalid_python_apply() -> None:
    """Hard syntax error → ok=false + validation_passed=false +
    validation_bypassed=false + checks[] + counts.error >= 1.
    No session_id, no /patch/apply call, no write."""
    tools = _build_tools(_bad_python_routes())
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_BAD_CONTENT,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["validation_passed"] is False
    assert payload["validation_bypassed"] is False
    assert "validation" in payload["error"].lower()
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT

    # Must carry structured checks + counts.
    checks = payload["checks"]
    assert isinstance(checks, list) and len(checks) >= 1
    assert any(c.get("severity") == "error" for c in checks)
    assert payload["counts"]["error"] >= 1

    # No session
    assert "session_id" not in payload or payload.get("session_id") is None

    # next_actions must guide the editor.
    assert payload.get("next_actions")
    joined = " ".join(payload["next_actions"]).lower()
    assert "fix" in joined or "validate" in joined or "omni_patch" in joined


def test_omni_edit_alias_does_not_write_when_validation_fails() -> None:
    """The /patch/apply backend MUST NOT have been called when
    validation failed and no force was passed."""
    tools = _build_tools(_bad_python_routes())
    _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_BAD_CONTENT,
        format="json",
    ))
    captured = tools["__captured__"]
    assert "/patch/validate" in captured  # validate WAS called
    assert "/patch/apply" not in captured  # but apply was NOT


# ---------------------------------------------------------------------------
# 3. force semantics
# ---------------------------------------------------------------------------


def test_omni_edit_alias_force_requires_reason() -> None:
    """force=True without force_reason → ok=false. Same contract as
    omni_patch v2: an audit trail is mandatory."""
    tools = _build_tools(_bad_python_routes())
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_BAD_CONTENT,
        force=True,
        force_reason=None,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "force_reason" in payload["error"].lower()
    assert payload["force"] is True
    assert payload["force_reason"] is None
    # Apply must NOT have been called.
    assert "/patch/apply" not in tools["__captured__"]


def test_omni_edit_alias_force_visibility() -> None:
    """force=True with force_reason → write goes through, but the
    response shouts about the bypass."""
    tools = _build_tools({
        # validate fails (same bad content)
        "/patch/validate": _bad_python_routes()["/patch/validate"],
        # but force=True should still get to apply
        "/patch/apply": {
            "success": True,
            "session_id": "sess-force",
            "rollback_available": True,
            "lines_added": 1,
            "lines_removed": 0,
        },
    })
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_BAD_CONTENT,
        force=True,
        force_reason="round4 alias force visibility test",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["validation_passed"] is False
    assert payload["validation_bypassed"] is True
    assert payload["force"] is True
    assert payload["force_reason"] == "round4 alias force visibility test"
    # Top-level lifted fields.
    assert payload["session_id"] == "sess-force"
    assert payload["rollback_available"] is True
    # The first next_action MUST be the warning.
    first = (payload["next_actions"] or [""])[0]
    assert "⚠" in first or "bypass" in first.lower()
    # alias envelope still present
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"


def test_omni_edit_alias_force_write_is_rollbackable() -> None:
    """A force=True apply must still produce a session that supports
    rollback so an editor can undo the bypass."""
    tools = _build_tools({
        "/patch/validate": _bad_python_routes()["/patch/validate"],
        "/patch/apply": {
            "success": True,
            "session_id": "rollback-me",
            "rollback_available": True,
            "lines_added": 1,
            "lines_removed": 0,
        },
        "/patch/rollback": {"success": True, "message": "rolled back"},
    })
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_BAD_CONTENT,
        force=True,
        force_reason="rollback chain test",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["session_id"] == "rollback-me"
    assert payload["rollback_available"] is True

    # Rollback through the alias works.
    raw_rb = _run(tools["omni_edit"](
        action="rollback",
        session_id="rollback-me",
        format="json",
    ))
    rb = json.loads(raw_rb)
    assert rb["ok"] is True
    assert rb["rolled_back"] is True


# ---------------------------------------------------------------------------
# 4. top-level field parity with omni_patch v2
# ---------------------------------------------------------------------------


def test_omni_edit_alias_top_level_patch_fields() -> None:
    """Successful apply lifts the same audit fields omni_patch v2
    surfaces: session_id, rollback_available, validation_passed,
    validation_bypassed, force, force_reason, lines_added, lines_removed,
    original_hash, new_hash."""
    tools = _build_tools(_good_python_routes())
    raw = _run(tools["omni_edit"](
        action="apply",
        file="tests/tmp_round4_alias.py",
        content=_GOOD_CONTENT,
        format="json",
    ))
    payload = json.loads(raw)
    for key in (
        "session_id", "rollback_available",
        "validation_passed", "validation_bypassed",
        "force", "force_reason",
        "lines_added", "lines_removed",
        "original_hash", "new_hash",
    ):
        assert key in payload, f"missing top-level key {key!r}: {payload}"
    assert payload["validation_passed"] is True
    assert payload["validation_bypassed"] is False
    assert payload["force"] is False
    assert payload["force_reason"] is None
    assert payload["session_id"] == "sess-r13"
    # alias envelope
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    assert payload["use_instead"]
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT


# ---------------------------------------------------------------------------
# 5. ai_edit branch returns JSON when format=json
# ---------------------------------------------------------------------------


def test_omni_edit_ai_edit_json_disabled_or_dry_run_envelope() -> None:
    """Whether ai_edit is disabled (backend errors out) OR a dry_run is
    requested, format='json' must return parseable JSON with the alias
    envelope — never plain text."""
    # Case 1: backend reports the feature is disabled
    routes_disabled = {
        "/edit": {"error": "ai_edit disabled (OMNICODE_LLM_ROUTER=false)"},
    }
    tools = _build_tools(routes_disabled)
    raw = _run(tools["omni_edit"](
        action="ai_edit",
        file="tests/tmp_round4_alias.py",
        instructions="add a docstring",
        dry_run=False,
        format="json",
    ))
    payload = json.loads(raw)  # MUST parse — that's the whole point of P1-E.
    assert payload["ok"] is False
    assert payload["action"] == "ai_edit"
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    assert "next_actions" in payload

    # Case 2: dry_run path returns structured preview JSON
    routes_dry = {
        "/edit": {
            "success": True,
            "preview_diff": "+def f():\n+    \"\"\"hi.\"\"\"\n",
            "preview_summary": {"lines_added": 2, "lines_removed": 0},
            "suggested_content": "def f():\n    \"\"\"hi.\"\"\"\n",
        },
    }
    tools = _build_tools(routes_dry)
    raw = _run(tools["omni_edit"](
        action="ai_edit",
        file="tests/tmp_round4_alias.py",
        instructions="add a docstring",
        dry_run=True,
        format="json",
    ))
    payload = json.loads(raw)  # parseable JSON, not plain text
    assert payload["ok"] is True
    assert payload["action"] == "ai_edit"
    assert payload["dry_run"] is True
    assert payload["lines_added"] == 2
    assert payload["lines_removed"] == 0
    assert "diff" in payload
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    # No write — apply must not have been called.
    assert "/patch/apply" not in tools["__captured__"]


def test_omni_edit_ai_edit_path_guard() -> None:
    """ai_edit must reuse the same path guard (r9) — bad path → ok=false,
    no backend call."""
    tools = _build_tools({})  # no routes; guard must fire first
    raw = _run(tools["omni_edit"](
        action="ai_edit",
        file="../../outside.py",
        instructions="write here",
        dry_run=True,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "path-guard" in payload["error"].lower()
    assert payload["deprecated"] is True
    assert "/edit" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 6. handler features stamp the new flags
# ---------------------------------------------------------------------------


def test_handler_features_advertise_alias_validate_gate() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "alias.edit_validate_gate" in flags
    assert "alias.edit_force_visibility" in flags
    assert "alias.edit_json_ai_edit" in flags
