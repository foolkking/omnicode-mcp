"""Agent auto-start policy for hybrid MCP sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from omnicode_core.config.runtime import RuntimeConfig


@dataclass(frozen=True)
class AgentAutoDecision:
    mode: str
    sync_mode: str
    executor: str
    target: str
    should_start: bool
    initial_sync: bool
    debounce_ms: int
    requires_remote: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def decide_agent_auto(runtime: RuntimeConfig) -> AgentAutoDecision:
    """Decide whether the local MCP process should start an embedded agent."""
    agent_mode = (runtime.agent_mode or "auto").lower()
    sync_mode = (runtime.sync_mode or "off").lower()
    executor = (runtime.executor or "local").lower()
    remote_configured = bool(runtime.backend_url)

    if agent_mode == "off":
        return _decision(
            runtime,
            target="off",
            should_start=False,
            requires_remote=False,
            reason="agent mode is off",
        )
    if sync_mode == "off":
        return _decision(
            runtime,
            target="off",
            should_start=False,
            requires_remote=False,
            reason="sync mode is off",
        )
    if agent_mode == "external":
        return _decision(
            runtime,
            target="external",
            should_start=False,
            requires_remote=True,
            reason="external agent mode expects a separately started watcher",
        )
    if executor != "hybrid":
        return _decision(
            runtime,
            target="disabled",
            should_start=False,
            requires_remote=False,
            reason="embedded agent auto-start only applies to hybrid executor",
        )
    if not remote_configured:
        return _decision(
            runtime,
            target="blocked" if sync_mode == "strict" else "disabled",
            should_start=False,
            requires_remote=True,
            reason="cloud backend is required for agent sync",
        )
    return _decision(
        runtime,
        target="embedded",
        should_start=True,
        requires_remote=True,
        reason="auto mode will start embedded agent for hybrid sync",
    )


def _decision(
    runtime: RuntimeConfig,
    *,
    target: str,
    should_start: bool,
    requires_remote: bool,
    reason: str,
) -> AgentAutoDecision:
    return AgentAutoDecision(
        mode=runtime.agent_mode,
        sync_mode=runtime.sync_mode,
        executor=runtime.executor,
        target=target,
        should_start=should_start,
        initial_sync=should_start and runtime.sync_mode in {"watch", "smart", "strict"},
        debounce_ms=runtime.debounce_ms,
        requires_remote=requires_remote,
        reason=reason,
    )


__all__ = ["AgentAutoDecision", "decide_agent_auto"]
