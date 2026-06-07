"""Contract tests for the workspace root alignment fix (audit-bundle.r11).

P1-B from r10 live verification: ``_resolve_workspace_path`` and the
new file_exists/new_file markers used ``Path.cwd()``, which silently
disagrees with the backend's ``Settings.WORKING_DIR`` when the MCP
host is launched from a different cwd.

This file pins the post-fix behaviour:

* ``_get_workspace_root()`` prefers (in order) workspace registry →
  ``Settings.WORKING_DIR`` → ``Path.cwd()`` fallback.
* Falling back to cwd surfaces ``workspace_root_fallback_to_cwd`` in the
  warnings list.
* ``omni_status`` reports ``workspace_root``, ``workspace_root_source``,
  ``cwd``, ``workspace_root_matches_cwd``, and the warnings.
* ``_resolve_workspace_path`` defaults to the aligned root, not cwd.
* The path guard's traversal/absolute checks still fire, with no leaks
  of ``file_exists``/``new_file`` on rejection.
* file_exists/new_file markers stat against the aligned root so a real
  workspace file is reported as ``file_exists=True``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _HANDLER_VERSION,
    _get_workspace_root,
    _resolve_workspace_path,
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
# 1. _get_workspace_root() helper
# ---------------------------------------------------------------------------


def test_workspace_root_helper_returns_three_tuple() -> None:
    root, source, warnings = _get_workspace_root()
    assert isinstance(root, Path)
    assert root.is_dir(), f"helper returned non-existent dir: {root}"
    assert source in (
        "explicit_local_workspace", "workspace_registry",
        "settings_working_dir", "cwd_fallback",
    )
    assert isinstance(warnings, list)


def test_workspace_root_helper_prefers_explicit_local_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_root = tmp_path / "local"
    cloud_root = tmp_path / "cloud"
    local_root.mkdir()
    cloud_root.mkdir()

    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(local_root))
    monkeypatch.setattr(
        "omnicode_core.workspace.registry._default_store_path",
        lambda: tmp_path / "workspaces.json",
    )
    monkeypatch.setattr(
        "omnicode_core.workspace.registry._DEFAULT_REGISTRY", None,
    )
    from omnicode_core.workspace import get_workspace_registry

    get_workspace_registry().add(
        name="repo-a",
        path=str(cloud_root),
        set_active=True,
        workspace_id="repo-a",
    )

    root, source, warnings = _get_workspace_root()
    assert root == local_root.resolve()
    assert source == "explicit_local_workspace"
    assert "workspace_root_fallback_to_cwd" not in warnings


def test_workspace_root_helper_prefers_settings_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Settings.WORKING_DIR points somewhere other than cwd, the
    helper must use it and NOT fall back to cwd."""
    # Make sure the registry has no active workspace so we exercise the
    # settings path.
    fake_registry_path = tmp_path / "no_workspaces.json"
    monkeypatch.setattr(
        "omnicode_core.workspace.registry._default_store_path",
        lambda: fake_registry_path,
    )
    # Reset the lazy registry singleton so our patch is picked up.
    monkeypatch.setattr(
        "omnicode_core.workspace.registry._DEFAULT_REGISTRY", None,
    )
    # Also clear lru_cache on get_settings and stub WORKING_DIR.
    from omnicode.config import settings as _settings_mod
    _settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("WORKING_DIR", str(tmp_path))

    root, source, warnings = _get_workspace_root()
    assert root == tmp_path.resolve()
    assert source == "settings_working_dir"
    assert "workspace_root_fallback_to_cwd" not in warnings


def test_workspace_root_fallback_warns_when_using_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both registry and settings are unusable the helper falls back
    to ``Path.cwd()`` and emits ``workspace_root_fallback_to_cwd`` so
    omni_status can surface it as a warning."""
    # Force the registry import to fail.
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "omnicode_core.workspace", None)
    # And the settings import too.
    monkeypatch.setitem(_sys.modules, "omnicode.config.settings", None)

    root, source, warnings = _get_workspace_root()
    assert source == "cwd_fallback"
    assert "workspace_root_fallback_to_cwd" in warnings
    assert root == Path.cwd().resolve()


# ---------------------------------------------------------------------------
# 2. omni_status reports workspace_root + cwd
# ---------------------------------------------------------------------------


def test_omni_status_reports_workspace_root_and_cwd() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    # New r11 fields must all be present.
    assert "workspace_root" in payload
    assert "workspace_root_source" in payload
    assert "cwd" in payload
    assert "workspace_root_matches_cwd" in payload
    assert isinstance(payload["workspace_root_matches_cwd"], bool)
    assert payload["workspace_root_source"] in (
        "explicit_local_workspace", "workspace_registry",
        "settings_working_dir", "cwd_fallback",
    )
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == "status.v1"


def test_omni_status_handler_features_advertise_alignment() -> None:
    flags = set(hlt._HANDLER_FEATURES)
    assert "workspace.root_alignment" in flags
    assert "patch.workspace_root_aligned" in flags


# ---------------------------------------------------------------------------
# 3. _resolve_workspace_path uses the aligned root by default
# ---------------------------------------------------------------------------


def test_resolve_workspace_path_uses_aligned_root_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When called without an explicit workspace_root, the helper must
    use ``_get_workspace_root()`` rather than ``Path.cwd()``. We pin
    ``_get_workspace_root`` to a tmp dir and verify resolution lands
    inside it even when cwd is different."""
    target_root = tmp_path / "fake_workspace"
    target_root.mkdir()
    (target_root / "tests").mkdir()
    (target_root / "tests" / "fixture.py").write_text("# x\n")

    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )

    resolved = _resolve_workspace_path("tests/fixture.py")
    assert resolved == (target_root / "tests" / "fixture.py").resolve()


# ---------------------------------------------------------------------------
# 4. file_exists / new_file markers use the aligned root
# ---------------------------------------------------------------------------


def test_patch_existing_file_markers_align_with_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker set on validate must reflect file presence under the
    *aligned* root, not cwd."""
    target_root = tmp_path / "wsroot"
    target_root.mkdir()
    (target_root / "tests").mkdir()
    (target_root / "tests" / "real.py").write_text("def f(): pass\n")

    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )

    tools = _build_tools({
        "/read": {
            "success": True, "content": "def f(): pass\n",
            "language": "python", "total_lines": 1, "symbols": [],
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/real.py",
        content="def f(): pass\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["file_exists"] is True
    assert payload["new_file"] is False


def test_patch_new_file_markers_use_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "wsroot2"
    target_root.mkdir()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )

    tools = _build_tools({
        "/read": {
            "success": False, "error": "File not found: tests/never_existed.py",
        },
        "/patch/validate": {
            "success": True, "message": "ok", "issues": [],
        },
    })
    raw = _run(tools["omni_patch"](
        action="validate", file="tests/never_existed.py",
        content="print('x')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["file_exists"] is False
    assert payload["new_file"] is True


# ---------------------------------------------------------------------------
# 5. path guard still wins on traversal / absolute, no marker leak
# ---------------------------------------------------------------------------


def test_patch_path_guard_uses_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "wsroot3"
    target_root.mkdir()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )
    tools = _build_tools({})
    raw = _run(tools["omni_patch"](
        action="apply", file="../../outside.py",
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "path-guard" in payload["error"].lower()
    # Markers must NOT leak on a guard rejection.
    assert "file_exists" not in payload
    assert "new_file" not in payload
    # Backend never called.
    assert "/patch/apply" not in tools["__captured__"]


def test_omni_edit_alias_uses_workspace_root_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "wsroot4"
    target_root.mkdir()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )
    tools = _build_tools({})
    raw = _run(tools["omni_edit"](
        action="apply", file="../../outside.py",
        content="print('bad')\n", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["deprecated"] is True
    assert payload["alias"] == "omni_edit"
    assert payload["replacement"] == "omni_patch"
    assert "path-guard" in payload["error"].lower()
    assert "/patch/apply" not in tools["__captured__"]


# ---------------------------------------------------------------------------
# 6. sessions unsafe_legacy annotation uses the aligned root
# ---------------------------------------------------------------------------


def test_sessions_unsafe_legacy_annotation_uses_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_root = tmp_path / "wsroot5"
    target_root.mkdir()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (target_root.resolve(), "workspace_registry", []),
    )
    tools = _build_tools({
        "/patch/sessions": {
            "sessions": [
                {
                    "session_id": "safe-1",
                    "file_path": "tests/inside.py",
                    "lines_added": 1, "lines_removed": 0,
                },
                {
                    "session_id": "unsafe-1",
                    "file_path": "../../outside.py",
                    "lines_added": 1, "lines_removed": 0,
                },
            ],
        },
    })
    raw = _run(tools["omni_patch"](action="sessions", format="json"))
    payload = json.loads(raw)
    assert payload["ok"] is True
    by_id = {s["session_id"]: s for s in payload["sessions"]}
    assert by_id["safe-1"]["unsafe_legacy_session"] is False
    assert by_id["unsafe-1"]["unsafe_legacy_session"] is True


# ---------------------------------------------------------------------------
# 7. when settings == cwd, workspace_root_matches_cwd is True
# ---------------------------------------------------------------------------


def test_omni_status_matches_cwd_when_settings_is_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the common case where the host was launched from the project
    root, omni_status should report ``workspace_root_matches_cwd=True``."""
    cwd = Path.cwd().resolve()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (cwd, "settings_working_dir", []),
    )
    tools = _build_tools({})
    raw = _run(tools["omni_status"]())
    payload = json.loads(raw)
    assert payload["workspace_root_matches_cwd"] is True
    assert "workspace_root_fallback_to_cwd" not in payload["warnings"]
