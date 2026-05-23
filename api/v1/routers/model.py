"""
Model status & provider management endpoints (STAGE 2.5 + External API Integration)
====================================================================================
Exposes the live state of the multi-provider LLM gateway and lets callers
register / update / disable / test custom providers at runtime.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import get_llm_router, get_settings
from omnicode.llm.provider_registry import ProviderConfig
from utils import create_error_response, create_success_response

router = APIRouter(tags=["model"])


# ---------------------------------------------------------------------------
# /model-status — gateway snapshot
# ---------------------------------------------------------------------------
@router.get("/model-status")
async def get_model_status():
    """Return live status, statistics and configuration of the LLM gateway."""
    try:
        llm_router = get_llm_router()
        settings = get_settings()

        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)

        status = llm_router.get_status()
        # Augment with high-level config snapshot
        status["config"] = {
            "default_provider": settings.DEFAULT_LLM_PROVIDER,
            "default_model": settings.DEFAULT_LLM_MODEL,
            "api_keys_configured": {
                "anthropic": bool(settings.ANTHROPIC_API_KEY),
                "openai": bool(settings.OPENAI_API_KEY),
                "gemini": bool(settings.GEMINI_API_KEY),
                "deepseek": bool(settings.DEEPSEEK_API_KEY),
            },
        }
        # Compute aggregate health
        provider_details = status.get("providers_detail", {})
        healthy_count = sum(
            1 for p in provider_details.values() if p.get("stats", {}).get("healthy", True)
        )
        status["health"] = {
            "healthy_providers": healthy_count,
            "total_providers": len(provider_details),
            "all_healthy": healthy_count == len(provider_details),
        }
        return create_success_response(status)

    except Exception as e:  # pragma: no cover - defensive
        return create_error_response(f"Failed to get model status: {e}", 500)


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------
class ProviderUpsertRequest(BaseModel):
    """Payload for registering or updating a custom provider."""

    name: str = Field(..., description="Unique provider name")
    model: str = Field(..., description="LiteLLM model string, e.g. 'azure/gpt-4o'")
    api_key: Optional[str] = Field(None, description="API key (stored in local DB)")
    api_base: Optional[str] = Field(None, description="Custom API base URL")
    provider_type: str = Field(
        default="openai-compatible",
        description="Provider family tag (openai-compatible / anthropic / gemini / ollama / azure / bedrock / custom)",
    )
    group: str = Field(
        default="balanced",
        description="Routing group: 'quality', 'cost', or 'balanced'",
    )
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = Field(default=True)
    description: str = Field(default="")


class ProviderTestRequest(BaseModel):
    """Optional payload for /providers/{name}/test."""

    prompt: Optional[str] = Field(None, description="Override the default ping prompt")
    max_tokens: int = Field(default=16, ge=1, le=128)


@router.get("/providers")
async def list_providers(reveal_secrets: bool = False) -> Any:
    """List all known providers, including those that are currently disabled."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)

        configs = llm_router.list_provider_configs(redact_secret=not reveal_secrets)
        return create_success_response({
            "providers": configs,
            "active_providers": list(llm_router.providers.keys()),
            "quality_chain": llm_router.quality_chain,
            "cost_chain": llm_router.cost_chain,
        })
    except Exception as e:
        return create_error_response(f"Failed to list providers: {e}", 500)


@router.post("/providers")
async def add_or_update_provider(request: ProviderUpsertRequest) -> Any:
    """Add a new custom provider or update an existing one."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)

        if request.group not in {"quality", "cost", "balanced"}:
            return create_error_response(
                f"Invalid group '{request.group}'. Must be quality, cost, or balanced.",
                400,
            )

        existing = llm_router.registry.get(request.name)
        # Preserve built-in flag — the API never lets callers create a built-in
        cfg = ProviderConfig(
            name=request.name,
            model=request.model,
            api_key=request.api_key if request.api_key is not None
                    else (existing.api_key if existing else None),
            api_base=request.api_base,
            provider_type=request.provider_type,
            group=request.group,
            extra_headers=request.extra_headers or {},
            enabled=request.enabled,
            built_in=existing.built_in if existing else False,
            description=request.description,
        )
        llm_router.add_or_update_provider(cfg)
        return create_success_response({
            "message": f"Provider '{cfg.name}' saved.",
            "provider": cfg.to_dict(),
            "active_providers": list(llm_router.providers.keys()),
        })
    except Exception as e:
        return create_error_response(f"Failed to save provider: {e}", 500)


@router.put("/providers/{name}")
async def update_provider(name: str, request: ProviderUpsertRequest) -> Any:
    """Alias for POST /providers — convenience route."""
    if request.name != name:
        request.name = name
    return await add_or_update_provider(request)


@router.delete("/providers/{name}")
async def delete_provider(name: str) -> Any:
    """Delete a custom (non-built-in) provider."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)

        cfg = llm_router.registry.get(name)
        if cfg is None:
            return create_error_response(f"Unknown provider: {name}", 404)
        if cfg.built_in:
            return create_error_response(
                f"Provider '{name}' is built-in and cannot be deleted. "
                "You can disable it instead.",
                400,
            )
        ok = llm_router.remove_provider(name)
        if not ok:
            return create_error_response(f"Failed to delete provider '{name}'.", 500)
        return create_success_response({
            "message": f"Provider '{name}' deleted.",
            "active_providers": list(llm_router.providers.keys()),
        })
    except Exception as e:
        return create_error_response(f"Failed to delete provider: {e}", 500)


@router.post("/providers/{name}/enable")
async def enable_provider(name: str) -> Any:
    """Enable a provider (built-ins included)."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        ok = llm_router.set_provider_enabled(name, True)
        if not ok:
            return create_error_response(f"Unknown provider: {name}", 404)
        return create_success_response({
            "message": f"Provider '{name}' enabled.",
            "active_providers": list(llm_router.providers.keys()),
        })
    except Exception as e:
        return create_error_response(f"Failed to enable provider: {e}", 500)


@router.post("/providers/{name}/disable")
async def disable_provider(name: str) -> Any:
    """Disable a provider so it is excluded from routing."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        ok = llm_router.set_provider_enabled(name, False)
        if not ok:
            return create_error_response(f"Unknown provider: {name}", 404)
        return create_success_response({
            "message": f"Provider '{name}' disabled.",
            "active_providers": list(llm_router.providers.keys()),
        })
    except Exception as e:
        return create_error_response(f"Failed to disable provider: {e}", 500)


@router.post("/providers/{name}/test")
async def test_provider(name: str, request: Optional[ProviderTestRequest] = None) -> Any:
    """Send a tiny ping prompt to the provider and report success/failure."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        prompt = (request.prompt if request and request.prompt
                  else "Reply with the single word: pong.")
        max_tokens = request.max_tokens if request else 16
        result = await llm_router.test_provider(
            name, prompt=prompt, max_tokens=max_tokens
        )
        if result.get("success"):
            return create_success_response(result)
        # Failure: keep the full payload (error + hint + duration_ms) under
        # success_response so the UI can render the hint.  We still indicate
        # the test failed via result.success == False.
        return create_success_response(result)
    except Exception as e:
        return create_error_response(f"Failed to test provider: {e}", 500)


@router.post("/providers/reload")
async def reload_providers() -> Any:
    """Reload providers from the registry (no-op if nothing changed)."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        status = llm_router.reload()
        return create_success_response({
            "message": "Providers reloaded.",
            "providers_count": status.get("providers_count", 0),
            "providers": status.get("providers", []),
        })
    except Exception as e:
        return create_error_response(f"Failed to reload providers: {e}", 500)


# ---------------------------------------------------------------------------
# Active model selection (which provider serves which role)
# ---------------------------------------------------------------------------
class SelectionUpdateRequest(BaseModel):
    """Bulk update of role → provider assignments. Empty value clears the role."""

    assignments: Dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of role name to provider name. "
                    "Use empty string to clear a role and revert to auto-routing.",
    )


@router.get("/selections")
async def get_selections() -> Any:
    """Return the active routing assignments and the list of valid roles."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        from omnicode.llm.provider_selection import VALID_ROLES
        selections = llm_router.get_selections()
        configs = llm_router.list_provider_configs()
        # Highlight which assigned providers are currently disabled / missing.
        config_index = {c["name"]: c for c in configs}
        warnings: List[str] = []
        for role, name in selections.items():
            cfg = config_index.get(name)
            if cfg is None:
                warnings.append(
                    f"Role '{role}' is assigned to '{name}' but no such provider exists."
                )
            elif not cfg.get("enabled", True):
                warnings.append(
                    f"Role '{role}' is assigned to disabled provider '{name}'."
                )
        return create_success_response({
            "valid_roles": list(VALID_ROLES),
            "assignments": selections,
            "available_providers": [
                c["name"] for c in configs if c.get("enabled", True)
            ],
            "warnings": warnings,
        })
    except Exception as e:
        return create_error_response(f"Failed to get selections: {e}", 500)


@router.put("/selections")
async def update_selections(request: SelectionUpdateRequest) -> Any:
    """Update active routing assignments in bulk."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        try:
            llm_router.set_selections(request.assignments)
        except ValueError as ve:
            return create_error_response(str(ve), 400)
        return create_success_response({
            "message": "Selections updated.",
            "assignments": llm_router.get_selections(),
        })
    except Exception as e:
        return create_error_response(f"Failed to update selections: {e}", 500)


@router.put("/selections/{role}")
async def set_selection(role: str, provider_name: Optional[str] = None) -> Any:
    """Assign a single role to a provider (or clear with empty)."""
    try:
        llm_router = get_llm_router()
        if llm_router is None:
            return create_error_response("LLM router not initialized", 500)
        try:
            llm_router.set_selection(role, provider_name or None)
        except ValueError as ve:
            return create_error_response(str(ve), 400)
        return create_success_response({
            "role": role,
            "provider_name": provider_name,
            "assignments": llm_router.get_selections(),
        })
    except Exception as e:
        return create_error_response(f"Failed to set selection: {e}", 500)
