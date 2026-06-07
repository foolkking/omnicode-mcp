"""Tests for embedded agent auto-start policy."""

from __future__ import annotations

from pathlib import Path

from omnicode_core.config.runtime import RuntimeConfig
from omnicode_core.workspace.agent_auto import decide_agent_auto


def _runtime(**overrides) -> RuntimeConfig:
    values = {
        "workspace_root": Path.cwd(),
        "workspace_id": "repo-a",
        "executor": "hybrid",
        "backend_url": "http://cloud",
        "sync_mode": "smart",
        "agent_mode": "auto",
        "debounce_ms": 1200,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_auto_starts_embedded_agent_for_hybrid_sync() -> None:
    decision = decide_agent_auto(_runtime())

    assert decision.target == "embedded"
    assert decision.should_start is True
    assert decision.initial_sync is True
    assert decision.requires_remote is True
    assert decision.debounce_ms == 1200


def test_agent_off_disables_start() -> None:
    decision = decide_agent_auto(_runtime(agent_mode="off"))

    assert decision.target == "off"
    assert decision.should_start is False
    assert decision.requires_remote is False


def test_external_agent_does_not_start_embedded_watcher() -> None:
    decision = decide_agent_auto(_runtime(agent_mode="external"))

    assert decision.target == "external"
    assert decision.should_start is False
    assert decision.requires_remote is True


def test_sync_off_disables_agent_even_in_auto_mode() -> None:
    decision = decide_agent_auto(_runtime(sync_mode="off"))

    assert decision.target == "off"
    assert decision.should_start is False


def test_strict_sync_without_cloud_is_blocked() -> None:
    decision = decide_agent_auto(
        _runtime(sync_mode="strict", backend_url=None)
    )

    assert decision.target == "blocked"
    assert decision.should_start is False
    assert decision.requires_remote is True


def test_non_hybrid_executor_does_not_auto_start_embedded_agent() -> None:
    decision = decide_agent_auto(_runtime(executor="local"))

    assert decision.target == "disabled"
    assert decision.should_start is False
    assert "hybrid executor" in decision.reason


def test_decision_to_dict_is_stable() -> None:
    data = decide_agent_auto(_runtime()).to_dict()

    assert data["mode"] == "auto"
    assert data["sync_mode"] == "smart"
    assert data["executor"] == "hybrid"
    assert data["target"] == "embedded"


def test_mcp_embedded_agent_starts_initial_sync_without_stdout(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from omnicode_adapters.agent import client as client_mod
    from omnicode_adapters.agent import watcher as watcher_mod
    from omnicode_adapters.cli.commands import mcp_cmd

    events: list[tuple[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            events.append(("client", kwargs))

        def close(self) -> None:
            events.append(("close", None))

    class FakeWatcher:
        def __init__(self, **kwargs) -> None:
            events.append(("watcher", kwargs))
            self._printer = kwargs["printer"]

        def initial_sync(self) -> None:
            events.append(("initial_sync", None))
            self._printer("initial sync fake")

        def run(self) -> None:
            events.append(("run", None))

    monkeypatch.setattr(client_mod, "AgentClient", FakeClient)
    monkeypatch.setattr(watcher_mod, "Watcher", FakeWatcher)
    monkeypatch.setattr(mcp_cmd, "_embedded_agent_thread", None)

    runtime = _runtime(workspace_root=tmp_path)
    mcp_cmd._start_embedded_agent_if_configured(runtime, backend_token="token")
    thread = mcp_cmd._embedded_agent_thread
    assert thread is not None
    thread.join(timeout=1.0)
    assert not thread.is_alive()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "embedded watcher started" in captured.err
    assert "initial sync fake" in captured.err
    assert ("initial_sync", None) in events
    assert ("run", None) in events
    client_kwargs = next(value for name, value in events if name == "client")
    assert client_kwargs["workspace"] == tmp_path
    assert client_kwargs["workspace_id"] == "repo-a"
    assert client_kwargs["token"] == "token"
    assert client_kwargs["batch_max_files"] == runtime.batch_max_files
    assert client_kwargs["batch_max_bytes"] == runtime.batch_max_bytes
