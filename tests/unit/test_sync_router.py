"""Tests for the cloud-side /sync endpoints."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Generator, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.v1.routers.sync as sync_router
from omnicode_core.workspace.registry import WorkspaceRegistry
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _wait_indexed(
    client: TestClient,
    *,
    workspace_id: str = "repo-a",
    revision: int,
    attempts: int = 200,
) -> dict:
    for _ in range(attempts):
        status = client.get(
            "/sync/status",
            headers={"X-Omnicode-Workspace": workspace_id},
        ).json()
        if int(status.get("indexed_revision") or 0) >= revision:
            return cast(Dict[str, Any], status)
        time.sleep(0.01)
    raise AssertionError(f"indexed_revision did not reach {revision}")


def _wait_until(predicate: Callable[[], bool], *, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _reset_index_worker_state() -> None:
    task = getattr(sync_router, "_INDEX_WORKER_TASK", None)
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:
            pass
    sync_router._INDEX_QUEUE = None
    sync_router._INDEX_WORKER_TASK = None
    sync_router._INDEX_LOOP = None


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(tmp_path),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        sync_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(tmp_path)),
    )
    monkeypatch.setattr(sync_router, "get_workspace_registry", lambda: registry)
    snapshot_root = tmp_path / "cloud-sync"
    monkeypatch.setattr(
        sync_router,
        "_SNAPSHOT_STORE",
        CloudSnapshotStore(root=snapshot_root),
    )
    sync_router._SYNC_STATES.clear()
    _reset_index_worker_state()

    app = FastAPI()
    app.state.snapshot_root = snapshot_root
    app.include_router(sync_router.router)
    with TestClient(app) as test_client:
        yield test_client
    _reset_index_worker_state()


def test_unknown_workspace_is_rejected_when_path_has_other_id(
    client: TestClient,
) -> None:
    response = client.get(
        "/sync/status",
        headers={"X-Omnicode-Workspace": "missing"},
    )

    assert response.status_code == 409
    assert "path is already registered as workspace_id: repo-a" in response.json()["detail"]


def test_unknown_workspace_is_auto_registered_on_empty_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(
        sync_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(tmp_path)),
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
    test_client = TestClient(app)

    response = test_client.get(
        "/sync/status",
        headers={"X-Omnicode-Workspace": "missing"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["workspace_id"] == "missing"
    assert body["accepted_revision"] == 0
    assert registry.get("missing") is not None


def test_batch_updates_accepted_revision_and_status(client: TestClient) -> None:
    content = "print('hello')\n"
    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 1,
            "client_revision": 2,
            "files": [
                {
                    "path": "src/app.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["accepted_revision"] == 2
    assert body["indexed_revision"] <= 2
    assert body["exact_indexed_revision"] == 2
    assert body["indexing"] is True
    assert body["files_accepted"] == 1
    snapshot_root = cast(Any, client.app).state.snapshot_root
    mirror = (
        snapshot_root
        / "workspaces"
        / "repo-a"
        / "mirror"
        / "src"
        / "app.py"
    )
    assert mirror.read_text(encoding="utf-8") == content

    status = _wait_indexed(client, revision=2)
    assert status["ok"] is True
    assert status["accepted_revision"] == 2
    assert status["indexed_files"] == 1
    assert status["exact_indexed_revision"] == 2
    assert status["exact_index_ready"] is True
    assert status["exact_index"]["files"] == 1
    assert status["exact_index"]["lines"] == 1
    assert status["indexing"] is False
    assert status["last_index_error"] is None
    assert "last_batch_elapsed_ms" in status
    assert status["snapshot_store"] == {
        "latest_revision": 2,
        "accepted_revision": 2,
        "indexed_revision": 2,
        "files": 1,
        "deletes": 0,
    }

    sync_router._SYNC_STATES.clear()
    restored = client.get(
        "/sync/status",
        headers={"X-Omnicode-Workspace": "repo-a"},
    ).json()
    assert restored["accepted_revision"] == 2
    assert restored["indexed_revision"] == 2
    assert restored["exact_indexed_revision"] == 2
    assert restored["indexed_files"] == 1

    query_status = client.get("/sync/status?workspace_id=repo-a").json()
    assert query_status["ok"] is True
    assert query_status["accepted_revision"] == 2
    assert query_status["indexed_revision"] == 2
    assert query_status["exact_indexed_revision"] == 2


def test_batch_with_low_client_revision_still_advances_cloud_revision(
    client: TestClient,
) -> None:
    first_content = "VALUE = 'first'\n"
    first = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 10,
            "files": [
                {
                    "path": "tests/tmp_cloudsim_first.py",
                    "hash": _sha(first_content),
                    "size": len(first_content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": first_content,
                }
            ],
            "deletes": [],
        },
    ).json()
    assert first["accepted_revision"] == 10

    second_content = "VALUE = 'second'\n"
    second = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 2,
            "files": [
                {
                    "path": "tests/tmp_cloudsim_second.py",
                    "hash": _sha(second_content),
                    "size": len(second_content),
                    "mtime_ms": 2,
                    "encoding": "utf-8",
                    "content": second_content,
                }
            ],
            "deletes": [],
        },
    ).json()

    assert second["ok"] is True
    assert second["files_accepted"] == 1
    assert second["accepted_revision"] == 11
    status = _wait_indexed(client, revision=11)
    assert status["indexed_revision"] == 11


def test_batch_updates_search_index_when_engine_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _Engine:
        async def upsert_content(self, path: str, content: str) -> int:
            calls.append((path, content))
            return 1

        async def delete_file_index(self, path: str) -> int:
            return 1

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    content = 'MARKER = "cloudsim-agent-v1"\n'

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [
                {
                    "path": "tests/tmp_cloudsim_sync_agent.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    _wait_until(lambda: calls == [("tests/tmp_cloudsim_sync_agent.py", content)])
    assert calls == [("tests/tmp_cloudsim_sync_agent.py", content)]
    assert "C:\\" not in calls[0][0]


def test_batch_filters_low_value_files_from_semantic_index(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_calls: list[list[str]] = []

    class _Engine:
        async def upsert_contents(self, files, *, refresh: bool = True) -> int:
            bulk_calls.append([item[0] for item in files])
            return len(files)

        def refresh_stats(self) -> None:
            return None

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    py_content = "def included():\n    return 1\n"
    json_content = '{"locale": "ignored-by-semantic"}\n'
    po_content = 'msgid "Hello"\nmsgstr "Hallo"\n'
    mo_content = "binary-ish catalog placeholder\n"

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [
                {
                    "path": "src/included.py",
                    "hash": _sha(py_content),
                    "size": len(py_content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": py_content,
                },
                {
                    "path": "fixtures/large.json",
                    "hash": _sha(json_content),
                    "size": len(json_content),
                    "mtime_ms": 2,
                    "encoding": "utf-8",
                    "content": json_content,
                },
                {
                    "path": "locale/django.po",
                    "hash": _sha(po_content),
                    "size": len(po_content),
                    "mtime_ms": 3,
                    "encoding": "utf-8",
                    "content": po_content,
                },
                {
                    "path": "locale/django.mo",
                    "hash": _sha(mo_content),
                    "size": len(mo_content),
                    "mtime_ms": 4,
                    "encoding": "utf-8",
                    "content": mo_content,
                },
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["files_accepted"] == 4
    assert body["semantic_files_enqueued"] == 1
    assert body["semantic_files_skipped"] == 3
    assert body["semantic_skip_reasons"] == {"extension_not_enabled": 3}

    status = _wait_indexed(client, revision=1)
    assert bulk_calls == [["src/included.py"]]
    assert status["exact_index"]["files"] == 4
    assert status["last_semantic_files_enqueued"] == 1
    assert status["last_semantic_files_skipped"] == 3
    assert status["last_semantic_skip_reasons"] == {"extension_not_enabled": 3}
    assert ".py" in status["semantic_index_policy"]["extensions"]
    assert ".json" not in status["semantic_index_policy"]["extensions"]


def test_semantic_filter_barrier_advances_for_all_skipped_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_engine():
        raise AssertionError("filtered-only sync must not initialize search engine")

    monkeypatch.setattr(sync_router, "get_search_engine", _unexpected_engine)
    content = '{"huge": "data-only"}\n'

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 5,
            "files": [
                {
                    "path": "fixtures/data.json",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["accepted_revision"] == 5
    assert body["semantic_files_enqueued"] == 0
    assert body["semantic_files_skipped"] == 1
    assert body["semantic_skip_reasons"] == {"extension_not_enabled": 1}

    status = _wait_indexed(client, revision=5)
    assert status["indexed_revision"] == 5
    assert status["semantic_index_ready"] is True
    assert status["exact_index"]["files"] == 1


def test_large_initial_sync_defaults_to_exact_only_semantic_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_engine():
        raise AssertionError("large initial sync should not initialize semantic engine")

    monkeypatch.setattr(sync_router, "get_search_engine", _unexpected_engine)
    py_content = "def large_repo_symbol():\n    return 1\n"
    json_content = '{"fixture": true}\n'

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 12,
            "metadata": {
                "phase": "initial_sync",
                "files_seen": 7000,
                "files_pushed": 7000,
            },
            "files": [
                {
                    "path": "src/large_repo_symbol.py",
                    "hash": _sha(py_content),
                    "size": len(py_content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": py_content,
                },
                {
                    "path": "fixtures/data.json",
                    "hash": _sha(json_content),
                    "size": len(json_content),
                    "mtime_ms": 2,
                    "encoding": "utf-8",
                    "content": json_content,
                },
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["accepted_revision"] == 12
    assert body["files_accepted"] == 2
    assert body["semantic_files_enqueued"] == 0
    assert body["semantic_files_skipped"] == 2
    assert body["semantic_skip_reasons"] == {
        "initial_sync_large_repo_exact_only": 2
    }

    status = _wait_indexed(client, revision=12)
    assert status["indexed_revision"] == 12
    assert status["exact_indexed_revision"] == 12
    assert status["exact_index"]["files"] == 2
    assert status["semantic_index_ready"] is False
    assert status["search_degraded"] is True
    assert status["semantic_index_coverage"] == "exact_only_initial_sync"
    assert status["semantic_initial_exact_only"] is True
    assert status["semantic_index_policy"]["initial_sync_mode"] == "auto"
    assert status["semantic_index_policy"]["initial_sync_file_limit"] == 2000
    assert status["last_semantic_skip_reasons"] == {
        "initial_sync_large_repo_exact_only": 2
    }


def test_large_initial_sync_semantic_policy_can_be_forced_full(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_SYNC_SEMANTIC_INITIAL_MODE", "full")
    bulk_calls: list[list[str]] = []

    class _Engine:
        async def upsert_contents(self, files, *, refresh: bool = True) -> int:
            bulk_calls.append([item[0] for item in files])
            return len(files)

        def refresh_stats(self) -> None:
            return None

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    content = "def force_full():\n    return 1\n"

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 13,
            "metadata": {
                "phase": "initial_sync",
                "files_seen": 7000,
            },
            "files": [
                {
                    "path": "src/force_full.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": content,
                },
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["semantic_files_enqueued"] == 1
    assert body["semantic_files_skipped"] == 0
    status = _wait_indexed(client, revision=13)
    assert bulk_calls == [["src/force_full.py"]]
    assert status["semantic_index_ready"] is True
    assert status["semantic_index_coverage"] == "selected_files"
    assert status["semantic_initial_exact_only"] is False
    assert status["semantic_index_policy"]["initial_sync_mode"] == "full"


def test_semantic_filter_extension_override_allows_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_SYNC_SEMANTIC_EXTENSIONS", ".py,.json")
    bulk_calls: list[list[str]] = []

    class _Engine:
        async def upsert_contents(self, files, *, refresh: bool = True) -> int:
            bulk_calls.append([item[0] for item in files])
            return len(files)

        def refresh_stats(self) -> None:
            return None

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    content = '{"config": true}\n'

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [
                {
                    "path": "config/settings.json",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["semantic_files_enqueued"] == 1
    assert body["semantic_files_skipped"] == 0
    status = _wait_indexed(client, revision=1)
    assert bulk_calls == [["config/settings.json"]]
    assert ".json" in status["semantic_index_policy"]["extensions"]


def test_semantic_filter_skips_oversized_source_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_SYNC_SEMANTIC_MAX_FILE_BYTES", "10")
    content = "VALUE = 'too large for semantic'\n"

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 2,
            "files": [
                {
                    "path": "src/large.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 1,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["semantic_files_enqueued"] == 0
    assert body["semantic_files_skipped"] == 1
    assert body["semantic_skip_reasons"] == {"file_too_large": 1}
    status = _wait_indexed(client, revision=2)
    assert status["indexed_revision"] == 2
    assert status["exact_index"]["files"] == 1


def test_index_worker_serializes_background_updates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, list[str]]] = []
    active = 0
    max_active = 0

    def _blocking_index(
        workspace_id: str,
        revision: int,
        changed_files: list[tuple[str, str] | tuple[str, str, dict[str, object]]],
        deleted_paths: list[str],
        semantic_coverage: str = "unknown",
    ) -> int:
        nonlocal active, max_active
        assert workspace_id == "repo-a"
        assert not deleted_paths
        active += 1
        max_active = max(max_active, active)
        try:
            time.sleep(0.2)
            paths = []
            for item in changed_files:
                paths.append(item[0])
            calls.append((revision, paths))
            return revision
        finally:
            active -= 1

    monkeypatch.setattr(sync_router, "_run_index_update_blocking", _blocking_index)

    for idx in range(5):
        content = f'VALUE = "{idx}"\n'
        response = client.post(
            "/sync/batch",
            headers={"X-Omnicode-Workspace": "repo-a"},
            json={
                "client_id": "local-1",
                "base_revision": idx,
                "client_revision": idx + 1,
                "files": [
                    {
                        "path": f"tests/tmp_cloudsim_queue_{idx}.py",
                        "hash": _sha(content),
                        "size": len(content),
                        "mtime_ms": idx,
                        "encoding": "utf-8",
                        "content": content,
                    }
                ],
                "deletes": [],
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    status = _wait_indexed(client, revision=5)

    assert max_active == 1
    assert len(calls) < 5
    assert status["index_jobs_enqueued"] == 5
    assert status["index_jobs_completed"] == 5
    assert status["index_queue_depth"] == 0
    assert status["index_worker_running"] is False
    assert status["last_index_revision"] == 5
    assert isinstance(status["last_index_elapsed_ms"], int)


def test_index_coalescing_splits_large_worker_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_SYNC_INDEX_CHUNK_FILES", "2")
    jobs = [
        sync_router._IndexJob(
            workspace_id="repo-a",
            revision=idx + 1,
            changed_files=[(f"tests/tmp_cloudsim_chunk_{idx}.py", str(idx))],
            deleted_paths=[],
        )
        for idx in range(5)
    ]

    groups = sync_router._coalesce_index_jobs(jobs)

    assert [group.revision for group in groups] == [2, 4, 5]
    assert [group.job_count for group in groups] == [2, 2, 1]
    assert [len(group.changed_files) for group in groups] == [2, 2, 1]


def test_index_coalescing_default_chunk_is_large_repo_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_SYNC_INDEX_CHUNK_FILES", raising=False)
    jobs = [
        sync_router._IndexJob(
            workspace_id="repo-a",
            revision=idx + 1,
            changed_files=[(f"tests/tmp_cloudsim_default_chunk_{idx}.py", str(idx))],
            deleted_paths=[],
        )
        for idx in range(30)
    ]

    groups = sync_router._coalesce_index_jobs(jobs)

    assert [group.revision for group in groups] == [25, 30]
    assert [group.job_count for group in groups] == [25, 5]
    assert [len(group.changed_files) for group in groups] == [25, 5]


def test_index_coalescing_splits_by_content_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_SYNC_INDEX_CHUNK_FILES", "25")
    monkeypatch.setenv("OMNICODE_SYNC_INDEX_CHUNK_BYTES", "10")
    jobs = [
        sync_router._IndexJob(
            workspace_id="repo-a",
            revision=idx + 1,
            changed_files=[(f"tests/tmp_cloudsim_chunk_bytes_{idx}.py", "abcd")],
            deleted_paths=[],
        )
        for idx in range(5)
    ]

    groups = sync_router._coalesce_index_jobs(jobs)

    assert [group.revision for group in groups] == [2, 4, 5]
    assert [group.job_count for group in groups] == [2, 2, 1]
    assert [group.changed_bytes for group in groups] == [8, 8, 4]


def test_status_payload_reports_search_readiness() -> None:
    state = sync_router._SyncWorkspaceState(
        workspace_id="repo-a",
        accepted_revision=10,
        indexed_revision=7,
        file_count=4,
    )
    state.index_worker_running = True
    state.index_queue_depth = 1
    state.indexing = True

    degraded = sync_router._status_payload(
        state,
        {
            "latest_revision": 10,
            "accepted_revision": 10,
            "indexed_revision": 7,
            "file_count": 4,
            "delete_count": 0,
        },
    )

    assert degraded["snapshot_ready"] is True
    assert degraded["semantic_index_ready"] is False
    assert degraded["exact_index_ready"] is False
    assert degraded["index_worker_busy"] is True
    assert degraded["search_degraded"] is True
    assert degraded["recommended_query_mode"] == "snapshot_only"
    assert degraded["query_mode_reason"] == "exact_index_catching_up"
    assert degraded["exact_query_safe"] is False
    assert degraded["strict_semantic_safe"] is False
    assert degraded["index_readiness_contract"]["schema_version"] == "index_readiness.v1"
    assert degraded["semantic_pending_revisions"] == 3
    assert degraded["exact_pending_revisions"] == 10

    state.indexed_revision = 10
    state.index_worker_running = False
    state.index_queue_depth = 0
    state.indexing = False

    ready = sync_router._status_payload(
        state,
        {
            "latest_revision": 10,
            "accepted_revision": 10,
            "indexed_revision": 10,
            "file_count": 4,
            "delete_count": 0,
        },
        {
            "exact_indexed_revision": 10,
            "files": 4,
            "symbols": 2,
            "lines": 20,
            "line_fts_available": True,
        },
    )

    assert ready["snapshot_ready"] is True
    assert ready["semantic_index_ready"] is True
    assert ready["exact_index_ready"] is True
    assert ready["index_worker_busy"] is False
    assert ready["search_degraded"] is False
    assert ready["recommended_query_mode"] == "semantic_first"
    assert ready["query_mode_reason"] == "semantic_full"
    assert ready["exact_query_safe"] is True
    assert ready["strict_semantic_safe"] is True
    assert "semantic" in ready["supported_query_modes"]
    assert ready["semantic_pending_revisions"] == 0
    assert ready["exact_pending_revisions"] == 0
    assert ready["exact_index"]["symbols"] == 2


def test_status_payload_reports_exact_only_query_contract() -> None:
    state = sync_router._SyncWorkspaceState(
        workspace_id="repo-a",
        accepted_revision=10,
        indexed_revision=10,
        file_count=7000,
    )

    payload = sync_router._status_payload(
        state,
        {
            "latest_revision": 10,
            "accepted_revision": 10,
            "indexed_revision": 10,
            "file_count": 7000,
            "delete_count": 0,
            "semantic_index_coverage": "exact_only_initial_sync",
            "semantic_initial_exact_only": True,
        },
        {
            "exact_indexed_revision": 10,
            "files": 7000,
            "symbols": 45000,
            "lines": 1100000,
            "line_fts_available": False,
        },
    )

    assert payload["snapshot_ready"] is True
    assert payload["exact_index_ready"] is True
    assert payload["semantic_index_ready"] is False
    assert payload["search_degraded"] is True
    assert payload["recommended_query_mode"] == "exact_first"
    assert payload["query_mode_reason"] == "exact_only_initial_sync"
    assert payload["exact_query_safe"] is True
    assert payload["strict_semantic_safe"] is False
    assert payload["index_readiness_contract"]["semantic"]["initial_exact_only"] is True


def test_index_update_refreshes_stats_without_reinitializing_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_flags: list[bool] = []
    delete_refresh_flags: list[bool] = []
    initialize_calls = 0
    refresh_stats_calls = 0

    class _Engine:
        async def upsert_content(
            self,
            path: str,
            content: str,
            *,
            refresh: bool = True,
        ) -> int:
            refresh_flags.append(refresh)
            return 1

        async def delete_file_index(
            self,
            path: str,
            *,
            refresh: bool = True,
        ) -> bool:
            delete_refresh_flags.append(refresh)
            return True

        async def initialize(self) -> None:
            nonlocal initialize_calls
            initialize_calls += 1

        def refresh_stats(self) -> None:
            nonlocal refresh_stats_calls
            refresh_stats_calls += 1

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        sync_router._SNAPSHOT_STORE,
        "mark_indexed",
        lambda *, workspace_id, revision, **_kwargs: revision,
    )

    indexed = sync_router._run_index_update_blocking(
        "repo-a",
        7,
        [("tests/tmp_cloudsim_a.py", "A"), ("tests/tmp_cloudsim_b.py", "B")],
        ["tests/tmp_cloudsim_deleted.py"],
    )

    assert indexed == 7
    assert refresh_flags == [False, False]
    assert delete_refresh_flags == [False]
    assert initialize_calls == 0
    assert refresh_stats_calls == 1


def test_index_update_prefers_bulk_upsert_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_calls: list[tuple[list[tuple[str, str]], bool]] = []
    single_calls: list[str] = []
    initialize_calls = 0
    refresh_stats_calls = 0

    class _Engine:
        async def upsert_contents(
            self,
            files: list[tuple[str, str]],
            *,
            refresh: bool = True,
        ) -> int:
            bulk_calls.append((files, refresh))
            return len(files)

        async def upsert_content(
            self,
            path: str,
            content: str,
            *,
            refresh: bool = True,
        ) -> int:
            single_calls.append(path)
            return 1

        async def initialize(self) -> None:
            nonlocal initialize_calls
            initialize_calls += 1

        def refresh_stats(self) -> None:
            nonlocal refresh_stats_calls
            refresh_stats_calls += 1

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        sync_router._SNAPSHOT_STORE,
        "mark_indexed",
        lambda *, workspace_id, revision, **_kwargs: revision,
    )

    indexed = sync_router._run_index_update_blocking(
        "repo-a",
        8,
        [("tests/tmp_cloudsim_a.py", "A"), ("tests/tmp_cloudsim_b.py", "B")],
        [],
    )

    assert indexed == 8
    assert bulk_calls == [
        (
            [("tests/tmp_cloudsim_a.py", "A"), ("tests/tmp_cloudsim_b.py", "B")],
            False,
        )
    ]
    assert single_calls == []
    assert initialize_calls == 0
    assert refresh_stats_calls == 1


def test_index_update_preserves_hash_metadata_for_bulk_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_calls: list[tuple[list[tuple[str, str, dict[str, str | int]]], bool]] = []

    class _Engine:
        async def upsert_contents(
            self,
            files: list[tuple[str, str, dict[str, str | int]]],
            *,
            refresh: bool = True,
        ) -> int:
            bulk_calls.append((files, refresh))
            return len(files)

        def refresh_stats(self) -> None:
            return None

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        sync_router._SNAPSHOT_STORE,
        "mark_indexed",
        lambda *, workspace_id, revision, **_kwargs: revision,
    )

    indexed = sync_router._run_index_update_blocking(
        "repo-a",
        9,
        [
            (
                "tests/tmp_cloudsim_a.py",
                "A",
                {
                    "content_hash": "sha256:a",
                    "snapshot_hash": "sha256:a",
                    "snapshot_revision": 9,
                    "workspace_id": "repo-a",
                },
            )
        ],
        [],
    )

    assert indexed == 9
    assert bulk_calls == [
        (
            [
                (
                    "tests/tmp_cloudsim_a.py",
                    "A",
                    {
                        "content_hash": "sha256:a",
                        "snapshot_hash": "sha256:a",
                        "snapshot_revision": 9,
                        "workspace_id": "repo-a",
                    },
                )
            ],
            False,
        )
    ]


def test_index_update_marks_exact_only_when_semantic_embedding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_calls: list[tuple[list[tuple[str, str]], bool]] = []
    coverages: list[str | None] = []
    refresh_stats_calls = 0

    class _Engine:
        def semantic_available(self) -> bool:
            return False

        async def upsert_contents(
            self,
            files: list[tuple[str, str]],
            *,
            refresh: bool = True,
        ) -> int:
            bulk_calls.append((files, refresh))
            return len(files)

        def refresh_stats(self) -> None:
            nonlocal refresh_stats_calls
            refresh_stats_calls += 1

    def _mark_indexed(*, workspace_id, revision, semantic_coverage=None, **_kwargs):
        coverages.append(semantic_coverage)
        return revision

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(sync_router._SNAPSHOT_STORE, "mark_indexed", _mark_indexed)

    indexed = sync_router._run_index_update_blocking(
        "repo-a",
        10,
        [("tests/tmp_cloudsim_a.py", "A")],
        [],
        semantic_coverage="selected_files",
    )

    assert indexed == 10
    assert bulk_calls == []
    assert refresh_stats_calls == 1
    assert coverages == ["exact_only_initial_sync"]


def test_batch_skips_unchanged_hash_without_revision_bump(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _Engine:
        async def upsert_content(self, path: str, content: str) -> int:
            calls.append((path, content))
            return 1

        async def delete_file_index(self, path: str) -> int:
            return 1

    monkeypatch.setattr(sync_router, "get_search_engine", lambda: _Engine())
    content = 'VALUE = "v1"\n'
    payload = {
        "client_id": "local-1",
        "base_revision": 0,
        "client_revision": 1,
        "files": [
            {
                "path": "tests/tmp_cloudsim_incremental.py",
                "hash": _sha(content),
                "size": len(content),
                "mtime_ms": 123,
                "encoding": "utf-8",
                "content": content,
            }
        ],
        "deletes": [],
    }

    first = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json=payload,
    ).json()
    _wait_indexed(client, revision=1)
    payload["base_revision"] = first["accepted_revision"]
    payload["client_revision"] = first["accepted_revision"] + 1
    second = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json=payload,
    ).json()

    assert first["ok"] is True
    assert first["files_accepted"] == 1
    assert first["accepted_revision"] == 1
    assert second["ok"] is True
    assert second["files_accepted"] == 0
    assert second["skipped_unchanged"] == 1
    assert second["accepted_revision"] == 1
    assert second["indexed_revision"] == 1
    assert second["skipped_paths"] == [
        {"path": "tests/tmp_cloudsim_incremental.py", "hash": _sha(content)}
    ]
    assert calls == [("tests/tmp_cloudsim_incremental.py", content)]


def test_invalid_hash_returns_structured_error(client: TestClient) -> None:
    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [
                {
                    "path": "src/app.py",
                    "hash": "sha256:not-the-real-hash",
                    "size": 9,
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": "print(1)\n",
                }
            ],
            "deletes": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "invalid hash" in body["error"]
    assert body["accepted_revision"] == 0


def test_hash_mismatch_returns_structured_error(client: TestClient) -> None:
    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [
                {
                    "path": "src/app.py",
                    "hash": "sha256:" + ("0" * 64),
                    "size": 9,
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": "print(1)\n",
                }
            ],
            "deletes": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "hash mismatch" in body["error"]
    assert body["accepted_revision"] == 0


def test_large_sync_auto_disables_line_fts_without_breaking_exact_index(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_EXACT_LINE_FTS", raising=False)
    monkeypatch.setattr(sync_router, "_AUTO_FTS_LINE_LIMIT", 3)
    content = (
        "class BigSyncSymbol:\n"
        "    value = 1\n"
        "    value = 2\n"
        "    value = 3\n"
        "    value = 4\n"
    )

    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 7,
            "files": [
                {
                    "path": "pkg/big.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["exact_indexed_revision"] == 7

    status = _wait_indexed(client, revision=7)
    assert status["exact_index"]["files"] == 1
    assert status["exact_index"]["symbols"] >= 1
    assert status["exact_index"]["lines"] == 5
    assert status["exact_index"]["line_fts_available"] is False
    assert status["exact_index"]["line_fts_reason"] == "disabled_for_large_workspace"

    symbols = sync_router._exact_index().search_symbols(
        workspace_id="repo-a",
        query="BigSyncSymbol",
        fuzzy=False,
        max_results=3,
    )
    assert symbols
    assert symbols[0].path == "pkg/big.py"

    text = sync_router._exact_index().search_text(
        workspace_id="repo-a",
        query="class BigSyncSymbol:",
        case_sensitive=True,
        max_results=3,
    )
    assert text
    assert text[0].path == "pkg/big.py"


def test_path_escape_is_rejected_without_absolute_path_leak(client: TestClient) -> None:
    response = client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 1,
            "files": [],
            "deletes": [{"path": "../escape.py"}],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert "Invalid sync path" in body["error"]
    assert str(Path.cwd()) not in body["error"]


def test_barrier_ready_after_accepted_batch(client: TestClient) -> None:
    content = "x = 1\n"
    client.post(
        "/sync/batch",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "client_id": "local-1",
            "base_revision": 0,
            "client_revision": 4,
            "files": [
                {
                    "path": "src/app.py",
                    "hash": _sha(content),
                    "size": len(content),
                    "mtime_ms": 123,
                    "encoding": "utf-8",
                    "content": content,
                }
            ],
            "deletes": [],
        },
    )
    _wait_indexed(client, revision=4)

    response = client.post(
        "/sync/barrier",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={"min_revision": 4, "paths": ["src/app.py"], "wait_ms": 0},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["ready"] is True
    assert body["stale"] is False
    assert body["indexed_revision"] == 4


def test_barrier_stale_response_is_actionable(client: TestClient) -> None:
    response = client.post(
        "/sync/barrier",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={"min_revision": 10, "paths": ["src/app.py"], "wait_ms": 1},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["ready"] is False
    assert body["stale"] is True
    assert body["local_revision"] == 10
    assert body["indexed_revision"] == 0
    assert "Run omni_status()" in body["next_actions"][1]
