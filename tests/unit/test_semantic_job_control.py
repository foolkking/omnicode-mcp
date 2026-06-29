from __future__ import annotations

import threading
import hashlib
from pathlib import Path

import numpy as np

from api.v1.routers import search as search_router
from omnicode.search.engine import SemanticSearchEngine
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def _job(workspace_id: str, state: str = "running") -> dict:
    event = threading.Event()
    event.set()
    return {
        "job_id": f"{workspace_id}:1",
        "workspace_id": workspace_id,
        "state": state,
        "force": False,
        "scope": "semantic",
        "attempt": 1,
        "retryable": state in {"failed", "interrupted"},
        "resume_event": event,
        "thread": None,
        "records_seen": 5,
        "records_total": 10,
    }


def test_semantic_job_pause_and_resume_persist_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    search_router._SNAPSHOT_INDEX_JOBS.clear()
    job = _job("repo-a")
    search_router._SNAPSHOT_INDEX_JOBS["repo-a"] = job

    paused = search_router.control_snapshot_index_job(
        "repo-a",
        action="pause",
    )
    assert paused["state"] == "paused"
    assert job["resume_event"].is_set() is False
    assert search_router._snapshot_job_state_path("repo-a").is_file()

    resumed = search_router.control_snapshot_index_job(
        "repo-a",
        action="resume",
    )
    assert resumed["state"] == "running"
    assert job["resume_event"].is_set() is True


def test_semantic_job_retry_uses_previous_configuration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    search_router._SNAPSHOT_INDEX_JOBS.clear()
    failed = _job("repo-a", state="failed")
    failed["force"] = True
    failed["scope"] = "semantic"
    search_router._SNAPSHOT_INDEX_JOBS["repo-a"] = failed
    captured: dict = {}

    def fake_start(
        workspace_id: str,
        *,
        force: bool,
        scope: str,
        staging_dir: str | None = None,
        resume_staging: bool = False,
    ):
        captured.update({
            "workspace_id": workspace_id,
            "force": force,
            "scope": scope,
            "staging_dir": staging_dir,
            "resume_staging": resume_staging,
        })
        return {"job_id": "repo-a:2", "state": "running"}

    monkeypatch.setattr(
        search_router,
        "_start_snapshot_index_job",
        fake_start,
    )

    result = search_router.control_snapshot_index_job(
        "repo-a",
        action="retry",
    )

    assert result["state"] == "running"
    assert captured == {
        "workspace_id": "repo-a",
        "force": True,
        "scope": "semantic",
        "staging_dir": None,
        "resume_staging": False,
    }


def test_semantic_job_retry_reuses_persisted_staging(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    search_router._SNAPSHOT_INDEX_JOBS.clear()
    staging = tmp_path / "state" / "index-jobs" / "staging" / "repo-a"
    staging.mkdir(parents=True)
    failed = _job("repo-a", state="interrupted")
    failed.update({
        "force": True,
        "scope": "semantic",
        "staging_dir": str(staging),
    })
    search_router._SNAPSHOT_INDEX_JOBS["repo-a"] = failed
    captured: dict = {}

    def fake_start(workspace_id: str, **kwargs):
        captured.update({"workspace_id": workspace_id, **kwargs})
        return {"job_id": "repo-a:2", "state": "running"}

    monkeypatch.setattr(
        search_router,
        "_start_snapshot_index_job",
        fake_start,
    )

    search_router.control_snapshot_index_job("repo-a", action="retry")

    assert captured["staging_dir"] == str(staging)
    assert captured["resume_staging"] is True


def test_semantic_job_status_marks_persisted_running_job_interrupted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    search_router._SNAPSHOT_INDEX_JOBS.clear()
    job = _job("repo-a")
    search_router._persist_snapshot_index_job(job)

    status = search_router.snapshot_index_job_status("repo-a")

    assert status["state"] == "interrupted"
    assert status["job"]["retryable"] is True
    assert "restarted" in status["job"]["error"]


def test_semantic_job_progress_reports_rate_eta_and_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(tmp_path / "state"))
    search_router._SNAPSHOT_INDEX_JOBS.clear()

    def fake_run(
        workspace_id: str,
        *,
        force: bool,
        scope: str,
        progress,
        staging_dir: str | None = None,
        resume_staging: bool = False,
    ) -> dict:
        progress({
            "records_seen": 5,
            "records_total": 10,
            "indexed_files": 5,
            "indexed_chunks": 15,
        })
        return {
            "records_seen": 10,
            "records_total": 10,
            "indexed_files": 10,
            "indexed_chunks": 30,
            "skipped_unchanged": 0,
            "skipped_by_indexed_revision": 0,
            "skipped_by_policy": 0,
            "skip_policy_reasons": {},
            "deleted_index_entries": 0,
            "indexed_revision_watermark": 0,
            "staging_resumed": resume_staging,
            "activation": {"activated": bool(staging_dir)},
        }

    monkeypatch.setattr(
        search_router,
        "_run_snapshot_index_blocking",
        fake_run,
    )

    search_router._start_snapshot_index_job(
        "repo-a",
        force=False,
        scope="semantic",
    )
    internal = search_router._SNAPSHOT_INDEX_JOBS["repo-a"]
    internal["thread"].join(timeout=5)
    status = search_router.snapshot_index_job_status("repo-a")
    job = status["job"]

    assert status["state"] == "completed"
    assert job["progress_percent"] == 100.0
    assert job["eta_seconds"] == 0.0
    assert job["indexed_chunks"] == 30
    assert job["records_per_second"] >= 0


def test_semantic_full_bootstrap_activates_staging_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=state / "cloud-sync")
    content = "def target():\n    return 'middleware request handler'\n"
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    store.upsert(
        workspace_id="repo-a",
        path="pkg/service.py",
        content=content,
        hash_value=digest,
        size=len(content),
        mtime_ms=1,
        encoding="utf-8",
        revision=7,
    )

    class _Embedding:
        name = "test-embedding"
        dimension = 384
        _model_name = "sentence-transformers/all-MiniLM-L6-v2"

        @staticmethod
        def encode(value):
            if isinstance(value, list):
                return np.ones((len(value), 384), dtype=np.float32)
            return np.ones(384, dtype=np.float32)

    active = SemanticSearchEngine(
        str(workspace),
        db_dir=str(state / "search-indexes" / "active"),
    )
    active.embedding_model = _Embedding()
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state))
    monkeypatch.setenv(
        "OMNICODE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: active)
    staging = state / "index-jobs" / "staging" / "repo-a"

    result = search_router._run_snapshot_index_blocking(
        "repo-a",
        force=True,
        scope="semantic",
        staging_dir=str(staging),
        resume_staging=False,
    )

    assert result["staging_used"] is True
    assert result["activation"]["activated"] is True
    assert result["activation"]["vector_count"] > 0
    assert active.vector_store.index.ntotal > 0
    assert not staging.exists()
    assert store.status("repo-a")["semantic_index_coverage"] == "semantic_full"
