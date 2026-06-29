"""Runtime capability contract for LLM, embeddings, and diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from omnicode_core.config.runtime import RuntimeConfig


@dataclass(frozen=True)
class CapabilityPolicy:
    name: str
    mode: str
    target: str
    available: bool
    reason: str
    local_allowed: bool = False
    cloud_allowed: bool = False
    requires_cloud: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityContract:
    executor: str
    cloud_configured: bool
    llm: CapabilityPolicy
    embedding: CapabilityPolicy
    diagnostics: CapabilityPolicy

    def to_dict(self) -> dict:
        return {
            "executor": self.executor,
            "cloud_configured": self.cloud_configured,
            "llm": self.llm.to_dict(),
            "embedding": self.embedding.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
        }


def build_capability_contract(
    runtime: RuntimeConfig,
    *,
    local_llm_available: Optional[bool] = None,
    local_embedding_available: Optional[bool] = None,
    cloud_embedding_available: Optional[bool] = None,
) -> CapabilityContract:
    """Build a deterministic capability contract from runtime config."""
    cloud_configured = bool(runtime.backend_url)
    llm = _llm_policy(
        runtime.llm_mode,
        cloud_configured=cloud_configured,
        local_available=bool(local_llm_available),
        local_probe_known=local_llm_available is not None,
    )
    embedding = _embedding_policy(
        runtime.embedding_mode,
        cloud_configured=cloud_configured,
        local_available=True
        if local_embedding_available is None
        else bool(local_embedding_available),
        cloud_available=cloud_embedding_available,
    )
    diagnostics = _diagnostics_policy(
        runtime.diagnostics_mode,
        cloud_configured=cloud_configured,
    )
    return CapabilityContract(
        executor=runtime.executor,
        cloud_configured=cloud_configured,
        llm=llm,
        embedding=embedding,
        diagnostics=diagnostics,
    )


def _llm_policy(
    mode: str,
    *,
    cloud_configured: bool,
    local_available: bool,
    local_probe_known: bool,
) -> CapabilityPolicy:
    mode = (mode or "off").lower()
    if mode == "off":
        return CapabilityPolicy(
            name="llm",
            mode=mode,
            target="off",
            available=False,
            reason="LLM mode is off",
        )
    if mode == "local":
        return CapabilityPolicy(
            name="llm",
            mode=mode,
            target="local",
            available=local_available,
            reason="local LLM provider is available"
            if local_available
            else _local_llm_unavailable_reason(local_probe_known),
            local_allowed=True,
        )
    if mode == "remote":
        return CapabilityPolicy(
            name="llm",
            mode=mode,
            target="cloud",
            available=cloud_configured,
            reason="remote LLM uses configured cloud backend"
            if cloud_configured
            else "remote LLM requested but cloud backend is not configured",
            cloud_allowed=True,
            requires_cloud=True,
        )
    if mode == "auto":
        if local_available:
            return CapabilityPolicy(
                name="llm",
                mode=mode,
                target="local",
                available=True,
                reason="auto selected local LLM provider",
                local_allowed=True,
                cloud_allowed=cloud_configured,
            )
        if cloud_configured:
            return CapabilityPolicy(
                name="llm",
                mode=mode,
                target="cloud",
                available=True,
                reason="auto selected configured cloud backend",
                local_allowed=local_probe_known,
                cloud_allowed=True,
                requires_cloud=True,
            )
        return CapabilityPolicy(
            name="llm",
            mode=mode,
            target="off",
            available=False,
            reason="auto found no local LLM and no configured cloud backend",
            local_allowed=local_probe_known,
        )
    return CapabilityPolicy(
        name="llm",
        mode=mode,
        target="off",
        available=False,
        reason=f"unknown LLM mode: {mode}",
    )


def _embedding_policy(
    mode: str,
    *,
    cloud_configured: bool,
    local_available: bool,
    cloud_available: Optional[bool] = None,
) -> CapabilityPolicy:
    mode = (mode or "off").lower()
    if mode == "off":
        return CapabilityPolicy(
            name="embedding",
            mode=mode,
            target="off",
            available=False,
            reason="embedding mode is off",
        )
    if mode == "local":
        return CapabilityPolicy(
            name="embedding",
            mode=mode,
            target="local",
            available=local_available,
            reason="local embedding backend is available"
            if local_available
            else "local embedding backend is unavailable",
            local_allowed=True,
        )
    if mode == "cloud":
        if cloud_available is not None:
            available = cloud_configured and bool(cloud_available)
            return CapabilityPolicy(
                name="embedding",
                mode=mode,
                target="cloud",
                available=available,
                reason="cloud embedding backend is available"
                if available
                else (
                    "cloud embedding backend is unavailable"
                    if cloud_configured
                    else "cloud embedding requested but cloud backend is not configured"
                ),
                cloud_allowed=True,
                requires_cloud=True,
            )
        return CapabilityPolicy(
            name="embedding",
            mode=mode,
            target="cloud",
            available=cloud_configured,
            reason="cloud embedding uses configured backend"
            if cloud_configured
            else "cloud embedding requested but cloud backend is not configured",
            cloud_allowed=True,
            requires_cloud=True,
        )
    return CapabilityPolicy(
        name="embedding",
        mode=mode,
        target="off",
        available=False,
        reason=f"unknown embedding mode: {mode}",
    )


def _diagnostics_policy(
    mode: str,
    *,
    cloud_configured: bool,
) -> CapabilityPolicy:
    mode = (mode or "local-first").lower()
    if mode == "off":
        return CapabilityPolicy(
            name="diagnostics",
            mode=mode,
            target="off",
            available=False,
            reason="diagnostics mode is off",
        )
    if mode in {"local", "local-first"}:
        return CapabilityPolicy(
            name="diagnostics",
            mode=mode,
            target="local",
            available=True,
            reason="diagnostics run locally"
            if mode == "local"
            else "diagnostics run locally first",
            local_allowed=True,
            cloud_allowed=cloud_configured and mode == "local-first",
        )
    if mode == "remote":
        return CapabilityPolicy(
            name="diagnostics",
            mode=mode,
            target="cloud",
            available=cloud_configured,
            reason="remote diagnostics use configured cloud backend"
            if cloud_configured
            else "remote diagnostics requested but cloud backend is not configured",
            cloud_allowed=True,
            requires_cloud=True,
        )
    return CapabilityPolicy(
        name="diagnostics",
        mode=mode,
        target="off",
        available=False,
        reason=f"unknown diagnostics mode: {mode}",
    )


def _local_llm_unavailable_reason(probe_known: bool) -> str:
    if probe_known:
        return "local LLM provider is unavailable"
    return "local LLM availability was not probed"


__all__ = [
    "CapabilityContract",
    "CapabilityPolicy",
    "build_capability_contract",
]
