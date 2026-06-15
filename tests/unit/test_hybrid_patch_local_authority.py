"""Hybrid omni_patch local-authority contract tests.

In hybrid mode the configured backend URL points at the cloud mirror. Patch
operations must never use that cloud backend for writes; they are local
authority actions over the explicit MCP workspace root.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from tests.unit.mcp_harness import build_tools, run


def _payload(raw: str) -> Dict[str, Any]:
    return json.loads(raw)


@pytest.fixture(autouse=True)
def _hybrid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.delenv("OMNICODE_FASTAPI_BASE_URL", raising=False)
    monkeypatch.delenv("OMNICODE_BACKEND_URL", raising=False)


def test_hybrid_patch_apply_and_rollback_stay_local(tmp_path: Path) -> None:
    rel = "tests/tmp_hybrid_patch_local.py"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('VALUE = "v1"\n', encoding="utf-8")

    tools = build_tools({})
    content_v2 = 'VALUE = "v2"\n'

    preview = _payload(run(tools["omni_patch"](
        action="preview",
        file=rel,
        content=content_v2,
        format="json",
    )))
    assert preview["ok"] is True
    assert preview["source"] == "local"
    assert preview["local_authority"] is True

    validate = _payload(run(tools["omni_patch"](
        action="validate",
        file=rel,
        content=content_v2,
        format="json",
    )))
    assert validate["ok"] is True
    assert validate["source"] == "local"
    assert validate["local_authority"] is True

    applied = _payload(run(tools["omni_patch"](
        action="apply",
        file=rel,
        content=content_v2,
        format="json",
    )))
    assert applied["ok"] is True
    assert applied["source"] == "local"
    assert applied["local_authority"] is True
    assert applied["rollback_available"] is True
    assert applied["session_id"]
    assert applied["sync_pending"] is True
    assert target.read_text(encoding="utf-8") == content_v2

    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/patch/preview" not in captured
    assert "/patch/validate" not in captured
    assert "/patch/apply" not in captured

    sessions = _payload(run(tools["omni_patch"](
        action="sessions",
        format="json",
    )))
    assert sessions["ok"] is True
    assert sessions["source"] == "local"
    assert sessions["local_authority"] is True
    assert any(
        row.get("session_id") == applied["session_id"]
        for row in sessions["sessions"]
    )
    assert "/patch/sessions" not in captured

    rolled = _payload(run(tools["omni_patch"](
        action="rollback",
        session_id=applied["session_id"],
        format="json",
    )))
    assert rolled["ok"] is True
    assert rolled["source"] == "local"
    assert rolled["local_authority"] is True
    assert rolled["rolled_back"] is True
    assert rolled["sync_pending"] is True
    assert target.read_text(encoding="utf-8") == 'VALUE = "v1"\n'
    assert "/patch/rollback" not in captured


def test_hybrid_patch_new_file_rollback_unlinks_local_stub(
    tmp_path: Path,
) -> None:
    rel = "tests/tmp_hybrid_patch_new.py"
    target = tmp_path / rel
    tools = build_tools({})

    applied = _payload(run(tools["omni_patch"](
        action="apply",
        file=rel,
        content='VALUE = "new"\n',
        format="json",
    )))
    assert applied["ok"] is True
    assert target.exists()

    rolled = _payload(run(tools["omni_patch"](
        action="rollback",
        session_id=applied["session_id"],
        format="json",
    )))
    assert rolled["ok"] is True
    assert rolled["new_file_unlinked"] is True
    assert not target.exists()

    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/patch/apply" not in captured
    assert "/patch/rollback" not in captured


def test_hybrid_patch_apply_flushes_pending_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnicode_core.workspace import sync_client as sync_client_mod

    calls: List[Dict[str, Any]] = []

    class FakeSyncClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append({"init": kwargs})

        def push_batch(self, batch: Any) -> Any:
            calls.append({"paths": sorted(batch.paths)})
            return SimpleNamespace(
                ok=True,
                accepted_revision=41,
                indexed_revision=41,
                error=None,
                status_code=200,
            )

        def close(self) -> None:
            calls.append({"closed": True})

    monkeypatch.setenv("OMNICODE_REMOTE", "http://127.0.0.1:6791")
    monkeypatch.setattr(sync_client_mod, "SyncClient", FakeSyncClient)

    rel = "tests/tmp_hybrid_patch_flush.py"
    tools = build_tools({})

    applied = _payload(run(tools["omni_patch"](
        action="apply",
        file=rel,
        content='VALUE = "flush"\n',
        format="json",
    )))

    assert applied["ok"] is True
    assert applied["source"] == "local"
    assert applied["sync_pending"] is True
    assert applied["sync_flushed"] is True
    assert applied["accepted_revision"] == 41
    assert applied["indexed_revision"] == 41
    assert applied["sync_paths"] == [rel]
    assert any(call.get("paths") == [rel] for call in calls)


def test_hybrid_patch_apply_succeeds_when_cloud_sync_is_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnicode_core.workspace import sync_client as sync_client_mod

    class FakeSyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def push_batch(self, batch: Any) -> Any:
            return SimpleNamespace(
                ok=False,
                accepted_revision=0,
                indexed_revision=0,
                error="cloud down",
                status_code=503,
            )

        def close(self) -> None:
            return None

    monkeypatch.setenv("OMNICODE_REMOTE", "http://127.0.0.1:6799")
    monkeypatch.setattr(sync_client_mod, "SyncClient", FakeSyncClient)

    rel = "tests/tmp_hybrid_patch_cloud_down.py"
    target = tmp_path / rel
    tools = build_tools({})

    applied = _payload(run(tools["omni_patch"](
        action="apply",
        file=rel,
        content='VALUE = "local-still-works"\n',
        format="json",
    )))

    assert applied["ok"] is True
    assert applied["source"] == "local"
    assert applied["local_authority"] is True
    assert applied["sync_pending"] is True
    assert applied["sync_flushed"] is False
    assert applied["sync_flush_error"] == "cloud down"
    assert applied["sync_flush_status_code"] == 503
    assert target.read_text(encoding="utf-8") == 'VALUE = "local-still-works"\n'
    captured: Dict[str, List[Dict[str, Any]]] = tools["__captured__"]
    assert "/patch/apply" not in captured
