from __future__ import annotations

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_core.capabilities.registry import build_runtime_capabilities
from omnicode_core.search.planner import build_search_plan


def _caps(*, local_index: bool = True, semantic: bool = False, graph: bool = False):
    return build_runtime_capabilities(
        cloud_available=False,
        local_index_ready=local_index,
        line_fts_available=local_index,
        embedding_available=semantic,
        semantic_index_ready=semantic,
        graph_index_ready=graph,
    )


def test_stamp_adds_read_capability_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _caps(local_index=True),
    )
    payload = {"ok": True, "file": "pkg/a.py", "mode": "symbol"}

    hlt._stamp(payload, tool="omni_read")

    preflight = payload["capability_preflight"]
    assert preflight["required"] == ["read.symbol"]
    assert preflight["states"]["read.symbol"]["state"] in {"partial", "ready"}
    assert "read.range" in preflight["fallbacks"]
    assert preflight["can_execute"] is True
    assert preflight["execution_policy"]["can_execute"] is True


def test_stamp_adds_impact_degraded_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _caps(local_index=True, graph=False),
    )
    payload = {"ok": True, "symbol": "KnownSymbol"}

    hlt._stamp(payload, tool="omni_impact")

    preflight = payload["capability_preflight"]
    assert preflight["required"] == ["impact.graph"]
    assert preflight["states"]["impact.graph"]["state"] == "degraded"
    assert preflight["degraded"] == ["impact.graph"]
    assert preflight["execution_policy"]["mode"] == "degraded"
    assert preflight["execution_policy"]["can_execute"] is True


def test_stamp_adds_diagnostics_language_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _caps(local_index=True),
    )
    payload = {"ok": True, "file": "core/ReplicaManager.scala", "language": "scala"}

    hlt._stamp(payload, tool="omni_diagnostics")

    preflight = payload["capability_preflight"]
    assert preflight["required"] == ["diagnostics.scala"]
    assert preflight["states"]["diagnostics.scala"]["state"] == "unsupported"
    assert preflight["ready"] is False
    assert preflight["can_execute"] is False
    assert preflight["execution_policy"]["mode"] == "block"
    assert preflight["execution_policy"]["blocking_missing"] == ["diagnostics.scala"]


def test_stamp_adds_patch_validate_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _caps(local_index=True),
    )
    payload = {
        "ok": True,
        "action": "validate",
        "file": "tests/tmp_eval_patch.py",
    }

    hlt._stamp(payload, tool="omni_patch")

    preflight = payload["capability_preflight"]
    assert preflight["required"] == ["patch.safe_edit"]
    assert preflight["states"]["patch.safe_edit"]["state"] == "ready"
    assert "diagnostics.python" in preflight["fallbacks"]
    assert preflight["execution_policy"]["mode"] == "normal"


def test_references_preflight_uses_registered_capability(monkeypatch) -> None:
    monkeypatch.setattr(
        hlt,
        "_runtime_capability_registry_snapshot",
        lambda **_kwargs: _caps(local_index=True),
    )
    plan = build_search_plan(
        query="KnownSymbol",
        requested_mode="references",
        resolved_mode="references",
    )
    payload = {
        "ok": True,
        "query": "KnownSymbol",
        "query_plan": plan.to_dict(),
    }

    hlt._stamp(payload, tool="omni_search")

    preflight = payload["capability_preflight"]
    assert preflight["required"] == ["search.references"]
    assert preflight["states"]["search.references"]["state"] == "degraded"
    assert preflight["execution_policy"]["mode"] == "degraded"
    assert preflight["execution_policy"]["can_execute"] is True
    assert all(
        row.get("reason") != "capability not reported by registry"
        for row in preflight["states"].values()
    )


def test_runtime_snapshot_does_not_treat_configured_backend_as_reachable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNICODE_EXECUTOR_MODE", "hybrid")
    monkeypatch.setenv("OMNICODE_REMOTE", "http://127.0.0.1:6799")

    caps = hlt._runtime_capability_registry_snapshot()

    assert caps["sync.cloud"]["state"] == "unavailable"
    assert caps["read.full"]["state"] == "ready"


def test_runtime_snapshot_honors_explicit_live_cloud_status() -> None:
    caps = hlt._runtime_capability_registry_snapshot(cloud_available=True)

    assert caps["sync.cloud"]["state"] == "ready"
