from __future__ import annotations

from omnicode_adapters.mcp_server import high_level_tools as hlt
from omnicode_core.capabilities.registry import build_runtime_capabilities


def _payload(*, cloud: bool, local_index: bool, semantic: bool, graph: bool):
    caps = build_runtime_capabilities(
        cloud_available=cloud,
        local_index_ready=local_index,
        line_fts_available=local_index,
        embedding_available=semantic,
        semantic_index_ready=semantic,
        graph_index_ready=graph,
    )
    return hlt._recommend_tools_payload(
        "",
        matcher="rule",
        capability_registry=caps,
    )


def test_discover_recommends_index_bootstrap_only_when_local_index_missing() -> None:
    missing = _payload(cloud=False, local_index=False, semantic=False, graph=False)
    ready = _payload(cloud=False, local_index=True, semantic=False, graph=False)

    assert "omni_index" in missing["recommended_tools"]
    assert missing["required_bootstrap"]
    assert "omni_index" not in ready["recommended_tools"]
    assert ready["required_bootstrap"] == []


def test_discover_disables_cloud_sync_when_cloud_unavailable() -> None:
    payload = _payload(cloud=False, local_index=True, semantic=False, graph=False)

    disabled_caps = {row["capability"] for row in payload["disabled_tools"]}
    assert "sync.cloud" in disabled_caps
    assert payload["capability_registry"]["sync.cloud"]["state"] == "unavailable"


def test_discover_recommends_semantic_context_only_when_semantic_ready() -> None:
    unavailable = _payload(cloud=True, local_index=True, semantic=False, graph=False)
    ready = _payload(cloud=True, local_index=True, semantic=True, graph=False)

    disabled_unavailable = {
        row["capability"] for row in unavailable["disabled_tools"]
    }
    assert "search.semantic" in disabled_unavailable
    assert "omni_context" not in unavailable["recommended_tools"]
    assert "omni_context" in ready["recommended_tools"]
    assert "search.semantic" not in {
        row["capability"] for row in ready["disabled_tools"]
    }


def test_discover_recommends_impact_only_when_graph_ready() -> None:
    degraded = _payload(cloud=True, local_index=True, semantic=True, graph=False)
    ready = _payload(cloud=True, local_index=True, semantic=True, graph=True)

    assert "omni_impact" not in degraded["recommended_tools"]
    assert any(row["tool"] == "omni_impact" for row in degraded["degraded_tools"])
    assert "omni_impact" in ready["recommended_tools"]
    assert not any(row.get("tool") == "omni_impact" for row in ready["degraded_tools"])
