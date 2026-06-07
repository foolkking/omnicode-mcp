"""Focused E2E test for local manifest -> cloud sync -> router barrier."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.v1.routers.sync as sync_router
from omnicode_core.workspace.local import LocalWorkspace
from omnicode_core.workspace.manifest import LocalManifest
from omnicode_core.workspace.registry import WorkspaceRegistry
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore
from omnicode_core.workspace.sync_queue import SyncQueue
from omnicode_core.workspace.tool_router import HybridToolRouter, SyncRevisionState


def _wait_indexed(
    client: TestClient,
    *,
    workspace_id: str,
    revision: int,
    attempts: int = 200,
) -> dict:
    last_status: dict = {}
    for _ in range(attempts):
        last_status = client.get(
            "/sync/status",
            headers={"X-Omnicode-Workspace": workspace_id},
        ).json()
        if int(last_status.get("indexed_revision") or 0) >= revision:
            return last_status
        time.sleep(0.01)
    raise AssertionError(
        f"indexed_revision did not reach {revision}; last_status={last_status}"
    )


def test_local_manifest_syncs_to_cloud_snapshot_and_unblocks_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace_root),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        sync_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace_root)),
    )
    monkeypatch.setattr(sync_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(
        sync_router,
        "_SNAPSHOT_STORE",
        CloudSnapshotStore(root=tmp_path / "cloud-sync"),
    )
    sync_router._SYNC_STATES.clear()

    app = FastAPI()
    app.include_router(sync_router.router)

    workspace = LocalWorkspace(root=workspace_root, workspace_id="repo-a")
    manifest = LocalManifest.load(
        workspace=workspace,
        path=tmp_path / "manifest.json",
    )
    change = manifest.mark_changed("src/app.py")
    assert change is not None

    queue = SyncQueue(manifest)
    batch = queue.next_batch()
    assert batch is not None

    with TestClient(app) as client:
        pushed = client.post(
            "/sync/batch",
            headers={"X-Omnicode-Workspace": "repo-a"},
            json=batch.to_payload(),
        ).json()
        assert pushed["ok"] is True
        assert pushed["accepted_revision"] == manifest.local_revision

        queue.mark_accepted(
            batch,
            accepted_revision=pushed["accepted_revision"],
            indexed_revision=pushed["indexed_revision"],
        )
        assert manifest.pending == []

        status = _wait_indexed(
            client,
            workspace_id="repo-a",
            revision=manifest.local_revision,
        )
        assert status["accepted_revision"] == manifest.local_revision
        assert status["indexed_revision"] == manifest.local_revision
        assert status["indexed_files"] == 1

        barrier = client.post(
            "/sync/barrier",
            headers={"X-Omnicode-Workspace": "repo-a"},
            json={
                "min_revision": manifest.local_revision,
                "paths": ["src/app.py"],
                "wait_ms": 0,
            },
        ).json()
        assert barrier["ok"] is True
        assert barrier["ready"] is True

    route = HybridToolRouter(executor="hybrid").route(
        "omni_search",
        sync_state=SyncRevisionState(
            local_revision=manifest.local_revision,
            accepted_revision=status["accepted_revision"],
            indexed_revision=status["indexed_revision"],
            cloud_available=True,
        ),
    )
    assert route.target == "cloud"
    assert route.barrier_min_revision == manifest.local_revision
