from __future__ import annotations

import asyncio
from pathlib import Path

from omnicode_core.lsp.bridge import LSPBridge
from omnicode_core.lsp.shadow import LSPShadowWorkspace


def test_shadow_workspace_is_incremental_and_state_dir_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repo"
    state = tmp_path / "state"
    scala_file = source / "core" / "src" / "main" / "scala" / "App.scala"
    scala_file.parent.mkdir(parents=True)
    scala_file.write_text("object App {}\n", encoding="utf-8")
    (source / "build.sbt").write_text(
        'scalaVersion := "2.13.14"\n',
        encoding="utf-8",
    )
    generated = source / "target" / "Generated.scala"
    generated.parent.mkdir(parents=True)
    generated.write_text("object Generated {}\n", encoding="utf-8")

    shadow = LSPShadowWorkspace(source, state / "scala-shadow")
    first = shadow.sync_full()

    assert first["ready"] is True
    assert first["copied"] == 2
    assert (
        shadow.workspace_root
        / "core"
        / "src"
        / "main"
        / "scala"
        / "App.scala"
    ).is_file()
    assert not (shadow.workspace_root / "target" / "Generated.scala").exists()
    assert not (source / ".metals").exists()
    assert not (source / ".bloop").exists()

    second = shadow.sync_full()
    assert second["copied"] == 0
    assert second["unchanged"] == 2

    scala_file.write_text("object App { val value = 1 }\n", encoding="utf-8")
    third = shadow.sync_full()
    assert third["copied"] == 1
    assert "value = 1" in (
        shadow.workspace_root
        / "core"
        / "src"
        / "main"
        / "scala"
        / "App.scala"
    ).read_text(encoding="utf-8")

    scala_file.unlink()
    fourth = shadow.sync_full()
    assert fourth["deleted"] == 1
    assert not (
        shadow.workspace_root
        / "core"
        / "src"
        / "main"
        / "scala"
        / "App.scala"
    ).exists()


def test_bridge_maps_shadow_uri_back_to_workspace_relative_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repo"
    target = source / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App {}\n", encoding="utf-8")
    bridge = LSPBridge(str(source), state_dir=str(tmp_path / "state"))
    bridge._scala_shadow.sync_full()

    uri = bridge._file_uri(
        "src/main/scala/App.scala",
        root=str(bridge._scala_shadow.workspace_root),
    )

    assert bridge._uri_to_path(uri) == str(
        Path("src") / "main" / "scala" / "App.scala"
    )
    assert str(tmp_path / "state") not in bridge._uri_to_path(uri)


def test_scala_bootstrap_materializes_shadow_before_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "repo"
    target = source / "src" / "main" / "scala" / "App.scala"
    target.parent.mkdir(parents=True)
    target.write_text("object App {}\n", encoding="utf-8")
    bridge = LSPBridge(str(source), state_dir=str(tmp_path / "state"))

    class _Server:
        @staticmethod
        def is_alive() -> bool:
            return True

    async def _fake_get_server(language: str):
        assert language == "scala"
        return _Server()

    monkeypatch.setattr(bridge, "_is_available", lambda language: True)
    monkeypatch.setattr(bridge, "_get_server", _fake_get_server)

    result = asyncio.run(bridge.bootstrap({"scala"}))

    assert result["ready"] is True
    assert result["languages"]["scala"]["shadow_workspace"]["ready"] is True
    assert (
        bridge._scala_shadow.workspace_root
        / "src"
        / "main"
        / "scala"
        / "App.scala"
    ).is_file()


def test_java_uses_same_state_dir_shadow_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repo"
    target = source / "src" / "main" / "java" / "App.java"
    target.parent.mkdir(parents=True)
    target.write_text("class App {}\n", encoding="utf-8")
    bridge = LSPBridge(str(source), state_dir=str(tmp_path / "state"))

    status = bridge._sync_language_workspace("java")

    assert status["ready"] is True
    assert bridge._server_working_dir("java") == str(
        bridge._jvm_shadow.workspace_root
    )
    assert (
        bridge._jvm_shadow.workspace_root
        / "src"
        / "main"
        / "java"
        / "App.java"
    ).is_file()
    assert not (source / ".project").exists()
    assert not (source / ".settings").exists()


def test_jvm_lsp_command_can_be_pinned_by_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bridge = LSPBridge(str(tmp_path), state_dir=str(tmp_path / "state"))
    monkeypatch.setenv(
        "OMNICODE_JDTLS_COMMAND",
        '["C:/tools/jdtls.cmd", "--stdio"]',
    )
    monkeypatch.setenv(
        "OMNICODE_METALS_COMMAND",
        '"C:/Program Files/Metals/metals.cmd"',
    )

    assert bridge._configured_server_command("java") == [
        "C:/tools/jdtls.cmd",
        "--stdio",
    ]
    assert bridge._configured_server_command("scala") == [
        "C:/Program Files/Metals/metals.cmd",
    ]


def test_jvm_language_servers_support_separate_java_homes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bridge = LSPBridge(str(tmp_path), state_dir=str(tmp_path / "state"))
    java_home = tmp_path / "jdk-21"
    metals_home = tmp_path / "jdk-17"
    monkeypatch.setenv("OMNICODE_JDTLS_JAVA_HOME", str(java_home))
    monkeypatch.setenv("OMNICODE_METALS_JAVA_HOME", str(metals_home))

    java_env = bridge._server_environment("java")
    scala_env = bridge._server_environment("scala")

    assert java_env["JAVA_HOME"] == str(java_home.resolve())
    assert scala_env["JAVA_HOME"] == str(metals_home.resolve())
    assert java_env["PATH"].split(";")[0] == str(java_home.resolve() / "bin")
    assert scala_env["PATH"].split(";")[0] == str(
        metals_home.resolve() / "bin"
    )


def test_jvm_language_server_can_be_explicitly_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bridge = LSPBridge(str(tmp_path), state_dir=str(tmp_path / "state"))
    monkeypatch.setenv("OMNICODE_JDTLS_DISABLED", "true")

    assert bridge._is_available("java") is False
    assert bridge._start_policy("java") == (False, "java_lsp_disabled")
