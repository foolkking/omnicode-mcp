from __future__ import annotations

from pathlib import Path

from omnicode_core.capabilities.registry import build_runtime_capabilities
from omnicode_core.capabilities.toolchains import detect_workspace_toolchains


def _which_factory(available: set[str]):
    def _which(name: str) -> str | None:
        if name in available:
            return f"C:/tools/{name}.exe"
        return None

    return _which


def _caps(toolchains: dict):
    return build_runtime_capabilities(
        cloud_available=False,
        local_index_ready=True,
        line_fts_available=True,
        embedding_available=False,
        semantic_index_ready=False,
        graph_index_ready=False,
        toolchain_status=toolchains,
    )


def test_toolchain_status_no_tools_does_not_claim_workspace_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    monkeypatch.setattr(
        "omnicode_core.capabilities.toolchains.shutil.which",
        _which_factory(set()),
    )

    status = detect_workspace_toolchains(tmp_path)
    caps = _caps(status)

    assert status["java"]["workspace_diagnostics_ready"] is False
    assert status["java"]["reason"] == "jdtls_unavailable"
    assert status["scala"]["workspace_diagnostics_ready"] is False
    assert status["scala"]["reason"] == "metals_unavailable"
    assert caps["lsp.jdtls"]["state"] == "unavailable"
    assert caps["diagnostics.java"]["state"] == "partial"
    assert caps["diagnostics.java.workspace"]["state"] == "unavailable"
    assert caps["diagnostics.scala"]["state"] == "unsupported"
    assert caps["diagnostics.scala.workspace"]["state"] == "unavailable"


def test_jdtls_and_maven_make_java_workspace_diagnostics_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    monkeypatch.setattr(
        "omnicode_core.capabilities.toolchains.shutil.which",
        _which_factory({"jdtls", "mvn", "javac"}),
    )

    status = detect_workspace_toolchains(tmp_path)
    caps = _caps(status)

    assert status["build_files"]["maven"] is True
    assert status["java"]["toolchain_ready"] is True
    assert status["java"]["workspace_diagnostics_ready"] is False
    assert status["java"]["reason"] == "jdtls_not_started"
    assert caps["lsp.jdtls"]["state"] == "partial"
    assert caps["build.maven"]["state"] == "ready"
    assert caps["diagnostics.java"]["state"] == "partial"
    assert caps["diagnostics.java.workspace"]["state"] == "unavailable"

    running = detect_workspace_toolchains(
        tmp_path,
        lsp_runtime={"java": {"running": True, "initialized": True}},
    )
    running_caps = _caps(running)
    assert running["java"]["workspace_diagnostics_ready"] is True
    assert running["java"]["reason"] == "ready"
    assert running_caps["lsp.jdtls"]["state"] == "ready"
    assert running_caps["diagnostics.java"]["state"] == "ready"
    assert running_caps["diagnostics.java"]["provider"] == "jdtls"
    assert running_caps["diagnostics.java.workspace"]["state"] == "ready"


def test_gradle_wrapper_counts_as_java_build_tool(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "build.gradle").write_text("plugins {}", encoding="utf-8")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "omnicode_core.capabilities.toolchains.shutil.which",
        _which_factory({"jdtls"}),
    )

    status = detect_workspace_toolchains(
        tmp_path,
        lsp_runtime={"java": {"running": True, "initialized": True}},
    )
    caps = _caps(status)

    assert status["build_files"]["gradle"] is True
    assert status["build_files"]["gradle_wrapper"] is True
    assert status["java"]["workspace_diagnostics_ready"] is True
    assert caps["build.gradle"]["state"] == "ready"
    assert caps["diagnostics.java.workspace"]["state"] == "ready"


def test_metals_and_sbt_make_scala_workspace_diagnostics_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "build.sbt").write_text(
        'ThisBuild / scalaVersion := "2.13.14"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "omnicode_core.capabilities.toolchains.shutil.which",
        _which_factory({"metals", "sbt", "scalac"}),
    )

    status = detect_workspace_toolchains(tmp_path)
    caps = _caps(status)

    assert status["build_files"]["sbt"] is True
    assert status["scala"]["toolchain_ready"] is True
    assert status["scala"]["workspace_diagnostics_ready"] is False
    assert status["scala"]["reason"] == "metals_not_started"
    assert caps["lsp.metals"]["state"] == "partial"
    assert caps["build.sbt"]["state"] == "ready"
    assert caps["diagnostics.scala"]["state"] == "unsupported"
    assert caps["diagnostics.scala.workspace"]["state"] == "unavailable"

    running = detect_workspace_toolchains(
        tmp_path,
        lsp_runtime={"scala": {"running": True, "initialized": True}},
    )
    running_caps = _caps(running)
    assert running["scala"]["workspace_diagnostics_ready"] is True
    assert running["scala"]["reason"] == "ready"
    assert running_caps["lsp.metals"]["state"] == "ready"
    assert running_caps["diagnostics.scala"]["state"] == "ready"
    assert running_caps["diagnostics.scala"]["provider"] == "metals"
    assert running_caps["diagnostics.scala.workspace"]["state"] == "ready"


def test_metals_and_gradle_wrapper_support_scala_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'scala' }\n",
        encoding="utf-8",
    )
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "omnicode_core.capabilities.toolchains.shutil.which",
        _which_factory({"metals"}),
    )

    status = detect_workspace_toolchains(
        tmp_path,
        lsp_runtime={"scala": {"running": True, "initialized": True}},
    )
    caps = _caps(status)

    assert status["scala"]["build_system"] == "gradle"
    assert status["scala"]["build_ready"] is True
    assert status["scala"]["workspace_diagnostics_ready"] is True
    assert caps["build.gradle"]["state"] == "ready"
    assert caps["diagnostics.scala.workspace"]["state"] == "ready"
