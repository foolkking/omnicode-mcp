"""Contract tests for the omni_edit deprecated-alias path guard (audit-bundle.r9).

P1-2: omni_edit is a deprecated compatibility alias for omni_patch. Before
r9 its preview/validate/apply branches hit the /patch/* backend directly,
without the patch.v2 workspace path guard the modern tool enforces — so the
alias could be a bypass around the new safety edge.

This file pins the post-fix behaviour:

* path traversal ('../../outside.py') is rejected on preview / validate / apply
* absolute paths are rejected
* the backend is NEVER called when the guard fires
* no session is created on a bad path
* the JSON error envelope carries deprecated=true + replacement='omni_patch'
  + use_instead + handler_version + alias.compat.v1 contract
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List

from omnicode_adapters.mcp_server.high_level_tools import (
    _ALIAS_COMPAT_CONTRACT,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim + backend-call recorder
# ---------------------------------------------------------------------------


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _build_tools() -> Dict[str, Any]:
    """Wire up the tools with a make_request that records every call so a
    test can assert the backend was *not* touched on a guard rejection."""
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        # If a test ever does reach the backend, return a benign success so
        # we can still introspect the (unexpected) call.
        return {"result": {"success": True, "message": "stub", "session_id": "stub-sid"}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = dict(mcp.tools)
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


_TRAVERSAL = "../../outside.py"
_ABSOLUTE = "C:\\Windows\\Temp\\omni_bad.py"


def _assert_alias_guard_envelope(payload: Dict[str, Any]) -> None:
    assert payload["ok"] is False
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_edit"
    assert payload["replacement"] == "omni_patch"
    assert payload.get("use_instead")
    assert "path-guard" in payload["error"].lower()
    assert payload.get("allowed_paths_pattern")
    assert payload.get("next_actions")
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT


# ---------------------------------------------------------------------------
# 1-3. traversal rejected on preview / validate / apply
# ---------------------------------------------------------------------------


def test_omni_edit_alias_rejects_path_traversal_preview() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="preview", file=_TRAVERSAL,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    _assert_alias_guard_envelope(payload)
    assert "traversal" in payload["error"].lower() or ".." in payload["error"]
    assert "/patch/preview" not in tools["__captured__"]


def test_omni_edit_alias_rejects_path_traversal_validate() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="validate", file=_TRAVERSAL,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    _assert_alias_guard_envelope(payload)
    assert "/patch/validate" not in tools["__captured__"]


def test_omni_edit_alias_rejects_path_traversal_apply() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="apply", file=_TRAVERSAL,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    _assert_alias_guard_envelope(payload)
    assert "/patch/apply" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 4. absolute path rejected
# ---------------------------------------------------------------------------


def test_omni_edit_alias_rejects_absolute_path() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="preview", file=_ABSOLUTE,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    _assert_alias_guard_envelope(payload)
    assert "absolute" in payload["error"].lower()
    assert "/patch/preview" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 5. backend not called on any bad-path action
# ---------------------------------------------------------------------------


def test_omni_edit_alias_path_guard_backend_not_called() -> None:
    tools = _build_tools()
    for action in ("preview", "validate", "apply"):
        _run(tools["omni_edit"](
            action=action, file=_TRAVERSAL,
            content="print('bad')\n", format="json",
        ))
    captured = tools["__captured__"]
    # No /patch/* and no /edit endpoint should have been hit.
    assert not any(ep.startswith("/patch/") for ep in captured), captured
    assert "/edit" not in captured


# ---------------------------------------------------------------------------
# 6. error mentions the modern replacement
# ---------------------------------------------------------------------------


def test_omni_edit_alias_error_mentions_replacement_omni_patch() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="apply", file=_TRAVERSAL,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["replacement"] == "omni_patch"
    assert "omni_patch" in payload["use_instead"]
    assert any("omni_patch" in a for a in payload["next_actions"])


# ---------------------------------------------------------------------------
# 7. no session created on a bad path
# ---------------------------------------------------------------------------


def test_omni_edit_alias_does_not_create_session_on_bad_path() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="apply", file=_TRAVERSAL,
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    # The guard envelope must not carry a session_id, and no apply call
    # was made that could have created one.
    assert "session_id" not in payload
    assert "/patch/apply" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 8. valid relative path passes the guard and reaches the backend
# ---------------------------------------------------------------------------


def test_omni_edit_alias_valid_path_reaches_backend() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="preview", file="tests/tmp_eval_alias.py",
        content="print('ok')\n", format="json",
    ))
    payload = json.loads(raw)
    # Guard passed → backend was called → ok mirrors the stub success.
    assert "/patch/preview" in tools["__captured__"]
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_edit"
    assert payload["contract_version"] == _ALIAS_COMPAT_CONTRACT


# ---------------------------------------------------------------------------
# 9. text format also blocks the bad path (no plain-text bypass)
# ---------------------------------------------------------------------------


def test_omni_edit_alias_text_format_also_rejects_bad_path() -> None:
    tools = _build_tools()
    raw = _run(tools["omni_edit"](
        action="apply", file=_TRAVERSAL,
        content="print('bad')\n", format="text",
    ))
    assert "path-guard" in raw.lower()
    assert "/patch/apply" not in tools["__captured__"]
