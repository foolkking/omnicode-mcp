"""Contract tests for the audit-bundle.r12 backend file-marker probe.

r10 introduced ``file_exists`` / ``new_file`` markers via a local
``Path.stat()`` against the MCP host's workspace root. r11 made the host
root deterministic (``Settings.WORKING_DIR`` -> registry -> cwd) but
live verification proved the host root and the *backend* root can still
diverge when the FastAPI backend runs from a different process CWD —
the local stat then lies about file existence relative to where apply
will actually write.

r12 fixes this by asking the backend itself via a ``/read`` probe. This
file pins the new contract:

* preview / validate / apply markers use the backend probe, not local stat
* the backend "exists" path produces ``file_exists=True new_file=False``
* the backend "not found" path produces ``file_exists=False new_file=True``
* a probe failure produces ``file_exists=null`` AND
  ``file_marker_authoritative=False`` AND a ``file_marker_warning``
* the path guard runs BEFORE the probe — traversal/absolute paths never
  reach the backend probe at all
* the omni_edit alias inherits the same guard
* status surfaces ``backend_workspace_root`` / ``workspace_root_matches_backend``
* feature flags advertise ``patch.backend_file_markers`` +
  ``workspace.backend_root_visibility``
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    register_high_level_tools,
)


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
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


# ---------------------------------------------------------------------------
# 1. backend probe is the source of truth — local stat is NOT consulted
# ---------------------------------------------------------------------------


def test_patch_file_markers_use_backend_probe_not_local_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the local filesystem disagrees with the backend, the markers
    must follow the backend. Pin the host workspace root somewhere the
    file does NOT exist locally, but tell the scripted /read probe the
    file DOES exist on the backend side. The marker must be True."""
    host_root = tmp_path / "host_only"
    host_root.mkdir()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (host_root.resolve(), "workspace_registry", []),
    )

    # File does not exist locally under host_root (the empty dir we just
    # created), but the backend says it exists.
    tools = _build_tools({
        "/read": {
            "success": True,
            "content": "# placeholder\n",
            "language": "python",
            "total_lines": 1,
            "symbols": [],
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/conflict.py",
        content="# placeholder\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is True
    assert payload["new_file"] is False
    assert payload["file_marker_authoritative"] is True
    assert payload["file_marker_source"] == "backend_read_probe"
    # Backend probe was actually called.
    assert "/read" in tools["__captured__"]


# ---------------------------------------------------------------------------
# 2. existing-file marker matches what omni_read sees
# ---------------------------------------------------------------------------


def test_patch_existing_file_marker_matches_omni_read() -> None:
    """When the backend /read returns success=True for the same path,
    the patch marker must agree."""
    tools = _build_tools({
        "/read": {
            "success": True,
            "content": "x = 1\n",
            "language": "python",
            "total_lines": 1,
            "symbols": [],
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    # Sanity: omni_read confirms file exists.
    read_raw = _run(tools["omni_read"](
        file="tests/existing.py", mode="full", format="json",
    ))
    read_payload = json.loads(read_raw)
    assert read_payload["ok"] is True

    # And patch validate agrees.
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/existing.py",
        content="x = 1\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is True
    assert payload["new_file"] is False
    assert payload["file_marker_authoritative"] is True


# ---------------------------------------------------------------------------
# 3. new-file marker matches backend "not found"
# ---------------------------------------------------------------------------


def test_patch_new_file_marker_matches_backend_not_found() -> None:
    tools = _build_tools({
        "/read": {
            "success": False,
            "error": "File not found: tests/brand_new.py",
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/brand_new.py",
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is False
    assert payload["new_file"] is True
    assert payload["file_marker_authoritative"] is True
    assert payload["file_marker_source"] == "backend_read_probe"


# ---------------------------------------------------------------------------
# 4. probe failure → not authoritative + warning surfaced
# ---------------------------------------------------------------------------


def test_patch_marker_probe_failure_is_not_authoritative() -> None:
    """When the backend returns an unrelated error (not a file_exists
    signal), the markers must be ``null`` and the response must carry a
    warning explaining why."""
    tools = _build_tools({
        # Backend returns success=False but the message is not a
        # not-found signal.
        "/read": {
            "success": False, "error": "internal server error",
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/x.py",
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is None
    assert payload["new_file"] is None
    assert payload["file_marker_authoritative"] is False
    assert payload.get("file_marker_warning")


def test_patch_marker_probe_exception_is_not_authoritative() -> None:
    """When /read raises, the markers come back null + warning."""

    async def boom(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        if endpoint == "/read":
            raise RuntimeError("backend offline")
        # Other endpoints return a benign success.
        return {"result": {"success": True, "message": "ok", "issues": []}}

    mcp = _MCPStub()
    register_high_level_tools(mcp, boom)
    tools = mcp.tools
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/x.py",
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is None
    assert payload["new_file"] is None
    assert payload["file_marker_authoritative"] is False
    assert "backend probe failed" in (payload.get("file_marker_warning") or "")


# ---------------------------------------------------------------------------
# 5. omni_status surfaces backend root visibility
# ---------------------------------------------------------------------------


def test_status_reports_backend_workspace_root_when_available() -> None:
    """When the backend probe returns a workspace_root or a resolved
    path, omni_status should surface it."""
    tools = _build_tools({
        "/read": {
            "success": True,
            "content": "# README\n",
            "language": "markdown",
            "total_lines": 1,
            "symbols": [],
            "workspace_root": "C:/some/backend/root",
        },
    })
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    assert "backend_workspace_root" in payload
    assert payload["backend_workspace_root"] == "C:/some/backend/root"
    assert payload["backend_workspace_root_source"] == "backend_response"
    assert "workspace_root_matches_backend" in payload


def test_status_reports_backend_root_unknown_when_unavailable() -> None:
    """When the backend probe doesn't expose root info, the status fields
    are null AND a workspace_root_warning explains the gap."""
    tools = _build_tools({
        "/read": {
            "success": True,
            "content": "# README\n",
            "language": "markdown",
            "total_lines": 1,
            # Note: no workspace_root, no resolved_file_path.
        },
    })
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    assert payload["backend_workspace_root"] is None
    assert payload["workspace_root_matches_backend"] is None
    assert payload.get("workspace_root_warning")


def test_status_handler_features_advertise_r12_flags() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "patch.backend_file_markers" in flags
    assert "workspace.backend_root_visibility" in flags


# ---------------------------------------------------------------------------
# 6. path guard still runs BEFORE the probe — no /read call on traversal
# ---------------------------------------------------------------------------


def test_path_guard_still_rejects_traversal_before_marker_probe() -> None:
    tools = _build_tools({
        "/read": {
            "success": True,  # Wouldn't matter — should never be called.
            "content": "x", "language": "python",
            "total_lines": 1, "symbols": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="apply", file="../../outside.py",
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "path-guard" in payload["error"].lower()
    # Probe was NEVER called.
    assert "/read" not in tools["__captured__"]
    # Markers must NOT be in the guard rejection envelope.
    assert "file_exists" not in payload
    assert "new_file" not in payload
    assert "file_marker_source" not in payload


def test_omni_edit_alias_still_rejects_traversal() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_edit"](
        action="apply", file="../../outside.py",
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["deprecated"] is True
    assert payload["replacement"] == "omni_patch"
    assert "path-guard" in payload["error"].lower()
    assert "/read" not in tools["__captured__"]
    assert "/patch/apply" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 7. Option B passthrough — explicit backend file_exists wins over probe heuristic
# ---------------------------------------------------------------------------


def test_option_b_passthrough_when_backend_returns_explicit_file_exists() -> None:
    """A future backend may return an explicit ``file_exists`` field on
    the read response (or the patch response). When it does, we must
    prefer that over the success-flag heuristic."""
    tools = _build_tools({
        "/read": {
            # success=False would normally suggest "not found", but the
            # backend tells us explicitly the file exists.
            "success": False,
            "error": "permission denied",
            "file_exists": True,
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/locked.py",
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is True
    assert payload["new_file"] is False
    assert payload["file_marker_source"] == "backend_patch_response"
    assert payload["file_marker_authoritative"] is True
