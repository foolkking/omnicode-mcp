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


def test_discover_ranked_results_do_not_promote_degraded_impact() -> None:
    caps = build_runtime_capabilities(
        cloud_available=True,
        local_index_ready=True,
        line_fts_available=False,
        embedding_available=False,
        semantic_index_ready=False,
        graph_index_ready=False,
    )

    payload = hlt._recommend_tools_payload(
        "safe edit and understand impact before changing search routing",
        matcher="rule",
        capability_registry=caps,
    )

    result_names = [row["name"] for row in payload["results"]]
    assert "omni_impact" in result_names
    assert result_names[0] != "omni_impact"
    impact = next(row for row in payload["results"] if row["name"] == "omni_impact")
    assert impact["safe_to_use_by_default"] is False
    assert impact["capability"] == "impact.graph"
    assert impact["capability_state"] == "degraded"


def test_discover_pipeline_filters_unavailable_context_and_degraded_impact() -> None:
    caps = build_runtime_capabilities(
        cloud_available=True,
        local_index_ready=True,
        line_fts_available=False,
        embedding_available=False,
        semantic_index_ready=False,
        graph_index_ready=False,
    )

    payload = hlt._recommend_tools_payload(
        "understand impact before editing",
        matcher="rule",
        capability_registry=caps,
    )

    pipeline_text = "\n".join(payload["pipeline"] + payload["next_actions"])
    assert "omni_context" not in pipeline_text
    assert "omni_impact" not in pipeline_text
    assert payload["pipeline_kind"] == "deterministic_understanding"
    assert "omni_search" in pipeline_text
    assert "omni_read" in pipeline_text


def test_discover_recommends_lsp_bootstrap_when_jdtls_is_not_started() -> None:
    caps = build_runtime_capabilities(
        cloud_available=False,
        local_index_ready=True,
        line_fts_available=True,
        embedding_available=False,
        semantic_index_ready=False,
        graph_index_ready=False,
        toolchain_status={
            "java": {
                "workspace_diagnostics_ready": False,
                "toolchain_ready": True,
                "reason": "jdtls_not_started",
            },
            "scala": {
                "workspace_diagnostics_ready": False,
                "toolchain_ready": False,
                "reason": "metals_unavailable",
            },
        },
    )

    payload = hlt._recommend_tools_payload(
        "diagnose this Java workspace",
        matcher="rule",
        capability_registry=caps,
    )

    lsp_bootstrap = next(
        row
        for row in payload["required_bootstrap"]
        if row["args"]["scope"] == "lsp"
    )
    assert lsp_bootstrap["languages"] == ["java"]
    assert "omni_index" in payload["recommended_tools"]
    assert (
        caps["diagnostics.java.workspace"]["next_actions"]
        == [
            "omni_index(action='bootstrap', scope='lsp', background=False, format='json')"
        ]
    )
    assert any(
        row.get("capability") == "diagnostics.java.workspace"
        for row in payload["degraded_tools"]
    )
