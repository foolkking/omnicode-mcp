"""Contract tests for the omni_status runtime self-check tool.

omni_status exists so a human auditor can verify the live MCP host is
running the same code as the on-disk source + unit tests. The audit bug
that motivated this tool: omni_search picked up its source/confidence
fix on restart but omni_read kept serving the pre-fix diagnostics
schema, because FastMCP's per-tool registration was partial.

These tests pin:

* every required field is present
* warnings is empty when source + runtime agree
* a missing flagship tool surfaces in warnings
* a missing handler feature surfaces in warnings (regression guard)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_adapters.mcp_server.high_level_tools import (
    register_high_level_tools,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ToolManagerStub:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}


class _MCPStub:
    """Mimics FastMCP enough for omni_status to introspect it."""

    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}
        self._tool_manager = _ToolManagerStub()

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            self._tool_manager._tools[fn.__name__] = fn
            return fn

        return deco

    async def list_tools(self) -> list:  # pragma: no cover - fallback path
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


async def _noop_make_request(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    return {}


def _build_status_tool() -> Callable[..., Any]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    fn = mcp.tools.get("omni_status")
    assert fn is not None, "omni_status was not registered"
    return fn


def _build_status_tool_with_request(
    make_request: Callable[..., Any],
) -> Callable[..., Any]:
    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    fn = mcp.tools.get("omni_status")
    assert fn is not None, "omni_status was not registered"
    return fn


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_omni_status_returns_required_fields() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    required = {
        "ok",
        "pid",
        "process_start_time",
        "module_path",
        "module_sha1",
        "module_mtime",
        "python_executable",
        "python_version",
        "handler_version",
        "handler_features",
        "backend_url",
        "status_probe_timeout_seconds",
        "registered_tools",
        "deprecated_aliases_present",
        "warnings",
    }
    missing = required - set(payload.keys())
    assert not missing, f"omni_status missing fields: {missing}"

    # Sanity on a few values.
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert payload["module_path"].endswith("high_level_tools.py")
    assert len(payload["module_sha1"]) == 40  # full sha1 hex
    assert payload["handler_version"] == hlt._HANDLER_VERSION
    assert isinstance(payload["registered_tools"], list)
    assert payload["status_probe_timeout_seconds"] >= 0.05
    assert "omni_status" in payload["registered_tools"]
    assert "sync" in payload
    assert isinstance(payload["sync"], dict)
    assert "capability_contract" in payload
    assert isinstance(payload["capability_contract"], dict)
    assert "agent_auto" in payload
    assert isinstance(payload["agent_auto"], dict)


def test_omni_status_compact_detail_omits_heavy_fields() -> None:
    raw = _run(_build_status_tool()("compact"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["detail"] == "compact"
    assert payload["handler_version"] == hlt._HANDLER_VERSION
    assert "handler_features" not in payload
    assert "expected_contract_versions" not in payload
    assert "routes" not in payload["sync"]
    assert isinstance(payload["handler_features_count"], int)
    assert payload["registered_tools_count"] >= 13
    assert "sync" in payload
    assert "embedding" in payload
    assert "local_cache_available" in payload["embedding"]
    assert "capabilities" in payload
    assert "toolchains" in payload
    assert "java" in payload["toolchains"]
    assert "scala" in payload["toolchains"]
    assert payload["contract_version"] == "status.v1"


def test_omni_status_compact_skips_search_stats_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODE", "cloud")

    endpoints: list[str] = []

    async def cloud_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        endpoints.append(endpoint)
        if endpoint == "/sync/status":
            return {
                "ok": True,
                "accepted_revision": 11,
                "indexed_revision": 11,
                "exact_index_ready": True,
                "exact_query_safe": True,
                "semantic_query_safe": False,
                "strict_semantic_safe": False,
                "recommended_query_mode": "exact_first",
                "snapshot_store": {
                    "latest_revision": 11,
                    "accepted_revision": 11,
                    "indexed_revision": 11,
                    "files": 2,
                    "deletes": 0,
                },
            }
        if endpoint == "/search/stats":
            raise AssertionError("compact status must not call /search/stats")
        if endpoint == "/read":
            raise AssertionError("compact status must not call /read root probe")
        return {}

    raw = _run(_build_status_tool_with_request(cloud_request)("compact"))
    payload = json.loads(raw)

    assert "/sync/status" in endpoints
    assert "/read" not in endpoints
    assert "/search/stats" not in endpoints
    assert payload["detail"] == "compact"
    assert payload["sync"]["semantic_query_safe"] is False
    assert payload["embedding"]["available"] is False
    assert payload["embedding"]["runtime_available"] is False
    assert (
        payload["embedding"]["error_code"]
        == "semantic_runtime_not_probed_in_compact_status"
    )
    assert payload["capability_contract"]["embedding"]["available"] is False
    assert not any(
        "backend root probe failed" in warning
        for warning in payload["warnings"]
    )


def test_omni_status_compact_uses_sync_semantic_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_snapshot = (
        tmp_path
        / "models"
        / "models--BAAI--bge-small-en-v1.5"
        / "snapshots"
        / "snapshot"
    )
    cache_snapshot.mkdir(parents=True)
    (cache_snapshot / "config.json").write_text("{}", encoding="utf-8")
    (cache_snapshot / "modules.json").write_text("[]", encoding="utf-8")
    (cache_snapshot / "model.safetensors").write_bytes(b"stub")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODE", "cloud")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    async def cloud_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if endpoint == "/sync/status":
            return {
                "ok": True,
                "accepted_revision": 11,
                "indexed_revision": 11,
                "exact_index_ready": True,
                "exact_query_safe": True,
                "semantic_index_ready": False,
                "semantic_query_safe": False,
                "strict_semantic_safe": False,
                "semantic_runtime_ready": True,
                "semantic_runtime": {
                    "ready": True,
                    "embedding_available": True,
                    "model": "BAAI/bge-small-en-v1.5",
                    "dimension": 384,
                    "faiss_dimension": 384,
                    "chunker_version": "ast-chunker.v1",
                    "vector_count": 12,
                    "stale": False,
                    "invalid": False,
                    "stale_reason": None,
                    "metadata": {
                        "embedding_model": "BAAI/bge-small-en-v1.5",
                        "embedding_dimension": 384,
                        "chunker_version": "ast-chunker.v1",
                    },
                },
                "snapshot_store": {
                    "latest_revision": 11,
                    "accepted_revision": 11,
                    "indexed_revision": 11,
                    "files": 2,
                    "deletes": 0,
                },
            }
        if endpoint == "/search/stats":
            raise AssertionError("compact status must not call /search/stats")
        return {}

    raw = _run(_build_status_tool_with_request(cloud_request)("compact"))
    payload = json.loads(raw)

    assert payload["detail"] == "compact"
    assert payload["sync"]["semantic_runtime_ready"] is True
    assert payload["sync"]["semantic_query_safe"] is False
    assert payload["embedding"]["runtime_source"] == "cloud_semantic_index"
    assert payload["embedding"]["runtime_available"] is True
    assert payload["embedding"]["available"] is True
    assert payload["embedding"]["error_code"] is None
    assert payload["capability_contract"]["embedding"]["available"] is True


def test_omni_status_clean_when_source_and_runtime_agree() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert payload["warnings"] == [], payload["warnings"]
    assert payload["ok"] is True


def test_omni_status_lists_flagship_tools() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    flagship = {
        "omni_search", "omni_read", "omni_impact",
        "omni_diagnostics", "omni_patch", "omni_memory",
        "omni_context", "omni_skill", "omni_index", "discover_tools",
        "omni_status",
    }
    missing = flagship - set(payload["registered_tools"])
    assert not missing, missing


def test_omni_status_flags_missing_flagship_tool() -> None:
    """If a flagship tool isn't registered, warnings must surface it.

    Simulate by deleting omni_read from the registry after registration.
    """
    mcp = _MCPStub()
    register_high_level_tools(mcp, _noop_make_request)
    # Sabotage: remove omni_read from the live registry.
    mcp._tool_manager._tools.pop("omni_read", None)
    status_fn = mcp.tools["omni_status"]
    raw = _run(status_fn())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert any(
        w.startswith("flagship_tools_missing:") and "omni_read" in w
        for w in payload["warnings"]
    ), payload["warnings"]


def test_omni_status_handler_features_match_module_constant() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert tuple(payload["handler_features"]) == hlt._HANDLER_FEATURES


def test_omni_status_advertises_pending_drain_contract() -> None:
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    assert payload["handler_version"] == hlt._HANDLER_VERSION
    assert "sync.pending_force_after_local_patch" in payload["handler_features"]


def test_omni_status_compact_surfaces_line_fts_policy() -> None:
    raw = _run(_build_status_tool()(detail="compact"))
    payload = json.loads(raw)

    local_index = payload["local_index"]
    assert "local_line_fts_mode" in local_index
    assert "local_line_fts_auto_line_limit" in local_index
    assert "local_line_fts_reason" in local_index


def test_omni_status_pid_matches_current_process() -> None:
    import os
    raw = _run(_build_status_tool()())
    payload = json.loads(raw)
    assert payload["pid"] == os.getpid()


def test_omni_status_sync_defaults_are_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNICODE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("OMNICODE_EXECUTOR_MODE", raising=False)
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.delenv("OMNICODE_FASTAPI_BASE_URL", raising=False)

    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["sync"]["configured"] is False
    assert payload["sync"]["warning"] is None
    assert payload["sync"]["routes"]["omni_read"]["local_authority"] is True
    assert payload["sync"]["routes"]["omni_status"]["target"] == "aggregate"


def test_omni_status_sync_reports_hybrid_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")

    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    sync = payload["sync"]
    assert sync["configured"] is True
    assert sync["workspace_id"] == "repo-a"
    assert sync["executor_mode"] == "hybrid"
    assert payload["backend_url"] == "http://cloud"
    assert sync["backend_url"] == "http://cloud"
    assert sync["routes"]["omni_read"]["target"] == "local"
    assert sync["routes"]["omni_read"]["local_authority"] is True
    assert sync["routes"]["omni_patch"]["local_authority"] is True
    assert sync["routes"]["omni_search"]["target"] == "cloud"
    assert sync["routes"]["omni_search"]["requires_barrier"] is True
    assert sync["routes"]["omni_search"]["barrier_min_revision"] == 0


def test_omni_status_prefers_cloud_snapshot_for_index_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")

    async def cloud_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if endpoint == "/sync/status":
            return {
                "ok": True,
                "accepted_revision": 42,
                "indexed_revision": 42,
                "exact_indexed_revision": 42,
                "exact_index_ready": True,
                "semantic_index_ready": False,
                "semantic_index_coverage": "exact_only_initial_sync",
                "semantic_initial_exact_only": True,
                "recommended_query_mode": "exact_first",
                "query_mode_reason": "exact_only_initial_sync",
                "supported_query_modes": ["local", "snapshot", "exact_text", "exact_symbol"],
                "exact_query_safe": True,
                "strict_semantic_safe": False,
                "search_degraded": True,
                "exact_index": {
                    "files": 6991,
                    "symbols": 45279,
                    "lines": 1189358,
                    "line_fts_available": False,
                },
                "snapshot_store": {
                    "latest_revision": 42,
                    "accepted_revision": 42,
                    "indexed_revision": 42,
                    "files": 6991,
                    "deletes": 0,
                },
            }
        return {}

    raw = _run(_build_status_tool_with_request(cloud_request)())
    payload = json.loads(raw)

    sync = payload["sync"]
    assert sync["accepted_revision"] == 42
    assert sync["indexed_revision"] == 42
    assert sync["snapshot_store_source"] == "cloud"
    assert sync["snapshot_store"]["files"] == 6991
    readiness = sync["index_readiness"]
    assert readiness["fresh"] is True
    assert readiness["indexed_files"] == 6991
    assert readiness["text_index_ready"] is True
    assert readiness["symbol_index_ready"] is True
    assert readiness["exact_index_ready"] is True
    assert readiness["semantic_index_ready"] is False
    assert readiness["recommended_query_mode"] == "exact_first"
    assert readiness["query_mode_reason"] == "exact_only_initial_sync"
    assert readiness["strict_semantic_safe"] is False
    assert readiness["exact_query_safe"] is True
    assert readiness["semantic_index_coverage"] == "exact_only_initial_sync"
    assert readiness["graph_index_ready"] is False


def test_omni_status_surfaces_backend_semantic_metadata_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")

    async def cloud_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if endpoint == "/sync/status":
            return {
                "ok": True,
                "accepted_revision": 7,
                "indexed_revision": 7,
                "exact_index_ready": True,
                "semantic_index_ready": True,
                "exact_query_safe": True,
                "strict_semantic_safe": True,
                "snapshot_store": {
                    "latest_revision": 7,
                    "accepted_revision": 7,
                    "indexed_revision": 7,
                    "files": 2,
                    "deletes": 0,
                },
            }
        if endpoint == "/search/stats":
            return {
                "index_stats": {},
                "semantic_index": {
                    "semantic_index_ready": False,
                    "semantic_index_model": "sentence-transformers/all-MiniLM-L6-v2",
                    "semantic_index_dimension": 384,
                    "faiss_dimension": 384,
                    "semantic_index_stale_reason": "embedding_dimension_mismatch",
                    "semantic_index_invalid": True,
                    "semantic_index_stale": False,
                    "chunker_version": "ast-chunker.v1",
                    "vector_count": 12,
                },
            }
        return {}

    raw = _run(_build_status_tool_with_request(cloud_request)())
    payload = json.loads(raw)

    semantic = payload["sync"]["semantic_index"]
    readiness = payload["sync"]["index_readiness"]
    assert semantic["semantic_index_invalid"] is True
    assert readiness["semantic_index_ready"] is False
    assert readiness["semantic_index_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert readiness["semantic_index_dimension"] == 384
    assert readiness["semantic_index_invalid"] is True
    assert readiness["semantic_index_stale_reason"] == "embedding_dimension_mismatch"
    assert readiness["semantic_index_chunker_version"] == "ast-chunker.v1"
    assert readiness["semantic_vector_count"] == 12
    assert readiness["strict_semantic_safe"] is False
    assert readiness["semantic_query_safe"] is False
    assert readiness["recommended_query_mode"] == "exact_first"
    assert "semantic" not in readiness["supported_query_modes"]
    assert payload["capabilities"]["search.semantic"]["state"] == "unavailable"


def test_omni_status_cloud_embedding_uses_runtime_availability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_snapshot = (
        tmp_path
        / "models"
        / "models--BAAI--bge-small-en-v1.5"
        / "snapshots"
        / "snapshot"
    )
    cache_snapshot.mkdir(parents=True)
    (cache_snapshot / "config.json").write_text("{}", encoding="utf-8")
    (cache_snapshot / "modules.json").write_text("[]", encoding="utf-8")
    (cache_snapshot / "model.safetensors").write_bytes(b"stub")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODE", "cloud")
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("OMNICODE_EMBEDDING_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("OMNICODE_EMBEDDING_LOCAL_FILES_ONLY", "true")

    async def cloud_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if endpoint == "/sync/status":
            return {
                "ok": True,
                "accepted_revision": 9,
                "indexed_revision": 9,
                "exact_index_ready": True,
                "exact_query_safe": True,
                "snapshot_store": {
                    "latest_revision": 9,
                    "accepted_revision": 9,
                    "indexed_revision": 9,
                    "files": 2,
                    "deletes": 0,
                },
            }
        if endpoint == "/search/stats":
            return {
                "index_stats": {},
                "semantic_index": {
                    "semantic_index_ready": False,
                    "semantic_index_model": "BAAI/bge-small-en-v1.5",
                    "semantic_index_dimension": 384,
                    "semantic_index_stale_reason": "EMBEDDING_MODEL_NOT_FOUND",
                    "embedding_available": False,
                    "chunker_version": "ast-chunker.v1",
                    "vector_count": 12,
                },
            }
        return {}

    raw = _run(_build_status_tool_with_request(cloud_request)())
    payload = json.loads(raw)

    embedding = payload["embedding"]
    assert embedding["cached"] is True
    assert embedding["local_cache_available"] is True
    assert embedding["runtime_source"] == "cloud_semantic_index"
    assert embedding["runtime_available"] is False
    assert embedding["available"] is False
    assert embedding["error_code"] == "EMBEDDING_MODEL_NOT_FOUND"
    assert payload["capability_contract"]["embedding"]["available"] is False
    assert payload["capabilities"]["embedding.local"]["state"] == "unavailable"
    assert payload["capabilities"]["search.semantic"]["state"] == "unavailable"


def test_omni_status_reports_cloud_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://127.0.0.1:6799")

    async def down_request(
        _method: str,
        endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if endpoint == "/sync/status":
            return {
                "error": "Request failed with status 502",
                "error_type": "HTTPError",
                "status_code": 502,
            }
        return {}

    raw = _run(_build_status_tool_with_request(down_request)())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert any("cloud_unavailable:" in item for item in payload["warnings"])
    sync = payload["sync"]
    assert sync["cloud_available"] is False
    assert sync["cloud_unavailable"] is True
    assert "502" in sync["cloud_status_warning"]
    assert sync["routes"]["omni_search"]["reason"] == "cloud backend is unavailable"


def test_omni_status_probe_timeout_degrades_quickly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")
    monkeypatch.setenv("OMNICODE_STATUS_PROBE_TIMEOUT", "0.05")

    async def slow_request(
        _method: str,
        _endpoint: str,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        await asyncio.sleep(0.2)
        return {}

    started_at = time.perf_counter()
    raw = _run(_build_status_tool_with_request(slow_request)())
    elapsed = time.perf_counter() - started_at
    payload = json.loads(raw)

    assert elapsed < 1.0
    assert payload["ok"] is False
    assert payload["status_probe_timeout_seconds"] == 0.05
    assert any(
        item.startswith("status_probe_timeout:")
        for item in payload["warnings"]
    )
    sync = payload["sync"]
    assert sync["cloud_available"] is False
    assert sync["cloud_unavailable"] is True
    assert "timed out" in sync["cloud_status_warning"]


def test_omni_status_capability_contract_reports_cloud_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.delenv("OMNICODE_FASTAPI_BASE_URL", raising=False)
    monkeypatch.setenv("OMNICODE_EMBEDDING_MODE", "cloud")
    monkeypatch.setenv("OMNICODE_LLM_MODE", "remote")

    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    contract = payload["capability_contract"]
    assert contract["cloud_configured"] is False
    assert contract["embedding"]["target"] == "cloud"
    assert contract["embedding"]["available"] is False
    assert contract["llm"]["target"] == "cloud"
    assert contract["llm"]["available"] is False


def test_omni_status_agent_auto_reports_embedded_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNICODE_WORKSPACE_ID", "repo-a")
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.delenv("OMNICODE_REMOTE", raising=False)
    monkeypatch.setenv("OMNICODE_FASTAPI_BASE_URL", "http://cloud")
    monkeypatch.setenv("OMNICODE_SYNC_MODE", "smart")
    monkeypatch.setenv("OMNICODE_AGENT_MODE", "auto")

    raw = _run(_build_status_tool()())
    payload = json.loads(raw)

    assert payload["agent_auto"]["target"] == "embedded"
    assert payload["agent_auto"]["should_start"] is True
    assert payload["agent_auto"]["initial_sync"] is True
