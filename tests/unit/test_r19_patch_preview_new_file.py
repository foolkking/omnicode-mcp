"""Contract tests for audit-bundle.r19 (patch.preview_new_file_ok).

P0 from the final review: omni_patch preview on a nonexistent file
returned ``ok=False`` with ``error="Preview failed: File not found"``
even when ``new_file=True`` and ``file_marker_authoritative=True``. An
AI editor following the documented safe-edit pipeline (preview →
validate → apply) reads ``ok=False`` and aborts, so the canonical new-
file creation flow could not start.

r19 flips the contract for the new-file branch only:

* probe-authoritative ``new_file=True`` → preview returns ``ok=True``
  with ``preview_synthesized=True``, a synthesized unified-diff
  (every content line as an addition), accurate ``lines_added`` /
  ``lines_removed=0``, and the same validate/apply ``next_actions``.
* probe-authoritative ``new_file=False`` (existing file) preview
  failures STILL return ``ok=False`` — only the new-file branch
  changes.
* path guard, force-reason gate, and apply validate gate are
  untouched.

Tests pinned by this round:

1. new-file preview returns ok=True + synthesized diff + correct line
   counts + new_file/file_exists markers + validate/apply next_actions
2. existing-file preview failure (e.g. backend returns success=False
   for an existing file due to backend-internal error) still returns
   ok=False with the original error message
3. content with multiple lines is fully reflected in the synthesized
   diff (lines_added == content lines, every line prefixed with '+')
4. empty content on a new file still returns ok=True with
   lines_added=0 (no spurious failure)
5. path traversal on a "new file" path is rejected BEFORE any backend
   probe runs (no leak of the new-file logic past the guard)
6. _HANDLER_VERSION is r19+ and patch.preview_new_file_ok is in
   _HANDLER_FEATURES
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    _HANDLER_FEATURES,
    _HANDLER_VERSION,
)
from tests.unit.mcp_harness import (
    build_tools_with_route_keys as _build_tools,
)
from tests.unit.mcp_harness import (
    run as _run,
)

_NEW_FILE_RELPATH = "tests/tmp_r19_new_file.py"
_EXISTING_FILE_RELPATH = "tests/tmp_r19_existing.py"


def _read_route_for_missing() -> dict[str, Any]:
    """Scripted /read response that means 'file does not exist'."""
    return {
        "success": False,
        "error": "File not found: x.py",
    }


def _read_route_for_existing() -> dict[str, Any]:
    """Scripted /read response that means 'file exists with content'."""
    return {
        "success": True,
        "content": "old\n",
        "language": "python",
        "total_lines": 1,
        "symbols": [],
        "file_path": "/abs/path/to/file.py",
        "workspace_root": "/abs/path/to",
    }


@pytest.fixture(autouse=True)
def _pin_workspace_root_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin _get_workspace_root() to the test cwd so path-guard checks
    stay deterministic regardless of where pytest is invoked from.

    The helper returns a 3-tuple ``(path, source, registry_entries)``;
    the patch tool unpacks it via ``[0]`` to get the resolved root."""
    cwd = Path.cwd().resolve()
    monkeypatch.setattr(
        hlt, "_get_workspace_root",
        lambda: (cwd, "test_pinned", []),
    )


# ---------------------------------------------------------------------------
# 1. happy path — new-file preview returns ok=True with synthesized diff
# ---------------------------------------------------------------------------


def test_new_file_preview_returns_ok_true_with_synthesized_diff() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    content = "print('x')\n"
    raw = _run(tools["omni_patch"](
        action="preview",
        file=_NEW_FILE_RELPATH,
        content=content,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True, (
        "r19: new-file preview must NOT return ok=False"
    )
    assert payload["action"] == "preview"
    assert payload["file"] == _NEW_FILE_RELPATH
    assert payload["new_file"] is True
    assert payload["file_exists"] is False
    assert payload["file_marker_authoritative"] is True
    assert payload["preview_synthesized"] is True
    assert "preview_synthesized_reason" in payload
    assert "new_file" in payload["preview_synthesized_reason"].lower()
    # Standard preview shape is preserved.
    assert payload["lines_added"] == 1
    assert payload["lines_removed"] == 0
    assert payload["newline_normalized"] is False
    assert "diff" in payload
    assert "/dev/null" in payload["diff"]
    assert f"+++ b/{_NEW_FILE_RELPATH}" in payload["diff"]
    assert "+print('x')" in payload["diff"]
    # Stamps and contract version still present.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == "patch.v2"


def test_new_file_preview_recommends_validate_then_apply() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview",
        file=_NEW_FILE_RELPATH,
        content="def f():\n    return 1\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    actions = payload.get("next_actions") or []
    assert any("validate" in a for a in actions), (
        "new-file preview must recommend validate"
    )
    assert any("apply" in a for a in actions), (
        "new-file preview must recommend apply"
    )
    # Should NOT recommend rollback yet (no apply has happened).
    assert not any("rollback" in a.lower() for a in actions), (
        "rollback is only meaningful AFTER apply"
    )


# ---------------------------------------------------------------------------
# 2. multi-line content — every line surfaces as a + addition
# ---------------------------------------------------------------------------


def test_new_file_preview_multi_line_content_counts_match() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    content = "def add(a, b):\n    \"\"\"Return the sum.\"\"\"\n    return a + b\n"
    raw = _run(tools["omni_patch"](
        action="preview",
        file=_NEW_FILE_RELPATH,
        content=content,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    expected_lines = content.splitlines()
    assert payload["lines_added"] == len(expected_lines)
    assert payload["lines_removed"] == 0
    # Header has 3 lines (--- /dev/null, +++ b/<file>, @@ ...).
    expected_total = 3 + len(expected_lines)
    assert payload["diff_total_lines"] == expected_total
    # Every content line appears with a leading '+'.
    for line in expected_lines:
        assert f"+{line}" in payload["diff"], (
            f"missing addition for line: {line!r}"
        )
    # Hunk header reflects the line count.
    assert f"@@ -0,0 +1,{len(expected_lines)} @@" in payload["diff"]


def test_new_file_preview_empty_content_is_ok() -> None:
    """Empty content on a new file is unusual but should not regress to
    ok=False. lines_added == 0; diff has just the unified header."""
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview",
        file=_NEW_FILE_RELPATH,
        content="",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["new_file"] is True
    assert payload["lines_added"] == 0
    assert payload["lines_removed"] == 0
    assert payload["preview_synthesized"] is True


# ---------------------------------------------------------------------------
# 3. existing-file preview failure is UNCHANGED
# ---------------------------------------------------------------------------


def test_existing_file_preview_failure_still_returns_ok_false() -> None:
    """r19 narrowly flips ok=False→True for the new-file probe-
    authoritative branch only. An existing-file backend failure must
    still surface ok=False with the original error message — we don't
    want callers to mistake a backend hiccup for a successful preview.
    """
    tools = _build_tools({
        "/read": _read_route_for_existing(),
        "/patch/preview": {
            "success": False,
            "message": "Backend internal error: diff renderer failed",
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview",
        file=_EXISTING_FILE_RELPATH,
        content="# new content\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False, (
        "existing-file preview failures must remain ok=False"
    )
    assert "error" in payload
    assert "diff renderer failed" in payload["error"]
    # Markers reflect that the file already exists.
    assert payload["file_exists"] is True
    assert payload["new_file"] is False
    assert payload.get("preview_synthesized") in (None, False)


# ---------------------------------------------------------------------------
# 4. path guard still gates new-file paths
# ---------------------------------------------------------------------------


def test_new_file_preview_traversal_path_rejected_before_probe() -> None:
    """Path guard runs before the new-file logic. ``../escape.py`` must
    be rejected synchronously and the backend probe must never fire."""
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    raw = _run(tools["omni_patch"](
        action="preview",
        file="../escape_via_traversal.py",
        content="print('boom')\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    err = payload.get("error", "").lower()
    assert "traversal" in err or ".." in err, (
        f"expected traversal rejection, got: {err!r}"
    )
    # Backend was NOT consulted for either probe or preview.
    captured = tools["__captured__"]
    assert "/patch/preview" not in captured
    # The /read probe is also fenced — guard rejects before any I/O.
    assert "/read" not in captured


def test_new_file_preview_absolute_path_rejected_before_probe() -> None:
    tools = _build_tools({
        "/read": _read_route_for_missing(),
        "/patch/preview": {
            "success": False, "message": "File does not exist",
        },
    })
    abs_path = str(Path.cwd().resolve() / "tests" / "tmp_r19_abs.py")
    raw = _run(tools["omni_patch"](
        action="preview",
        file=abs_path,
        content="print('boom')\n",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    err = payload.get("error", "").lower()
    assert "absolute" in err
    captured = tools["__captured__"]
    assert "/patch/preview" not in captured


# ---------------------------------------------------------------------------
# 5. handler version + feature flag
# ---------------------------------------------------------------------------


def test_handler_version_is_r19_or_later() -> None:
    """Lexicographic comparison breaks past r10 ('r10' < 'r6' as
    strings), so extract the integer suffix and compare numerically."""
    m = re.search(r"\.r(\d+)$", _HANDLER_VERSION)
    assert m is not None, f"unexpected handler version: {_HANDLER_VERSION!r}"
    assert int(m.group(1)) >= 19, (
        f"_HANDLER_VERSION must be at least r19, got {_HANDLER_VERSION}"
    )


def test_patch_preview_new_file_ok_flag_present() -> None:
    assert "patch.preview_new_file_ok" in _HANDLER_FEATURES, (
        "r19 feature flag missing from _HANDLER_FEATURES"
    )
