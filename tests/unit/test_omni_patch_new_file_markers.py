"""Contract tests for the omni_patch new_file markers (audit-bundle.r10).

P1-A from Round 2: omni_patch validate/apply was returning ok=true /
validation_passed=true for nonexistent files without telling the AI editor
the file didn't exist. r10 adds two booleans to every preview/validate/
apply payload:

    file_exists: bool   — True if the resolved workspace path is a file
    new_file:    bool   — inverse of file_exists (creation case)

These markers are advisory only — the path guard still runs first, so
traversal / absolute paths can never reach the new_file logic.

Pinned by this round:

* validate on a nonexistent file → ok mirrors validation_passed,
  file_exists=False, new_file=True
* validate on an existing file → file_exists=True, new_file=False
* preview / apply on a nonexistent file → file_exists=False, new_file=True
* apply success carries the markers
* path traversal / absolute paths are STILL rejected before any new_file
  logic runs (no leak)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
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


def _build_tools(routes: Dict[str, Any]) -> Dict[str, Any]:
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


@pytest.fixture(autouse=True)
def _pin_workspace_root_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin _get_workspace_root() to the test process cwd so the
    "existing file" assertions don't depend on the user's active
    workspace registry entry. Without this, r11's aligned helper might
    point at a registered workspace where ``tests/conftest.py`` doesn't
    exist."""
    cwd = Path.cwd().resolve()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (cwd, "test_pinned", []),
    )


# A path that is workspace-relative and does NOT exist on disk under the
# current working directory. Tests run from the repo root, so this is
# safe regardless of where pytest is invoked from.
_NEW_FILE_RELPATH = "tests/_r10_new_file_marker_target.py"
_EXISTING_FILE_RELPATH = "tests/conftest.py"  # exists in the repo


def _read_route_for_existing() -> Dict[str, Any]:
    """Scripted /read response used by the r12 backend probe to mean
    "this file exists"."""
    return {
        "success": True,
        "content": "x = 1\n",
        "language": "python",
        "total_lines": 1,
        "symbols": [],
        "file_path": "/abs/path/to/file.py",
        "workspace_root": "/abs/path/to",
    }


def _read_route_for_missing() -> Dict[str, Any]:
    """Scripted /read response used by the r12 backend probe to mean
    "this file does not exist"."""
    return {
        "success": False,
        "error": "File not found: x.py",
    }


# ---------------------------------------------------------------------------
# 1. validate on a nonexistent file
# ---------------------------------------------------------------------------


def test_patch_validate_new_file_marks_new_file_true() -> None:
    # Backend says "validation passed" — we still want the markers.
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/validate": {
            "success": True, "message": "Validation passed",
            "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file=_NEW_FILE_RELPATH,
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["validation_passed"] is True
    assert payload["file_exists"] is False
    assert payload["new_file"] is True
    assert payload["file_marker_authoritative"] is True
    assert payload["file_marker_source"] in (
        "backend_patch_response", "backend_read_probe",
    )
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_patch"]
    assert payload["handler_version"] == _HANDLER_VERSION


def test_patch_validate_existing_file_marks_new_file_false() -> None:
    tools = _build_tools({
        "/read": _read_route_for_existing(),
        "/patch/validate": {
            "success": True, "message": "Validation passed",
            "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file=_EXISTING_FILE_RELPATH,
        content="# placeholder\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["file_exists"] is True
    assert payload["new_file"] is False
    assert payload["file_marker_authoritative"] is True


# ---------------------------------------------------------------------------
# 2. preview on a nonexistent file
# ---------------------------------------------------------------------------


def test_patch_preview_new_file_marks_new_file_true() -> None:
    # Real backends typically refuse to preview a nonexistent file. r19
    # (patch.preview_new_file_ok) flips the contract: when the probe-
    # authoritative ``new_file`` marker is true, omni_patch synthesizes
    # a successful creation diff locally instead of bubbling the
    # backend's ``File does not exist`` as an error. The markers ride
    # along on the success envelope so AI editors can still tell this
    # is a creation flow.
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview", file=_NEW_FILE_RELPATH,
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True, (
        "r19: new-file preview should be ok=True with a synthesized diff"
    )
    assert payload["file_exists"] is False
    assert payload["new_file"] is True
    assert payload["file_marker_authoritative"] is True
    assert payload["preview_synthesized"] is True
    assert payload["lines_added"] == 1  # "print('x')"
    assert payload["lines_removed"] == 0
    assert "/dev/null" in payload["diff"]
    assert f"+++ b/{_NEW_FILE_RELPATH}" in payload["diff"]
    assert "+print('x')" in payload["diff"]
    joined = " ".join(payload["next_actions"]).lower()
    assert "validate" in joined and "apply" in joined


def test_patch_preview_existing_file_marks_new_file_false() -> None:
    tools = _build_tools({
        "/read": _read_route_for_existing(),
        "/patch/preview": {
            "success": True,
            "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
            "lines_added": 1, "lines_removed": 1,
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview", file=_EXISTING_FILE_RELPATH,
        content="# placeholder\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["file_exists"] is True
    assert payload["new_file"] is False


# ---------------------------------------------------------------------------
# 3. apply on a nonexistent file (creation case)
# ---------------------------------------------------------------------------


def test_patch_apply_new_file_marks_new_file_true() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/validate": {
            "success": True, "message": "Validation passed", "issues": [],
        },
        "/patch/apply": {
            "success": True, "message": "Created",
            "session_id": "r10-new-file-sid",
            "lines_added": 1, "lines_removed": 0,
            "rollback_available": True,
        },
    })
    raw = _run(tools["omni_patch"](
        action="apply", file=_NEW_FILE_RELPATH,
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["session_id"] == "r10-new-file-sid"
    assert payload["rollback_available"] is True
    assert payload["file_exists"] is False
    assert payload["new_file"] is True
    assert payload["validation_passed"] is True
    assert payload["validation_bypassed"] is False


def test_patch_apply_existing_file_marks_new_file_false() -> None:
    tools = _build_tools({
        "/read": _read_route_for_existing(),
        "/patch/validate": {
            "success": True, "message": "Validation passed", "issues": [],
        },
        "/patch/apply": {
            "success": True, "message": "ok",
            "session_id": "r10-existing-sid",
            "lines_added": 0, "lines_removed": 0,
            "rollback_available": True,
        },
    })
    raw = _run(tools["omni_patch"](
        action="apply", file=_EXISTING_FILE_RELPATH,
        content="# placeholder\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["file_exists"] is True
    assert payload["new_file"] is False


# ---------------------------------------------------------------------------
# 4. path guard still wins — markers must NOT be a bypass
# ---------------------------------------------------------------------------


def test_patch_new_file_does_not_bypass_path_guard_traversal() -> None:
    tools = _build_tools({})
    for action in ("preview", "validate", "apply"):
        raw = _run(tools[action_field := "omni_patch"](
            action=action, file="../../outside_new.py",
            content="print('bad')\n", format="json",
        ))
        payload = json.loads(raw)
        assert payload["ok"] is False, action
        assert "path-guard" in payload["error"].lower(), action
        # Markers must NOT leak through on a guard rejection — the path
        # never reached the filesystem stat call.
        assert "file_exists" not in payload, action
        assert "new_file" not in payload, action
        # No backend hit either.
        assert "/patch/preview" not in tools["__captured__"]
        assert "/patch/validate" not in tools["__captured__"]
        assert "/patch/apply" not in tools["__captured__"]


def test_patch_new_file_does_not_bypass_path_guard_absolute() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_patch"](
        action="apply",
        file=os.path.abspath(os.sep + "tmp" + os.sep + "omni_r10_bad.py"),
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "path-guard" in payload["error"].lower()
    assert "file_exists" not in payload
    assert "new_file" not in payload


# ---------------------------------------------------------------------------
# 5. validation-block error path also carries the markers (so an editor
#    can see "creation case + 2 errors" without re-statting).
# ---------------------------------------------------------------------------


def test_patch_apply_validation_block_includes_new_file_markers() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/validate": {
            "success": False,
            "message": "Validation failed",
            "issues": [
                {"source": "guard", "severity": "error",
                 "line": 1, "rule": "invalid-syntax",
                 "message": "Expected `:`"},
            ],
        },
    })
    raw = _run(tools["omni_patch"](
        action="apply", file=_NEW_FILE_RELPATH,
        content="def bad(", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["validation_passed"] is False
    assert payload["validation_bypassed"] is False
    assert payload["file_exists"] is False
    assert payload["new_file"] is True
    # And nothing was applied — no /patch/apply call should have happened.
    assert "/patch/apply" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 6. handler features advertise the new markers
# ---------------------------------------------------------------------------


def test_handler_features_advertise_new_file_markers() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "patch.new_file_markers" in flags
    assert "read.error_next_actions" in flags
    assert "read.valid_modes_envelope" in flags
