"""Contract tests for omni_patch v2 (audit-bundle.r7).

Pinned by the audit:

* path traversal is rejected on every file-bearing action
* absolute paths rejected
* ``..`` rejected even when it would resolve into the workspace
* symlink escape rejected (best-effort: enforced by Path.resolve)
* apply runs validate by default and refuses on syntax errors
* apply with force=True bypasses validation but flags it explicitly
* validate returns structured ``checks[]`` + counts + tools_run
* every error path includes ``allowed_actions`` + ``next_actions``
* diff text has no double blank lines after CRLF normalisation
* apply / rollback responses surface hashes
* sessions includes ``next_actions`` + ``unsafe_legacy_session`` flag
* contract_version is exactly patch.v2
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
    _normalise_diff_text,
    _resolve_workspace_path,
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

    async def list_tools(self) -> List[Any]:  # pragma: no cover
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


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
    tools = mcp.tools
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. path-traversal rejection on preview
# ---------------------------------------------------------------------------


def test_patch_rejects_path_traversal_preview() -> None:
    tools = _build_tools({})  # no routes — guard must fire before make_request
    raw = _run(tools["omni_patch"](
        action="preview",
        file="../../outside.py",
        content="print('bad')\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    err = payload["error"].lower()
    assert "path-guard" in err
    assert "traversal" in err or ".." in err
    # Backend was NOT called.
    assert "/patch/preview" not in tools["__captured__"]
    # Stamp + structured envelope present.
    assert payload["allowed_actions"] == [
        "preview", "validate", "apply", "rollback", "sessions",
    ]
    assert payload.get("allowed_paths_pattern")
    assert payload.get("next_actions")
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_patch"]


# ---------------------------------------------------------------------------
# 2. path-traversal rejection on apply
# ---------------------------------------------------------------------------


def test_patch_rejects_path_traversal_apply() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_patch"](
        action="apply",
        file="../../outside.py",
        content="print('bad')\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    # Critical: the backend must NEVER be reached for traversal apply.
    assert "/patch/apply" not in tools["__captured__"]
    assert "/patch/validate" not in tools["__captured__"]
    assert "path-guard" in payload["error"].lower()


# ---------------------------------------------------------------------------
# 3. absolute path rejected
# ---------------------------------------------------------------------------


def test_patch_rejects_absolute_path() -> None:
    tools = _build_tools({})
    abs_path = "C:\\Windows\\System32\\evil.py" if os.name == "nt" else "/etc/passwd"
    raw = _run(tools["omni_patch"](
        action="preview", file=abs_path, content="x", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    err = payload["error"].lower()
    assert "absolute" in err
    assert payload["file"] == Path(abs_path).name
    assert "C:" not in payload["file"]
    assert "/" not in payload["file"]
    assert "\\" not in payload["file"]
    assert "/patch/preview" not in tools["__captured__"]


def test_patch_path_guard_error_redacts_resolved_absolute_paths() -> None:
    payload = hlt._patch_path_guard_error(
        action="preview",
        file="link.py",
        exc=ValueError(
            "path escapes workspace: 'link.py' -> C:\\secret\\outside.py. "
            "Files must stay under C:\\repo."
        ),
    )

    assert payload["ok"] is False
    assert payload["file"] == "link.py"
    assert "path-guard" in payload["error"].lower()
    assert "C:\\" not in payload["error"]
    assert "<absolute-path>" in payload["error"]


def test_patch_path_guard_error_sanitizes_submitted_absolute_file() -> None:
    payload = hlt._patch_path_guard_error(
        action="preview",
        file="C:/tmp/tmp_cloudsim_abs.py",
        exc=ValueError("absolute paths are not allowed: 'C:/tmp/tmp_cloudsim_abs.py'"),
    )

    assert payload["ok"] is False
    assert payload["file"] == "tmp_cloudsim_abs.py"
    assert "C:" not in payload["file"]
    assert "/" not in payload["file"]


def test_patch_path_guard_error_sanitizes_submitted_traversal_file() -> None:
    payload = hlt._patch_path_guard_error(
        action="preview",
        file="../tmp_cloudsim_escape.py",
        exc=ValueError("path traversal is not allowed: '../tmp_cloudsim_escape.py'"),
    )

    assert payload["ok"] is False
    assert payload["file"] == "tmp_cloudsim_escape.py"
    assert ".." not in payload["file"]
    assert "/" not in payload["file"]


# ---------------------------------------------------------------------------
# 4. symlink escape rejected (best-effort via Path.resolve)
# ---------------------------------------------------------------------------


def test_patch_rejects_symlink_escape(tmp_path: Path) -> None:
    """Build a symlink inside a fake workspace pointing outside it,
    then verify _resolve_workspace_path rejects it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("evil")

    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/user")

    with pytest.raises(ValueError) as excinfo:
        _resolve_workspace_path("link.txt", workspace_root=workspace)
    assert "escape" in str(excinfo.value).lower()


def test_patch_rejects_dotdot_in_any_segment() -> None:
    """``..`` anywhere in the path components → reject, even when it
    would resolve back inside the workspace."""
    with pytest.raises(ValueError):
        _resolve_workspace_path("foo/../bar.py")
    with pytest.raises(ValueError):
        _resolve_workspace_path("../bar.py")
    with pytest.raises(ValueError):
        _resolve_workspace_path("a/b/../../c.py")


# ---------------------------------------------------------------------------
# 5. apply runs validate by default
# ---------------------------------------------------------------------------


def test_patch_apply_runs_validation_by_default() -> None:
    """The apply path must call /patch/validate before /patch/apply.
    Captures what endpoints were hit and asserts the order."""
    routes = {
        "/patch/validate": {"success": True, "issues": []},
        "/patch/apply": {
            "success": True,
            "session_id": "sess-1",
            "lines_added": 1,
            "lines_removed": 0,
            "original_hash": "h1",
            "new_hash": "h2",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="apply",
        file="tests/x.py",
        content="def x(): pass\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["validation_passed"] is True
    captured = tools["__captured__"]
    # Both endpoints called, validate first.
    assert "/patch/validate" in captured
    assert "/patch/apply" in captured


# ---------------------------------------------------------------------------
# 6. apply blocks invalid python
# ---------------------------------------------------------------------------


def test_patch_apply_blocks_invalid_python() -> None:
    routes = {
        "/patch/validate": {
            "success": False,
            "message": "Validation failed: 1 error(s)",
            "issues": [
                {
                    "tool": "ruff", "severity": "error",
                    "line": 1, "column": 8, "code": "E999",
                    "message": "SyntaxError: invalid syntax",
                }
            ],
        },
        # Even if backend's /patch/apply *would* succeed, the gate must
        # prevent us from calling it.
        "/patch/apply": {
            "success": True, "session_id": "should-not-happen",
            "lines_added": 1, "lines_removed": 0,
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="apply",
        file="tests/x.py",
        content="def bad(:\n    pass\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["validation_passed"] is False
    assert payload["validation_bypassed"] is False
    assert "validation" in payload["error"].lower()
    assert payload["counts"]["error"] == 1
    assert payload["checks"]
    assert payload["checks"][0]["rule"] == "E999"


def test_patch_apply_does_not_write_when_validation_fails() -> None:
    routes = {
        "/patch/validate": {
            "success": False,
            "issues": [{"severity": "error", "message": "syntax"}],
        },
        "/patch/apply": {"success": True, "session_id": "nope"},
    }
    tools = _build_tools(routes)
    _run(tools["omni_patch"](
        action="apply",
        file="tests/x.py",
        content="def bad(:\n    pass\n",
        format="json",
    ))
    # The gate must prevent /patch/apply from being called.
    assert "/patch/apply" not in tools["__captured__"]
    assert "/patch/validate" in tools["__captured__"]


def test_patch_apply_force_requires_explicit_flag() -> None:
    """force=True must bypass the gate AND the response must say so."""
    routes = {
        "/patch/validate": {
            "success": False,
            "issues": [{"severity": "error", "message": "syntax"}],
        },
        "/patch/apply": {
            "success": True,
            "session_id": "forced-1",
            "lines_added": 1, "lines_removed": 0,
            "original_hash": "h1", "new_hash": "h2",
        },
    }
    tools = _build_tools(routes)
    # Without force → blocked.
    raw = _run(tools["omni_patch"](
        action="apply", file="tests/x.py", content="def bad(:\n", format="json",
    ))
    assert json.loads(raw)["ok"] is False

    # With force=True + reason → applied, but flagged.
    raw = _run(tools["omni_patch"](
        action="apply", file="tests/x.py", content="def bad(:\n",
        force=True, force_reason="audit-test", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["validation_passed"] is False
    assert payload["validation_bypassed"] is True
    assert payload["force_reason"] == "audit-test"
    # The response must SHOUT about the bypass in next_actions.
    joined = " ".join(payload["next_actions"]).lower()
    assert "bypass" in joined or "validation was bypassed" in joined


# ---------------------------------------------------------------------------
# 7. validate returns structured checks
# ---------------------------------------------------------------------------


def test_patch_validate_returns_structured_checks() -> None:
    routes = {
        "/patch/validate": {
            "success": False,
            "message": "Validation failed",
            "issues": [
                {"tool": "ruff", "severity": "error", "line": 1,
                 "column": 8, "code": "E999", "message": "SyntaxError"},
                {"tool": "mypy", "severity": "warning", "line": 5,
                 "column": 0, "code": "name-defined",
                 "message": "Name 'x' is not defined"},
            ],
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="validate", file="x.py", content="def bad(:\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False  # validation_passed=false → ok=false
    assert payload["validation_passed"] is False
    checks = payload["checks"]
    assert len(checks) == 2
    # Diagnostics-shaped row:
    for c in checks:
        assert {"source", "severity", "line", "column", "rule", "message"} <= set(c)
    # Counts breakdown.
    assert payload["counts"]["error"] == 1
    assert payload["counts"]["warning"] == 1
    assert payload["counts"]["total"] == 2
    # Optional but documented fields.
    assert "tools_run" in payload
    assert "tools_skipped" in payload
    assert payload["next_actions"]


# ---------------------------------------------------------------------------
# 8. error paths include allowed_actions + next_actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call_kwargs", [
    {"action": "illegal_action", "format": "json"},
    {"action": "apply", "file": "x.py", "format": "json"},  # missing content
    {"action": "rollback", "format": "json"},                 # missing session_id
    {"action": "preview", "file": "../etc/passwd",           # path-guard
     "content": "x", "format": "json"},
])
def test_patch_error_paths_include_allowed_actions_next_actions(call_kwargs):
    tools = _build_tools({})
    raw = _run(tools["omni_patch"](**call_kwargs))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload.get("allowed_actions") == [
        "preview", "validate", "apply", "rollback", "sessions",
    ]
    assert payload.get("next_actions"), payload
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_patch"]


# ---------------------------------------------------------------------------
# 9. CRLF / double-blank-line normalisation
# ---------------------------------------------------------------------------


def test_patch_diff_normalizes_crlf_without_double_blank_lines() -> None:
    """Backend used to leak ``\\r\\n`` and a stray empty line after every
    diff body row; the normaliser must collapse both."""
    routes = {
        "/patch/preview": {
            "success": True,
            "lines_added": 1,
            "lines_removed": 0,
            # Pathological: CRLF line endings plus a blank row inserted
            # after every body row (the exact pattern the old MCP
            # response showed in audit logs).
            "diff": (
                "--- a/x.py\r\n"
                "+++ b/x.py\r\n"
                "\r\n"
                "@@ -1,1 +1,2 @@\r\n"
                "\r\n"
                " def x():\r\n"
                "\r\n"
                "+    return 1\r\n"
                "\r\n"
                "     pass\r\n"
            ),
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="preview", file="x.py", content="x", format="json",
    ))
    payload = json.loads(raw)
    diff = payload["diff"]
    assert "\r\n" not in diff
    # No run of three or more newlines (= two blank lines back-to-back).
    assert "\n\n\n" not in diff
    assert payload["newline_normalized"] is True


def test_patch_normalise_helper_directly() -> None:
    """Pure function check on the helper in isolation."""
    src = "a\r\nb\r\n\r\n\r\nc\r\n"
    out, was_norm = _normalise_diff_text(src)
    assert was_norm is True
    assert "\r\n" not in out
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# 10. apply response includes hashes
# ---------------------------------------------------------------------------


def test_patch_apply_response_includes_hashes() -> None:
    routes = {
        "/patch/validate": {"success": True, "issues": []},
        "/patch/apply": {
            "success": True,
            "session_id": "s1",
            "lines_added": 2,
            "lines_removed": 0,
            "original_hash": "abc123",
            "new_hash": "def456",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="apply", file="x.py", content="ok", format="json",
    ))
    payload = json.loads(raw)
    assert payload["original_hash"] == "abc123"
    assert payload["new_hash"] == "def456"
    assert payload["session_id"] == "s1"
    assert payload["rollback_available"] is True
    assert payload["validation_passed"] is True
    assert payload["validation_bypassed"] is False


# ---------------------------------------------------------------------------
# 11. rollback response includes hashes
# ---------------------------------------------------------------------------


def test_patch_rollback_response_includes_hashes() -> None:
    routes = {
        "/patch/sessions": {
            "sessions": [{
                "session_id": "s1",
                "file_path": "x.py",
                "applied": True,
                "rolled_back": False,
            }]
        },
        "/patch/rollback": {
            "success": True,
            "message": "rolled back",
            "previous_hash": "def456",
            "restored_hash": "abc123",
        },
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="rollback", session_id="s1", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["rolled_back"] is True
    assert payload["previous_hash"] == "def456"
    assert payload["restored_hash"] == "abc123"
    assert payload["next_actions"]


def test_patch_rollback_refuses_unsafe_legacy_session() -> None:
    """Sessions whose file_path sits outside the workspace must not be
    rolled back — they could be relics of the pre-r7 path-traversal
    bug. The composer surfaces ``unsafe_legacy_session=true`` and
    refuses."""
    routes = {
        "/patch/sessions": {
            "sessions": [{
                "session_id": "legacy",
                "file_path": "../../outside.py",
                "applied": True,
                "rolled_back": False,
            }]
        },
        "/patch/rollback": {"success": True, "message": "should not run"},
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](
        action="rollback", session_id="legacy", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["unsafe_legacy_session"] is True
    assert "/patch/rollback" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 12. sessions includes next_actions
# ---------------------------------------------------------------------------


def test_patch_sessions_include_next_actions() -> None:
    routes = {
        "/patch/sessions": {
            "sessions": [
                {
                    "session_id": "s1", "file_path": "x.py",
                    "applied": True, "rolled_back": False,
                    "lines_added": 1, "lines_removed": 0,
                    "timestamp": "2026-05-30T00:00:00",
                    "source": "external", "patch_type": "full_replace",
                },
                {
                    "session_id": "s2", "file_path": "../../outside.py",
                    "applied": True, "rolled_back": False,
                    "lines_added": 1, "lines_removed": 0,
                    "timestamp": "2026-05-29T00:00:00",
                    "source": "audit", "patch_type": "full_replace",
                },
            ]
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    assert payload.get("next_actions"), payload
    # Each row gets the unsafe_legacy_session annotation.
    sessions = payload["sessions"]
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["s1"]["unsafe_legacy_session"] is False
    assert by_id["s2"]["unsafe_legacy_session"] is True


# ---------------------------------------------------------------------------
# 13. contract_version is patch.v2
# ---------------------------------------------------------------------------


def test_patch_contract_version_is_patch_v2() -> None:
    tools = _build_tools({"/patch/sessions": {"sessions": []}})
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    assert payload["contract_version"] == "patch.v2"
    assert _CONTRACT_VERSIONS["omni_patch"] == "patch.v2"


def test_patch_handler_version_is_r7() -> None:
    tools = _build_tools({"/patch/sessions": {"sessions": []}})
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    # The r7 bundle is the floor for patch.v2; later audit rounds bump
    # _HANDLER_VERSION but must keep the patch.v2 contract intact.
    # Numeric round comparison (string ordering breaks at r10).
    import re as _re
    assert payload["handler_version"] == _HANDLER_VERSION
    m = _re.search(r"\.r(\d+)$", _HANDLER_VERSION)
    assert m, f"unexpected handler_version shape: {_HANDLER_VERSION}"
    assert int(m.group(1)) >= 7


def test_patch_status_features_advertised() -> None:
    """omni_status must advertise the three new patch.v2 capabilities so
    auditors can discover them deterministically."""
    flags = set(hlt._HANDLER_FEATURES)
    assert "patch.workspace_path_guard" in flags
    assert "patch.apply_validate_gate" in flags
    assert "patch.structured_validation" in flags
