from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnicode_core.workspace.registry import WorkspaceRegistry
from omnicode_core.workspace.exact_index import SnapshotExactIndex
from omnicode_core.workspace.snapshot_store import CloudSnapshotStore


def _load_router_module(name: str, rel_path: str):
    root = Path(__file__).resolve().parents[2]
    module_path = root / rel_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


files_router = _load_router_module(
    "files_router_under_test",
    "api/v1/routers/files.py",
)
search_router = _load_router_module(
    "search_router_under_test",
    "api/v1/routers/search.py",
)
intelligence_router = _load_router_module(
    "intelligence_router_under_test",
    "api/v1/routers/intelligence.py",
)
graph_router = _load_router_module(
    "graph_router_under_test",
    "api/v1/routers/graph.py",
)


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_read_falls_back_to_cloud_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = 'VALUE = "v2"\n'
    store.upsert(
        workspace_id="repo-a",
        path="tests/tmp_cloudsim_incremental.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=8,
    )

    class _Engine:
        async def read_symbol_content(self, **kwargs):
            return {"success": False, "error": "File not found"}

    monkeypatch.setattr(
        files_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(files_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(files_router, "CloudSnapshotStore", lambda: store)

    app = FastAPI()
    app.include_router(files_router.router)
    client = TestClient(app)

    response = client.post(
        "/read",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={
            "file_path": "tests/tmp_cloudsim_incremental.py",
            "mode": "full",
            "with_line_numbers": True,
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["success"] is True
    assert body["result"]["source"] == "snapshot_store"
    assert body["result"]["content"] == '1 | VALUE = "v2"'


def test_outline_read_falls_back_to_cloud_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = 'def cloudsim_route():\n    return "local-v2"\n'
    store.upsert(
        workspace_id="repo-a",
        path="tests/tmp_cloudsim_routing.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=23,
    )

    class _Engine:
        async def list_symbols_in_file(self, _file_path):
            return {"symbols": [], "language": "python"}

    monkeypatch.setattr(
        files_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(files_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(files_router, "CloudSnapshotStore", lambda: store)

    app = FastAPI()
    app.include_router(files_router.router)
    client = TestClient(app)

    response = client.post(
        "/read",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={
            "file_path": "tests/tmp_cloudsim_routing.py",
            "mode": "outline",
            "with_line_numbers": True,
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["source"] == "snapshot_store"
    assert body["result"]["symbol_count"] == 1
    assert body["result"]["symbols"][0]["name"] == "cloudsim_route"


def test_text_search_includes_cloud_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = 'VALUE = "v2"\n'
    store.upsert(
        workspace_id="repo-a",
        path="tests/tmp_cloudsim_incremental.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=8,
    )

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/text",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"query": 'VALUE = "v2"', "max_results": 10},
    )
    stale = client.post(
        "/search/text",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"query": 'VALUE = "v1"', "max_results": 10},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["total_results"] == 1
    row = body["result"]["results"][0]
    assert row["file_path"] == "tests/tmp_cloudsim_incremental.py"
    assert row["line_content"] == 'VALUE = "v2"'
    assert row["hash"] == _sha(content)
    assert row["revision"] == 8
    assert stale.json()["result"]["total_results"] == 0


def test_text_search_reads_snapshot_records_without_index_reload(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    def _read_text_should_not_run(**_kwargs):
        raise AssertionError("text snapshot scan should not reload index per file")

    monkeypatch.setattr(store, "read_text", _read_text_should_not_run)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/text",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"query": "class BaseHandler:", "max_results": 10},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["results"][0]["file_path"] == "django/core/handlers/base.py"


def test_symbol_search_bootstraps_from_cloud_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = (
        "class BaseHandler:\n"
        "    def load_middleware(self):\n"
        "        pass\n"
    )
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Engine:
        async def search(self, _request):
            raise AssertionError("snapshot exact symbol should not call search engine")

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "snapshot_required_revision": 11,
            "semantic_fresh": False,
            "snapshot_fresh": True,
            "semantic_stale": True,
            "freshness": "snapshot_fresh",
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"query": "BaseHandler", "fuzzy": False, "max_results": 10},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["snapshot_store_used"] is True
    assert body["result"]["snapshot_fast_path"] is True
    assert body["result"]["total_results"] == 1
    row = body["result"]["results"][0]
    assert row["file_path"] == "django/core/handlers/base.py"
    assert row["symbol_name"] == "BaseHandler"
    assert row["symbol_type"] == "class"
    assert row["source"] == "snapshot_store"
    assert row["line_start"] == 1
    assert row["hash"] == _sha(content)
    assert row["revision"] == 11
    assert "symbol:exact" in row["why_matched"]


def test_snapshot_index_marks_indexed_revision_and_uses_record_reads(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    def _read_text_should_not_run(**_kwargs):
        raise AssertionError("snapshot indexing should not reload index per file")

    class _Engine:
        def __init__(self) -> None:
            self.files = []
            self.refreshed = False

        async def upsert_contents(self, files, *, refresh=False):
            self.files.extend(files)
            assert refresh is False
            return len(files)

        def refresh_stats(self):
            self.refreshed = True

        def get_stats(self):
            return {"total_files": len(self.files), "refreshed": self.refreshed}

    engine = _Engine()
    monkeypatch.setattr(store, "read_text", _read_text_should_not_run)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: engine)

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a"},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["snapshot_store_used"] is True
    assert result["force"] is False
    assert result["records_seen"] == 1
    assert result["indexed_files"] == 1
    assert result["indexed_chunks"] == 1
    assert result["skipped_unchanged"] == 0
    assert result["deleted_index_entries"] == 0
    assert result["accepted_revision"] == 11
    assert result["indexed_revision"] == 11
    assert result["stats"]["refreshed"] is True
    assert result["scope"] == "semantic"
    assert result["skipped_by_policy"] == 0
    assert store.status("repo-a")["semantic_index_coverage"] == "semantic_full"
    assert store.status("repo-a")["semantic_initial_exact_only"] is False
    assert engine.files == [
        (
            "django/core/handlers/base.py",
            content,
            {
                "content_hash": _sha(content),
                "snapshot_hash": _sha(content),
                "snapshot_revision": 11,
                "workspace_id": "repo-a",
            },
        )
    ]
    assert store.status("repo-a")["indexed_revision"] == 11


def test_snapshot_semantic_bootstrap_clears_exact_only_coverage(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )
    store.mark_indexed(
        workspace_id="repo-a",
        revision=11,
        semantic_coverage="exact_only_initial_sync",
    )

    class _Engine:
        async def upsert_contents(self, files, *, refresh=False):
            return len(files)

        def refresh_stats(self):
            return None

        def get_stats(self):
            return {"total_files": 1}

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a", "force": True, "scope": "semantic"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["scope"] == "semantic"
    status = store.status("repo-a")
    assert status["semantic_index_coverage"] == "semantic_full"
    assert status["semantic_initial_exact_only"] is False


def test_snapshot_index_exact_policy_skips_low_value_files(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    py_content = "class BaseHandler:\n    pass\n"
    json_content = '{"fixture": true}\n'
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=py_content,
        hash_value=_sha(py_content),
        size=len(py_content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )
    store.upsert(
        workspace_id="repo-a",
        path="fixtures/data.json",
        content=json_content,
        hash_value=_sha(json_content),
        size=len(json_content),
        mtime_ms=124,
        encoding="utf-8",
        revision=12,
    )
    indexed_paths: list[str] = []

    class _Engine:
        async def upsert_contents(self, files, *, refresh=False):
            indexed_paths.extend(item[0] for item in files)
            return len(files)

        def refresh_stats(self):
            return None

        def get_stats(self):
            return {"total_files": len(indexed_paths)}

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a", "scope": "exact_policy"},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["scope"] == "exact_policy"
    assert result["indexed_files"] == 1
    assert result["skipped_by_policy"] == 1
    assert result["skip_policy_reasons"] == {"extension_not_enabled": 1}
    assert indexed_paths == ["django/core/handlers/base.py"]
    assert store.status("repo-a")["semantic_index_coverage"] == "filtered"


def test_snapshot_index_reports_running_progress(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    contents = {
        "django/core/handlers/base.py": "class BaseHandler:\n    pass\n",
        "django/urls/resolvers.py": "class URLResolver:\n    pass\n",
        "django/db/models/base.py": "class Model:\n    pass\n",
    }
    for idx, (path, content) in enumerate(contents.items(), start=1):
        store.upsert(
            workspace_id="repo-a",
            path=path,
            content=content,
            hash_value=_sha(content),
            size=len(content),
            mtime_ms=123 + idx,
            encoding="utf-8",
            revision=idx,
        )

    class _Engine:
        async def upsert_contents(self, files, *, refresh=False):
            return len(files)

        def refresh_stats(self):
            return None

        def get_stats(self):
            return {"total_files": len(contents)}

    progress: list[dict] = []
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())

    result = search_router._run_snapshot_index_blocking(
        "repo-a",
        progress=progress.append,
    )

    assert result["records_total"] == 3
    assert progress
    assert progress[0]["records_seen"] == 0
    assert progress[0]["records_total"] == 3
    assert progress[-1]["records_seen"] == 3
    assert progress[-1]["records_total"] == 3
    assert progress[-1]["indexed_files"] == 3


def test_snapshot_index_skips_unchanged_hashes_without_reading_objects(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    def _read_record_should_not_run(**_kwargs):
        raise AssertionError("unchanged snapshot record should be skipped")

    class _Engine:
        def __init__(self) -> None:
            self.files = []
            self.deleted = []
            self.refreshed = False

        def indexed_file_hashes(self, *, workspace_id=None):
            assert workspace_id == "repo-a"
            return {
                "django/core/handlers/base.py": _sha(content),
                "django/core/handlers/old.py": "sha256:old",
            }

        async def upsert_contents(self, files, *, refresh=False):
            self.files.extend(files)
            return len(files)

        async def delete_file_index(self, path, *, refresh=False):
            assert refresh is False
            self.deleted.append(path)
            return True

        def refresh_stats(self):
            self.refreshed = True

        def get_stats(self):
            return {"total_files": 1, "refreshed": self.refreshed}

    engine = _Engine()
    monkeypatch.setattr(store, "read_record_text", _read_record_should_not_run)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: engine)

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a"},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["records_seen"] == 1
    assert result["indexed_files"] == 0
    assert result["indexed_chunks"] == 0
    assert result["skipped_unchanged"] == 1
    assert result["deleted_index_entries"] == 1
    assert result["indexed_revision"] == 11
    assert engine.files == []
    assert engine.deleted == ["django/core/handlers/old.py"]


def test_snapshot_index_skips_records_at_indexed_revision_watermark(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    old_content = "class AlreadyIndexed:\n    pass\n"
    new_content = "class NeedsIndex:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/old.py",
        content=old_content,
        hash_value=_sha(old_content),
        size=len(old_content),
        mtime_ms=123,
        encoding="utf-8",
        revision=3,
    )
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/new.py",
        content=new_content,
        hash_value=_sha(new_content),
        size=len(new_content),
        mtime_ms=124,
        encoding="utf-8",
        revision=7,
    )
    assert store.mark_indexed(workspace_id="repo-a", revision=5) == 5

    def _read_record_text(*, workspace_id, record):
        assert workspace_id == "repo-a"
        if record.path == "django/core/handlers/old.py":
            raise AssertionError("record at indexed revision should be skipped")
        return new_content

    class _Engine:
        def __init__(self) -> None:
            self.files = []

        def indexed_file_hashes(self, *, workspace_id=None):
            assert workspace_id == "repo-a"
            return {}

        async def upsert_contents(self, files, *, refresh=False):
            self.files.extend(files)
            return len(files)

        def refresh_stats(self):
            return None

        def get_stats(self):
            return {"total_files": 1}

    engine = _Engine()
    monkeypatch.setattr(store, "read_record_text", _read_record_text)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: engine)

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a"},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["records_seen"] == 2
    assert result["indexed_files"] == 1
    assert result["skipped_unchanged"] == 0
    assert result["skipped_by_indexed_revision"] == 1
    assert result["indexed_revision_watermark"] == 5
    assert result["indexed_revision"] == 7
    assert engine.files == [
        (
            "django/core/handlers/new.py",
            new_content,
            {
                "content_hash": _sha(new_content),
                "snapshot_hash": _sha(new_content),
                "snapshot_revision": 7,
                "workspace_id": "repo-a",
            },
        )
    ]


def test_snapshot_index_background_returns_job_without_blocking(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: object())
    monkeypatch.setattr(
        search_router,
        "_start_snapshot_index_job",
        lambda workspace_id, *, force=False, scope="semantic": {
            "job_id": "repo-a:1",
            "workspace_id": workspace_id,
            "state": "running",
            "force": force,
            "scope": scope,
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/index",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"workspace_id": "repo-a", "background": True, "force": True},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["background"] is True
    assert result["job"] == {
        "job_id": "repo-a:1",
        "workspace_id": "repo-a",
        "state": "running",
        "force": True,
        "scope": "semantic",
    }


def test_snapshot_index_status_reports_background_job(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    with search_router._SNAPSHOT_INDEX_JOBS_LOCK:
        search_router._SNAPSHOT_INDEX_JOBS["repo-a"] = {
            "job_id": "repo-a:status",
            "workspace_id": "repo-a",
            "state": "completed",
            "thread": object(),
            "result": {"indexed_revision": 9},
        }

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)
    try:
        response = client.get(
            "/search/index/status",
            headers={"X-Omnicode-Workspace": "repo-a"},
            params={"workspace_id": "repo-a"},
        )
    finally:
        with search_router._SNAPSHOT_INDEX_JOBS_LOCK:
            search_router._SNAPSHOT_INDEX_JOBS.pop("repo-a", None)

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["state"] == "completed"
    assert result["job"]["job_id"] == "repo-a:status"
    assert "thread" not in result["job"]


def test_symbol_search_reads_snapshot_records_without_index_reload(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    def _read_text_should_not_run(**_kwargs):
        raise AssertionError("symbol snapshot scan should not reload index per file")

    monkeypatch.setattr(store, "read_text", _read_text_should_not_run)

    class _Engine:
        async def search(self, _request):
            raise AssertionError("snapshot exact symbol should not call search engine")

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "snapshot_required_revision": 11,
            "semantic_fresh": False,
            "snapshot_fresh": True,
            "semantic_stale": True,
            "freshness": "snapshot_fresh",
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"query": "BaseHandler", "max_results": 10},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["result"]["fuzzy_enabled"] is True
    assert body["result"]["snapshot_fast_path"] is True
    assert body["result"]["results"][0]["symbol_name"] == "BaseHandler"


def test_symbol_search_stale_check_runs_before_engine(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: {
            "ok": False,
            "success": False,
            "stale": True,
            "error": "Cloud index is stale",
            "workspace_id": workspace_id,
            "required_revision": min_revision,
        },
    )
    monkeypatch.setattr(
        search_router,
        "get_search_engine",
        lambda: (_ for _ in ()).throw(
            AssertionError("stale symbol search should not touch engine")
        ),
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "42",
        },
        params={"query": "BaseHandler", "fuzzy": False, "max_results": 10},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    assert body["stale"] is True
    assert body["required_revision"] == 42


def test_symbol_search_allows_snapshot_fresh_when_semantic_index_lags(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Engine:
        async def search(self, _request):
            raise AssertionError("snapshot exact symbol should not use stale engine")

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "snapshot_required_revision": 11,
            "semantic_fresh": False,
            "snapshot_fresh": True,
            "semantic_stale": True,
            "freshness": "snapshot_fresh",
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
        params={"query": "BaseHandler", "fuzzy": False, "max_results": 10},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["snapshot_fast_path"] is True
    assert result["freshness"] == "snapshot_fresh"
    assert result["semantic_stale"] is True
    assert result["accepted_revision"] == 11
    assert result["indexed_revision"] == 0
    assert result["results"][0]["file_path"] == "django/core/handlers/base.py"


def test_semantic_search_boosts_exact_snapshot_symbol_above_noise(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Engine:
        async def search(self, _request):
            return [
                SimpleNamespace(
                    file_path="django/contrib/gis/admin/options.py",
                    symbol_name="BaseHandlerAdmin",
                    chunk_type="class",
                    line_start=1,
                    line_end=20,
                    signature="class BaseHandlerAdmin:",
                    docstring="",
                    relevance_score=0.99,
                    why_matched=["semantic"],
                )
            ]

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={"query": "BaseHandler", "search_type": "semantic", "max_results": 3},
    )

    body = response.json()
    result = body["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["snapshot_store_used"] is True
    assert result["snapshot_exact_boost"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["symbol_name"] == "BaseHandler"
    assert first["source"] == "snapshot_store"
    assert "symbol:exact" in first["why_matched"]
    assert "semantic:exact_boost" in first["why_matched"]
    assert result["results"][1]["file_path"] == "django/contrib/gis/admin/options.py"


def test_semantic_search_uses_exact_index_boost_before_snapshot_scan(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content = "class BaseHandler:\n    pass\n"
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    class _Engine:
        async def search(self, _request):
            return [
                SimpleNamespace(
                    file_path="django/contrib/gis/admin/options.py",
                    symbol_name="BaseHandlerAdmin",
                    chunk_type="class",
                    line_start=1,
                    line_end=20,
                    signature="class BaseHandlerAdmin:",
                    docstring="",
                    relevance_score=0.99,
                    why_matched=["semantic"],
                )
            ]

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={"query": "BaseHandler", "search_type": "semantic", "max_results": 3},
    )

    result = response.json()["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] == "exact_index"
    assert first["rank_reason"] == "exact_symbol_before_semantic"
    assert "semantic:exact_boost" in first["why_matched"]


def test_semantic_search_boosts_snapshot_lexical_overlap_above_noise(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = (
        "class BaseHandler:\n"
        "    def load_middleware(self):\n"
        "        self._middleware_chain = self._get_response\n"
        "        # Request handling builds the configured middleware chain.\n"
    )
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Engine:
        async def search(self, _request):
            return [
                SimpleNamespace(
                    file_path="tests/auth_tests/test_remote_user.py",
                    symbol_name="",
                    chunk_type="text",
                    line_start=10,
                    line_end=10,
                    signature="request middleware",
                    docstring="",
                    relevance_score=0.99,
                    why_matched=["semantic"],
                )
            ]

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search",
        headers={"X-Omnicode-Workspace": "repo-a"},
        json={
            "query": "request middleware chain",
            "search_type": "semantic",
            "max_results": 3,
        },
    )

    body = response.json()
    result = body["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["snapshot_lexical_boost"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] == "snapshot_store"
    assert "semantic:lexical_boost" in first["why_matched"]
    assert set(first["matched_tokens"]) >= {"request", "middleware", "chain"}


def test_symbol_search_uses_exact_index_before_semantic_engine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content = "class BaseHandler:\n    pass\n"
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    class _Engine:
        async def search(self, _request):  # pragma: no cover - should not run
            raise AssertionError("semantic engine should not be called")

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "freshness": "exact_fresh",
            "accepted_revision": 11,
            "indexed_revision": 7,
            "exact_indexed_revision": 11,
            "semantic_stale": True,
            "exact_stale": False,
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols?query=BaseHandler",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
    )

    result = response.json()["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert result["exact_index_used"] is True
    assert result["snapshot_fast_path"] is True
    assert result["freshness"] == "exact_fresh"
    assert result["semantic_stale"] is True
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] == "exact_index"
    assert first["symbol_name"] == "BaseHandler"


def test_text_search_uses_exact_index_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EXACT_LINE_FTS", "true")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content = "class BaseHandler:\n    MARKER = 'middleware-chain'\n"
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "freshness": "exact_fresh",
            "accepted_revision": 11,
            "indexed_revision": 7,
            "exact_indexed_revision": 11,
            "semantic_stale": True,
            "exact_stale": False,
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/text",
        params={"query": "middleware-chain", "max_results": 1},
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
    )

    result = response.json()["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert result["exact_index_used"] is True
    assert result["exact_line_fts_available"] is True
    assert result["freshness"] == "exact_fresh"
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] == "exact_index"


def test_text_search_skips_exact_line_scan_without_fts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OMNICODE_EXACT_LINE_FTS", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content = "class BaseHandler:\n    MARKER = 'middleware-chain'\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    def fail_slow_scan(**_kwargs):
        raise AssertionError("slow exact text scan should be skipped without FTS")

    exact.search_text = fail_slow_scan  # type: ignore[method-assign]
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: None,
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/text",
        params={"query": "class BaseHandler:", "max_results": 1},
        headers={"X-Omnicode-Workspace": "repo-a"},
    )

    result = response.json()["result"]
    first = result["results"][0]
    assert response.status_code == 200
    assert result["exact_index_used"] is False
    assert result["exact_line_fts_available"] is False
    assert first["file_path"] == "django/core/handlers/base.py"
    assert first["source"] in {"snapshot_mirror", "snapshot_store"}
    assert "symbol_prioritized" in first["why_matched"]


def test_intelligence_context_promotes_same_file_after_snapshot_anchor(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Composer:
        def __init__(self, working_dir: str) -> None:
            assert working_dir == str(workspace)

        async def build(self, **_kwargs):
            return SimpleNamespace(
                to_dict=lambda: {
                    "request": {},
                    "capability_status": [
                        {
                            "capability": "llm_enhancement",
                            "available": True,
                            "detail": "raw router state",
                            "backend": "test",
                        }
                    ],
                    "code_understanding": {"symbol_count": 0},
                    "search": {
                        "query": "BaseHandler",
                        "result_count": 2,
                        "results": [
                            {
                                "file": "django/contrib/gis/admin/options.py",
                                "start_line": 1,
                                "snippet": "semantic noise",
                            },
                            {
                                "file": "django/core/handlers/base.py",
                                "start_line": 20,
                                "snippet": "same file context",
                            },
                        ],
                    },
                    "impact": {},
                    "memory": {},
                    "git_history": {},
                    "advisories": [],
                    "token_estimate": 0,
                    "token_budget": 2000,
                    "elapsed_ms": 0,
                    "errors": {},
                }
            )

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        intelligence_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(intelligence_router, "IntelligenceComposer", _Composer)
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "snapshot_required_revision": 11,
            "semantic_fresh": False,
            "snapshot_fresh": True,
            "semantic_stale": True,
            "freshness": "snapshot_fresh",
        },
    )

    app = FastAPI()
    app.include_router(intelligence_router.router)
    client = TestClient(app)

    response = client.post(
        "/intelligence/context",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
        json={
            "file_path": "django/core/handlers/base.py",
            "symbol": "BaseHandler",
            "query": "BaseHandler",
            "include_memory": False,
        },
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["snapshot_exact_symbol"] is True
    assert result["freshness"] == "snapshot_fresh"
    assert result["search"]["results"][0]["file"] == "django/core/handlers/base.py"
    assert result["search"]["results"][1]["file"] == "django/core/handlers/base.py"
    assert result["search"]["results"][2]["file"] == "django/contrib/gis/admin/options.py"
    assert result["search"]["results"][0]["source"] == "snapshot_store"
    assert result["context_quality"]["primary_anchor"] == "snapshot_exact_symbol"
    assert result["context_quality"]["same_file_results_promoted"] == 1
    assert result["context_quality"]["semantic_noise_demoted"] == 1
    assert result["code_understanding"]["symbols"][0]["name"] == "BaseHandler"
    assert result["impact"]["impact_status"] == "unknown"
    assert result["impact"]["graph_available"] is False
    assert result["llm"]["available"] is False
    assert result["capability_status"][0]["available"] is False


def test_intelligence_context_snapshot_fast_path_skips_composer(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Composer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("snapshot fast path should not build composer")

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        intelligence_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(intelligence_router, "IntelligenceComposer", _Composer)
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "semantic_stale": True,
            "freshness": "exact_fresh",
        },
    )

    app = FastAPI()
    app.include_router(intelligence_router.router)
    client = TestClient(app)

    response = client.post(
        "/intelligence/context",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
        json={
            "file_path": "django/core/handlers/base.py",
            "symbol": "BaseHandler",
            "query": "BaseHandler",
            "include_memory": False,
            "include_git_history": False,
        },
    )

    result = response.json()["result"]
    assert response.status_code == 200
    assert result["context_fast_path"] is True
    assert result["snapshot_exact_symbol"] is True
    assert result["search"]["results"][0]["file"] == "django/core/handlers/base.py"
    assert result["impact"]["impact_status"] == "unknown"
    assert result["memory"]["skipped"] is True
    assert result["git_history"]["skipped"] is True


def test_intelligence_context_fast_path_uses_exact_index_before_snapshot_scan(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content = "class BaseHandler:\n    pass\n"
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "django/core/handlers/base.py",
                "hash": _sha(content),
                "size": len(content),
                "content": content,
            }
        ],
        deleted_paths=[],
        revision=11,
    )

    class _Composer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("snapshot fast path should not build composer")

    def _snapshot_scan_should_not_run(**_kwargs):
        raise AssertionError("snapshot scan should not run when exact index hits")

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(
        search_router,
        "_snapshot_symbol_search",
        _snapshot_scan_should_not_run,
    )
    monkeypatch.setattr(
        intelligence_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(intelligence_router, "IntelligenceComposer", _Composer)
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "semantic_stale": True,
            "freshness": "exact_fresh",
        },
    )

    app = FastAPI()
    app.include_router(intelligence_router.router)
    client = TestClient(app)

    response = client.post(
        "/intelligence/context",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
        json={
            "file_path": "django/core/handlers/base.py",
            "symbol": "BaseHandler",
            "query": "BaseHandler",
            "include_memory": False,
            "include_git_history": False,
        },
    )

    result = response.json()["result"]
    assert response.status_code == 200
    assert result["context_fast_path"] is True
    assert result["search"]["results"][0]["source"] == "exact_index"
    assert result["context_quality"]["primary_anchor"] == "snapshot_exact_symbol"


def test_intelligence_context_include_memory_uses_composer(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )
    called = {"composer": False}

    class _Composer:
        def __init__(self, working_dir: str) -> None:
            assert working_dir == str(workspace)

        async def build(self, **_kwargs):
            called["composer"] = True
            return SimpleNamespace(
                to_dict=lambda: {
                    "request": {},
                    "capability_status": [],
                    "code_understanding": {"symbol_count": 0},
                    "search": {"query": "BaseHandler", "results": []},
                    "impact": {},
                    "memory": {"rows": []},
                    "git_history": {},
                    "advisories": [],
                    "token_estimate": 0,
                    "token_budget": 2000,
                    "elapsed_ms": 0,
                    "errors": {},
                }
            )

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        intelligence_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(intelligence_router, "IntelligenceComposer", _Composer)
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        intelligence_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "workspace_id": workspace_id,
            "accepted_revision": 11,
            "indexed_revision": 0,
            "required_revision": 11,
            "semantic_stale": True,
            "freshness": "exact_fresh",
        },
    )

    app = FastAPI()
    app.include_router(intelligence_router.router)
    client = TestClient(app)

    response = client.post(
        "/intelligence/context",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "11",
        },
        json={
            "file_path": "django/core/handlers/base.py",
            "symbol": "BaseHandler",
            "query": "BaseHandler",
            "include_memory": True,
            "include_git_history": False,
        },
    )

    result = response.json()["result"]
    assert response.status_code == 200
    assert called["composer"] is True
    assert result.get("context_fast_path") is not True
    assert result["snapshot_exact_symbol"] is True


def test_graph_risk_unknown_when_snapshot_symbol_has_no_graph_evidence(
    tmp_path: Path, monkeypatch,
) -> None:
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Analyzer:
        async def assess_risk(self, **_kwargs):
            return {
                "symbol": "BaseHandler",
                "risk": "low",
                "risk_score": 2,
                "reasons": ["No test coverage found"],
                "direct_callers": 0,
                "files_affected": 0,
                "test_coverage": 0,
                "suggested_checks": [],
            }

    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(graph_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(graph_router, "_build", lambda: _Analyzer())

    app = FastAPI()
    app.include_router(graph_router.router)
    client = TestClient(app)

    response = client.get(
        "/graph/risk",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"symbol": "BaseHandler", "max_files": 200},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["risk"] == "unknown"
    assert result["graph_available"] is False
    assert result["confidence"] == "low"
    assert result["symbol_source"] == "snapshot_store"


def test_graph_impact_marks_snapshot_graph_unavailable(
    tmp_path: Path, monkeypatch,
) -> None:
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    content = "class BaseHandler:\n    pass\n"
    store.upsert(
        workspace_id="repo-a",
        path="django/core/handlers/base.py",
        content=content,
        hash_value=_sha(content),
        size=len(content),
        mtime_ms=123,
        encoding="utf-8",
        revision=11,
    )

    class _Analyzer:
        async def get_impact_radius(self, **_kwargs):
            return {
                "symbol": "BaseHandler",
                "depth": 2,
                "affected_symbols": [],
                "dependent_symbols": [],
                "affected_count": 0,
                "dependent_count": 0,
                "files_involved": [],
                "files_count": 0,
                "total_blast_radius": 1,
            }

    monkeypatch.setitem(sys.modules, "api.v1.routers.search", search_router)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(graph_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(graph_router, "_build", lambda: _Analyzer())

    app = FastAPI()
    app.include_router(graph_router.router)
    client = TestClient(app)

    response = client.get(
        "/graph/impact",
        headers={"X-Omnicode-Workspace": "repo-a"},
        params={"symbol": "BaseHandler", "depth": 2, "max_files": 200},
    )

    body = response.json()
    result = body["result"]
    assert response.status_code == 200
    assert body["success"] is True
    assert result["impact_status"] == "unknown"
    assert result["graph_available"] is False
    assert result["symbol_found"] is True


def test_symbol_search_isolates_exact_index_by_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    content_a = "class RepoAOnly:\n    pass\n"
    content_b = "class RepoBOnly:\n    pass\n"
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[
            {
                "path": "pkg/a.py",
                "hash": _sha(content_a),
                "size": len(content_a),
                "content": content_a,
            }
        ],
        deleted_paths=[],
        revision=1,
    )
    exact.update_batch(
        workspace_id="repo-b",
        changed_files=[
            {
                "path": "pkg/b.py",
                "hash": _sha(content_b),
                "size": len(content_b),
                "content": content_b,
            }
        ],
        deleted_paths=[],
        revision=1,
    )

    current_root = {"path": str(repo_a)}
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo-a",
        path=str(repo_a),
        set_active=True,
        workspace_id="repo-a",
    )
    registry.add(
        name="repo-b",
        path=str(repo_b),
        workspace_id="repo-b",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=current_root["path"]),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "freshness": "exact_fresh",
            "accepted_revision": 1,
            "indexed_revision": 0,
            "exact_indexed_revision": 1,
            "semantic_stale": True,
            "exact_stale": False,
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response_a = client.post(
        "/search/symbols",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "1",
        },
        params={"query": "RepoBOnly", "max_results": 5},
    )
    assert response_a.status_code == 200
    assert response_a.json()["result"]["results"] == []

    current_root["path"] = str(repo_b)
    response_b = client.post(
        "/search/symbols",
        headers={
            "X-Omnicode-Workspace": "repo-b",
            "X-Omnicode-Min-Revision": "1",
        },
        params={"query": "RepoBOnly", "max_results": 5},
    )
    result_b = response_b.json()["result"]

    assert response_b.status_code == 200
    assert result_b["results"][0]["file_path"] == "pkg/b.py"
    assert result_b["results"][0]["symbol_name"] == "RepoBOnly"


def test_move_sync_removes_old_path_from_snapshot_and_exact_index(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CloudSnapshotStore(root=tmp_path / "state" / "cloud-sync")
    exact = SnapshotExactIndex(store=store)
    old_content = "class MovedThing:\n    pass\n"
    new_content = "class MovedThing:\n    VALUE = 'new-path'\n"
    old_file = {
        "path": "pkg/old_location.py",
        "hash": _sha(old_content),
        "size": len(old_content),
        "mtime_ms": 100,
        "encoding": "utf-8",
        "content": old_content,
    }
    new_file = {
        "path": "pkg/new_location.py",
        "hash": _sha(new_content),
        "size": len(new_content),
        "mtime_ms": 200,
        "encoding": "utf-8",
        "content": new_content,
    }
    store.apply_batch(
        workspace_id="repo-a",
        files=[old_file],
        deletes=[],
        revision=1,
    )
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[old_file],
        deleted_paths=[],
        revision=1,
    )
    store.apply_batch(
        workspace_id="repo-a",
        files=[new_file],
        deletes=["pkg/old_location.py"],
        revision=2,
    )
    exact.update_batch(
        workspace_id="repo-a",
        changed_files=[new_file],
        deleted_paths=["pkg/old_location.py"],
        revision=2,
    )

    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo-a",
        path=str(workspace),
        set_active=True,
        workspace_id="repo-a",
    )
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(workspace)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "_exact_index", lambda: exact)
    monkeypatch.setattr(search_router, "CloudSnapshotStore", lambda: store)
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_error",
        lambda *, workspace_id, min_revision, **_kwargs: None,
    )
    monkeypatch.setattr(
        search_router,
        "cloud_freshness_state",
        lambda *, workspace_id, min_revision: {
            "freshness": "exact_fresh",
            "accepted_revision": 2,
            "indexed_revision": 0,
            "exact_indexed_revision": 2,
            "semantic_stale": True,
            "exact_stale": False,
        },
    )

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={
            "X-Omnicode-Workspace": "repo-a",
            "X-Omnicode-Min-Revision": "2",
        },
        params={"query": "MovedThing", "max_results": 5},
    )
    result = response.json()["result"]
    records = store.list_records(workspace_id="repo-a")

    assert response.status_code == 200
    assert [row["file_path"] for row in result["results"]] == ["pkg/new_location.py"]
    assert [record.path for record in records] == ["pkg/new_location.py"]
    assert store.read_text(workspace_id="repo-a", path="pkg/old_location.py") is None
    assert (
        store.workspaces_root
        / "repo-a"
        / "mirror"
        / "pkg"
        / "old_location.py"
    ).exists() is False


def test_symbol_search_rejects_workspace_header_for_other_root(
    tmp_path: Path, monkeypatch,
) -> None:
    active = tmp_path / "active"
    other = tmp_path / "other"
    active.mkdir()
    other.mkdir()
    registry = WorkspaceRegistry(store_path=tmp_path / "workspaces.json")
    registry.add(
        name="repo-a",
        path=str(active),
        set_active=True,
        workspace_id="repo-a",
    )
    registry.add(
        name="repo-b",
        path=str(other),
        workspace_id="repo-b",
    )

    class _Engine:
        async def search(self, _request):
            raise AssertionError("search should be blocked before engine use")

    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(WORKING_DIR=str(active)),
    )
    monkeypatch.setattr(search_router, "get_workspace_registry", lambda: registry)
    monkeypatch.setattr(search_router, "get_search_engine", lambda: _Engine())

    app = FastAPI()
    app.include_router(search_router.router)
    client = TestClient(app)

    response = client.post(
        "/search/symbols",
        headers={"X-Omnicode-Workspace": "repo-b"},
        params={"query": "cleanroom_only_marker"},
    )

    assert response.status_code == 409
    assert "active backend WORKING_DIR" in response.json()["detail"]
