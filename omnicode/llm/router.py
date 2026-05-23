"""
LLM Router (STAGE 2 + External API Integration)
================================================
Intelligent multi-provider router with:

* Routing strategies (cost-optimized / quality-first / fastest / task-based)
* Health & rate-limit aware fallback chains
* Per-provider call statistics, cost & latency tracking
* Background-friendly call recording (used by /model-status endpoint)
* Hot-add custom providers via the ProviderRegistry (SQLite-backed)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..config import get_settings
from .base import BaseLLMProvider, LLMMessage, LLMResponse, Role
from .provider_registry import (
    ProviderConfig,
    ProviderRegistry,
    get_provider_registry,
)
from .provider_selection import (
    VALID_ROLES,
    ProviderSelectionStore,
    get_provider_selection_store,
)
from .providers.litellm_provider import LiteLLMProvider

logger = logging.getLogger(__name__)


class RoutingStrategy(str, Enum):
    COST_OPTIMIZED = "cost_optimized"
    QUALITY_FIRST = "quality_first"
    FASTEST = "fastest"
    TASK_BASED = "task_based"


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------
@dataclass
class CallRecord:
    """Single LLM invocation record kept for diagnostics."""

    provider: str
    model: str
    timestamp: str
    duration_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    success: bool = True
    error: Optional[str] = None
    strategy: Optional[str] = None


@dataclass
class ProviderStats:
    """Aggregated stats for a single provider."""

    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    last_error: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    consecutive_failures: int = 0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_latency_ms / max(1, self.total_calls)

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_count / self.total_calls

    @property
    def healthy(self) -> bool:
        # Heuristic: provider is healthy unless it has 3+ consecutive failures
        return self.consecutive_failures < 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate, 4),
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "healthy": self.healthy,
        }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
class LLMRouter:
    """Intelligent router for LLM requests."""

    # Quick-glance task → strategy hints used by RoutingStrategy.TASK_BASED
    _TASK_HINTS = {
        "edit": RoutingStrategy.QUALITY_FIRST,
        "refactor": RoutingStrategy.QUALITY_FIRST,
        "review": RoutingStrategy.QUALITY_FIRST,
        "scan": RoutingStrategy.COST_OPTIMIZED,
        "index": RoutingStrategy.COST_OPTIMIZED,
        "embed": RoutingStrategy.COST_OPTIMIZED,
        "summary": RoutingStrategy.FASTEST,
        "chat": RoutingStrategy.FASTEST,
    }

    def __init__(
        self,
        recent_size: int = 32,
        registry: Optional[ProviderRegistry] = None,
        selection_store: Optional[ProviderSelectionStore] = None,
    ) -> None:
        self.settings = get_settings()
        self.providers: Dict[str, BaseLLMProvider] = {}
        self.configs: Dict[str, ProviderConfig] = {}
        self.quality_chain: List[str] = []
        self.cost_chain: List[str] = []
        self.stats: Dict[str, ProviderStats] = {}
        self.recent_calls: Deque[CallRecord] = deque(maxlen=recent_size)
        self._lock = asyncio.Lock()
        self.registry = registry or get_provider_registry()
        self.selection_store = selection_store or get_provider_selection_store()
        self._sync_builtins_to_registry()
        self._reload_from_registry()

    # ------------------------------------------------------------ bootstrap
    @staticmethod
    def _is_real_key(value: Optional[str]) -> bool:
        """Return True only if ``value`` looks like a real API key.

        Filters out the obvious placeholders shipped in ``.env.example`` so
        a user who never edited their .env doesn't end up with broken
        built-in providers (and broken role assignments pointing at them).
        """
        if not value:
            return False
        v = value.strip()
        if len(v) < 8:
            return False
        lowered = v.lower()
        # Common placeholder patterns we ship in .env.example.
        bad_substrings = (
            "your_",
            "your-",
            "<your",
            "placeholder",
            "replace_me",
            "replace-me",
            "changeme",
            "change-me",
            "xxx",
            "example",
            "_here",
            "-here",
        )
        return not any(s in lowered for s in bad_substrings)
    def _builtin_configs_from_env(self) -> List[ProviderConfig]:
        """Construct built-in provider configs from environment-supplied keys."""
        out: List[ProviderConfig] = []
        s = self.settings

        if self._is_real_key(s.ANTHROPIC_API_KEY):
            out.append(ProviderConfig(
                name="claude", model="claude-3-opus-20240229",
                api_key=s.ANTHROPIC_API_KEY, provider_type="anthropic",
                group="quality", built_in=True,
                description="Anthropic Claude 3 Opus (built-in)",
            ))
            out.append(ProviderConfig(
                name="claude_fast", model="claude-3-haiku-20240307",
                api_key=s.ANTHROPIC_API_KEY, provider_type="anthropic",
                group="cost", built_in=True,
                description="Anthropic Claude 3 Haiku (built-in)",
            ))

        if self._is_real_key(s.OPENAI_API_KEY):
            out.append(ProviderConfig(
                name="openai", model="gpt-4o",
                api_key=s.OPENAI_API_KEY, provider_type="openai",
                group="quality", built_in=True,
                description="OpenAI GPT-4o (built-in)",
            ))
            out.append(ProviderConfig(
                name="openai_fast", model="gpt-4o-mini",
                api_key=s.OPENAI_API_KEY, provider_type="openai",
                group="cost", built_in=True,
                description="OpenAI GPT-4o-mini (built-in)",
            ))

        if self._is_real_key(s.GEMINI_API_KEY):
            out.append(ProviderConfig(
                name="gemini", model="gemini/gemini-1.5-pro",
                api_key=s.GEMINI_API_KEY, provider_type="gemini",
                group="quality", built_in=True,
                description="Google Gemini 1.5 Pro (built-in)",
            ))
            out.append(ProviderConfig(
                name="gemini_fast", model="gemini/gemini-1.5-flash",
                api_key=s.GEMINI_API_KEY, provider_type="gemini",
                group="cost", built_in=True,
                description="Google Gemini 1.5 Flash (built-in)",
            ))

        if self._is_real_key(s.DEEPSEEK_API_KEY):
            out.append(ProviderConfig(
                name="deepseek", model="deepseek/deepseek-coder",
                api_key=s.DEEPSEEK_API_KEY, provider_type="openai-compatible",
                group="cost", built_in=True,
                description="DeepSeek Coder (built-in)",
            ))

        return out

    def _sync_builtins_to_registry(self) -> None:
        """Make sure built-in providers (env-key-derived) exist in the registry.

        Built-ins are always re-synced from env on startup so updated env keys
        propagate.  User customisation is preserved through ``enabled`` flag.

        Also cleans up stale built-ins whose env keys are now placeholders or
        missing — those rows would otherwise stay in the SQLite registry from
        a previous run and silently break role assignments.
        """
        valid = self._builtin_configs_from_env()
        valid_names = {cfg.name for cfg in valid}
        for cfg in valid:
            existing = self.registry.get(cfg.name)
            if existing is not None:
                # Preserve user toggle of enabled flag; refresh creds + group.
                cfg.enabled = existing.enabled
            self.registry.upsert(cfg)

        # Drop built-ins from a previous boot whose key is no longer real
        # (e.g. user reset .env back to placeholders, or never set the key).
        all_known_builtins = {
            "claude", "claude_fast",
            "openai", "openai_fast",
            "gemini", "gemini_fast",
            "deepseek",
        }
        for stale_name in all_known_builtins - valid_names:
            existing = self.registry.get(stale_name)
            if existing is not None and existing.built_in:
                logger.info(
                    "Dropping stale built-in '%s' — its env key is unset or a placeholder.",
                    stale_name,
                )
                try:
                    self.registry.delete(stale_name, force=True)
                except TypeError:
                    # Older registry impls don't take force=
                    self.registry.delete(stale_name)
                except Exception as exc:  # pragma: no cover
                    logger.debug("Could not drop stale built-in %s: %s", stale_name, exc)

        # Clear role selections that point to a provider that no longer
        # exists in the registry — otherwise the router will keep trying
        # to route through a phantom name and every Edit call will fail.
        try:
            existing_names = {c.name for c in self.registry.list_providers()}
            current = self.selection_store.get_all() if self.selection_store else {}
            for role, pname in list(current.items()):
                if pname and pname not in existing_names:
                    logger.info(
                        "Clearing role '%s' which pointed to dropped provider '%s'.",
                        role, pname,
                    )
                    self.selection_store.set_role(role, None)
        except Exception as exc:  # pragma: no cover
            logger.debug("Selection cleanup skipped: %s", exc)

    def _build_provider(self, cfg: ProviderConfig) -> Optional[BaseLLMProvider]:
        """Construct a runtime provider object from a ProviderConfig."""
        try:
            return LiteLLMProvider(
                self._normalize_model_name(cfg),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                extra_headers=cfg.extra_headers or None,
            )
        except TypeError:
            # Older LiteLLMProvider signature lacks api_base / extra_headers.
            return LiteLLMProvider(self._normalize_model_name(cfg), cfg.api_key)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to build provider %s: %s", cfg.name, exc)
            return None

    @staticmethod
    def _extract_short_error(raw: str, fallback_name: str) -> str:
        """Pull the most actionable message out of a LiteLLM exception repr.

        LiteLLM exceptions look like::

            litellm.AuthenticationError: GeminiException - {
              "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT", ...
              }
            }

        Truncating to the first line yields ``"GeminiException - {"`` which
        hides the useful bit.  Try in order:
          * ``"message": "..."`` JSON field
          * ``"error": "..."``  JSON field
          * first line of the exception repr
          * exception class name
        """
        if not raw:
            return fallback_name
        # Try to pull a JSON message out
        import re

        m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            return m.group(1)[:300]
        m = re.search(r'"error"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            return m.group(1)[:300]
        # Otherwise: prefix (e.g. "litellm.AuthenticationError: ...") only,
        # collapsed to a single line.
        first_line = raw.split("\n", 1)[0].strip()
        # Drop dangling JSON opener
        if first_line.endswith("{"):
            # Try to grab the next non-empty line for context
            tail = raw.split("\n", 2)
            if len(tail) > 1:
                second = tail[1].strip()
                if second:
                    first_line = first_line[:-1].rstrip() + " " + second
        return first_line[:300] or fallback_name

    @staticmethod
    def _normalize_model_name(cfg: ProviderConfig) -> str:
        """Coerce ``cfg.model`` into a LiteLLM-routable name.

        LiteLLM picks its backend handler from the model-name *prefix*:
          * ``gemini/<model>``        -> Google AI Studio (api key auth)
          * ``vertex_ai/<model>``     -> Google Vertex AI (GCP ADC auth)
          * ``anthropic/<model>``     -> Anthropic (api key auth)
          * ``deepseek/<model>``      -> DeepSeek (api key auth)
          * ``openai/<model>``        -> OpenAI / OpenAI-compatible
          * ``ollama/<model>``        -> Local Ollama
          * <bare>                    -> defaults vary by provider.

        Resolution order:
          1. **api_base set** (e.g. user runs a local OpenAI-compatible
             gateway like Ollama, vLLM, LM Studio, or a self-hosted Gemini
             relay on port 2048) → ALWAYS use the ``openai/`` prefix so
             LiteLLM speaks the OpenAI protocol against the user's host
             instead of going off to ``generativelanguage.googleapis.com``.
          2. Already-prefixed names pass through untouched.
          3. ``provider_type`` driven defaults (anthropic / deepseek / etc).
          4. Last-resort heuristic: a bare ``gemini-*`` without api_base
             becomes ``gemini/<model>`` (the user clearly means Google AI
             Studio, not Vertex AI).
        """
        model = (cfg.model or "").strip()
        if not model:
            return model

        # ── 1. Custom api_base trumps everything ──────────────────────────
        # If the user pointed us at a self-hosted endpoint (anything from
        # http://127.0.0.1:11434 to https://my-corp-gateway/v1) we MUST use
        # the OpenAI transport — never go off to a public provider's domain
        # just because the model happens to be called "gemini-2.5-pro".
        if cfg.api_base:
            # If they already prefixed openai/ keep it; otherwise add it.
            if model.startswith("openai/"):
                return model
            return f"openai/{model}"

        # ── 2. Already-prefixed names pass through ────────────────────────
        prefixes = (
            "gemini/",
            "vertex_ai/",
            "anthropic/",
            "claude-",        # bare anthropic models work via API key
            "deepseek/",
            "openai/",
            "azure/",
            "bedrock/",
            "ollama/",
            "groq/",
            "mistral/",
            "cohere/",
            "together_ai/",
            "openrouter/",
            "text-completion-openai/",
        )
        if any(model.startswith(p) for p in prefixes) or "/" in model.split(":", 1)[0]:
            return model

        ptype = (cfg.provider_type or "").lower().strip()

        # ── 3. provider_type driven defaults (no api_base) ────────────────
        # Heuristic: a bare ``gemini-*`` without an api_base almost always
        # means Google AI Studio (the most common UI typo).  The user can
        # still override by typing the explicit ``vertex_ai/gemini-...``
        # prefix above.
        lower = model.lower()
        if lower.startswith("gemini-") or lower.startswith("gemini "):
            return f"gemini/{model}"

        if ptype == "gemini" or ptype == "google" or ptype == "google-ai-studio":
            return f"gemini/{model}"
        if ptype == "vertex" or ptype == "vertex_ai":
            return f"vertex_ai/{model}"
        if ptype == "anthropic":
            # Anthropic models like claude-3-opus-* work bare via API key.
            return model if model.startswith("claude-") else f"anthropic/{model}"
        if ptype == "deepseek":
            return f"deepseek/{model}"
        if ptype == "ollama":
            return f"ollama/{model}"
        if ptype in ("openai", "openai-compatible"):
            # Bare OpenAI ids (gpt-4o, gpt-4o-mini, ...) work as-is.
            # For an openai-compatible 3rd-party endpoint with a custom
            # base_url, prefix with `openai/` so LiteLLM uses the OpenAI
            # transport (otherwise it might try a registry lookup first).
            if cfg.api_base and not model.startswith(("gpt-", "o1-", "o3-")):
                return f"openai/{model}"
            return model

        return model

    def _reload_from_registry(self) -> None:
        """Rebuild self.providers and routing chains from the registry."""
        self.providers.clear()
        self.configs.clear()
        self.quality_chain.clear()
        self.cost_chain.clear()

        for cfg in self.registry.list_providers():
            if not cfg.enabled:
                continue
            provider = self._build_provider(cfg)
            if provider is None:
                continue
            self.providers[cfg.name] = provider
            self.configs[cfg.name] = cfg
            group = (cfg.group or "balanced").lower()
            if group == "quality":
                self.quality_chain.append(cfg.name)
            elif group == "cost":
                self.cost_chain.append(cfg.name)
            else:
                # balanced ⇒ join both chains
                self.quality_chain.append(cfg.name)
                self.cost_chain.append(cfg.name)

        # Fall back to a single default provider when nothing is configured
        if not self.providers:
            default_model = self.settings.DEFAULT_LLM_MODEL
            default_cfg = ProviderConfig(
                name="default", model=default_model,
                provider_type="openai-compatible", group="balanced",
                built_in=True, description="Fallback default provider",
            )
            self.providers["default"] = LiteLLMProvider(default_model)
            self.configs["default"] = default_cfg
            self.quality_chain.append("default")
            self.cost_chain.append("default")

        # Initialise stats for every provider, preserving prior aggregates
        for name in self.providers:
            self.stats.setdefault(name, ProviderStats())

        logger.info(
            "LLM Router providers refreshed (%d total): quality=%s cost=%s",
            len(self.providers),
            self.quality_chain,
            self.cost_chain,
        )

    # Public alias used by API/MCP layers after registry edits
    def reload(self) -> Dict[str, Any]:
        """Re-read providers from the registry and return the new status."""
        self._reload_from_registry()
        return self.get_status()

    # ------------------------------------------------------------ registry mgmt
    def add_or_update_provider(self, cfg: ProviderConfig) -> ProviderConfig:
        """Persist a provider config and reload the router."""
        self.registry.upsert(cfg)
        self._reload_from_registry()
        return cfg

    def remove_provider(self, name: str) -> bool:
        """Delete a custom provider (built-ins cannot be deleted)."""
        ok = self.registry.delete(name)
        if ok:
            self._reload_from_registry()
        return ok

    def set_provider_enabled(self, name: str, enabled: bool) -> bool:
        ok = self.registry.set_enabled(name, enabled)
        if ok:
            self._reload_from_registry()
        return ok

    async def test_provider(
        self,
        name: str,
        prompt: str = "Reply with the single word: pong.",
        max_tokens: int = 16,
        timeout_seconds: float = 20.0,
    ) -> Dict[str, Any]:
        """Send a tiny prompt through a specific provider for verification.

        Wrapped in :func:`asyncio.wait_for` so a stuck network call doesn't
        block the UI's spinner forever.  Default 20s — enough for cold
        starts on slow self-hosted gateways but short enough that broken
        configs surface quickly.
        """
        provider = self.providers.get(name)
        if provider is None:
            cfg = self.registry.get(name)
            if cfg is None:
                return {"success": False, "error": f"Unknown provider: {name}"}
            provider = self._build_provider(cfg)
            if provider is None:
                return {"success": False, "error": f"Cannot build provider: {name}"}

        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                provider.complete(
                    [LLMMessage(role=Role.USER, content=prompt)],
                    temperature=0.0,
                    max_tokens=max_tokens,
                ),
                timeout=timeout_seconds,
            )
            duration_ms = (time.perf_counter() - start) * 1000
            return {
                "success": True,
                "provider": name,
                "model": response.model_name,
                "duration_ms": round(duration_ms, 2),
                "content": (response.content or "")[:200],
                "tokens": response.total_tokens,
                "cost_usd": round(response.cost, 6),
            }
        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - start) * 1000
            cfg = self.registry.get(name)
            target = cfg.api_base if cfg and cfg.api_base else "the provider"
            return {
                "success": False,
                "provider": name,
                "duration_ms": round(duration_ms, 2),
                "error": f"Timed out after {int(timeout_seconds)}s waiting for {target}",
                "error_type": "TimeoutError",
                "hint": (
                    f"The provider didn't respond within {int(timeout_seconds)}s. "
                    "If it's a self-hosted gateway, make sure it's actually running "
                    "and reachable from this process; if it's a public API, the "
                    "network may be down."
                ),
                "hint_field": "api_base",
            }
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            # Tracebacks from LiteLLM/google-auth contain dozens of frames.
            # Extract a compact, actionable message:
            #   1. Look for a JSON error.message inside the body (Gemini, OpenAI)
            #   2. Otherwise keep just the first line of the exception repr.
            raw = str(exc).strip()
            short = self._extract_short_error(raw, type(exc).__name__)

            # Friendly hint for common misconfigurations.
            hint: Optional[str] = None
            hint_field: Optional[str] = None  # which form field is most likely wrong
            cfg = self.registry.get(name)
            if cfg is not None:
                model = (cfg.model or "").strip()
                lower_raw = raw.lower()

                # Connection refused / connection error to a self-hosted base
                if cfg.api_base and (
                    "connection refused" in lower_raw
                    or "connection error" in lower_raw
                    or "name resolution" in lower_raw
                    or "max retries exceeded" in lower_raw
                    or "cannot connect" in lower_raw
                ):
                    hint = (
                        f"Cannot reach api_base '{cfg.api_base}'. "
                        "Make sure that server is running and the URL "
                        "(usually ending in /v1) is correct."
                    )
                    hint_field = "api_base"
                # Vertex ADC failure — only meaningful when api_base is NOT set
                elif (
                    not cfg.api_base
                    and ("default credentials" in lower_raw or "google.auth" in lower_raw)
                ):
                    if not model.startswith("gemini/") and not model.startswith("vertex_ai/"):
                        hint = (
                            f"LiteLLM routed '{model}' to Vertex AI which needs "
                            "GCP ADC. If you meant the Google AI Studio API key, "
                            f"change the model field to 'gemini/{model}'."
                        )
                        hint_field = "model"
                # 404 / model-not-found from a self-hosted gateway — usually
                # means the model id isn't loaded on that server.
                elif cfg.api_base and (
                    "model not found" in lower_raw
                    or "is not a valid" in lower_raw
                    or "no such model" in lower_raw
                    or '"code": 404' in lower_raw
                ):
                    hint = (
                        f"Model '{model}' was rejected by the gateway at "
                        f"{cfg.api_base}. Check `GET {cfg.api_base.rstrip('/')}/models` "
                        "to see which model IDs that server actually serves."
                    )
                    hint_field = "model"
                # 401 / API key invalid
                elif "api key" in lower_raw and "not valid" in lower_raw:
                    if cfg.api_base:
                        hint = (
                            f"The gateway at {cfg.api_base} rejected the key. "
                            "Some self-hosted gateways accept any non-empty key — "
                            "try a placeholder like 'sk-local'. For others, paste "
                            "the key from that gateway's admin UI."
                        )
                    else:
                        hint = "API key was rejected by the provider. Verify the key is correct and active."
                    hint_field = "api_key"
                elif "api key" in lower_raw and not cfg.api_key:
                    hint = "No API key set for this provider."
                    hint_field = "api_key"

            payload: Dict[str, Any] = {
                "success": False,
                "provider": name,
                "duration_ms": round(duration_ms, 2),
                "error": short,
                "error_type": type(exc).__name__,
            }
            if hint:
                payload["hint"] = hint
            if hint_field:
                payload["hint_field"] = hint_field
            return payload

    # ------------------------------------------------------------ chains
    def _resolve_role(self, strategy: RoutingStrategy, task: Optional[str]) -> Optional[str]:
        """
        Translate a (strategy, task) pair into a single role string.
        Priority:
            1. explicit task name if it matches a known role
            2. mapping from strategy to role
        """
        if task:
            t = task.lower().strip()
            if t in VALID_ROLES:
                return t
        if strategy == RoutingStrategy.QUALITY_FIRST:
            return "quality"
        if strategy == RoutingStrategy.COST_OPTIMIZED:
            return "cost"
        if strategy == RoutingStrategy.FASTEST:
            return "fastest"
        if strategy == RoutingStrategy.TASK_BASED and task:
            mapped = self._TASK_HINTS.get(task.lower())
            if mapped:
                if mapped == RoutingStrategy.QUALITY_FIRST: return "quality"
                if mapped == RoutingStrategy.COST_OPTIMIZED: return "cost"
                if mapped == RoutingStrategy.FASTEST: return "fastest"
        return None

    def _get_provider_chain(
        self,
        strategy: RoutingStrategy,
        task: Optional[str] = None,
    ) -> List[str]:
        # 1. Check user-configured selection for the resolved role.
        selection = self.selection_store.get_all()
        role = self._resolve_role(strategy, task)
        explicit_first: List[str] = []
        if role and role in selection.assignments:
            chosen = selection.assignments[role]
            if chosen in self.providers:
                explicit_first.append(chosen)
        # Always honour the global default as a secondary preference.
        if "default" in selection.assignments:
            default_provider = selection.assignments["default"]
            if default_provider in self.providers and default_provider not in explicit_first:
                explicit_first.append(default_provider)

        # 2. Then fall back to the auto-built chain for the strategy.
        if strategy == RoutingStrategy.TASK_BASED and task:
            mapped = self._TASK_HINTS.get(task.lower(), RoutingStrategy.QUALITY_FIRST)
            strategy = mapped
        if strategy == RoutingStrategy.QUALITY_FIRST:
            chain = self.quality_chain + self.cost_chain
        elif strategy == RoutingStrategy.COST_OPTIMIZED:
            chain = self.cost_chain + self.quality_chain
        elif strategy == RoutingStrategy.FASTEST:
            chain = self.cost_chain
        else:
            chain = self.quality_chain + self.cost_chain

        # 3. Combine: explicit selection first, then auto chain — de-duped.
        ordered: List[str] = []
        seen = set()
        for n in explicit_first + chain:
            if n in seen or n not in self.providers:
                continue
            seen.add(n)
            ordered.append(n)
        # 4. Push unhealthy providers to the end.
        return sorted(ordered, key=lambda n: not self.stats.get(n, ProviderStats()).healthy)

    # ------------------------------------------------------------ complete
    async def complete(
        self,
        messages: List[LLMMessage],
        strategy: RoutingStrategy = RoutingStrategy.QUALITY_FIRST,
        task: Optional[str] = None,
        *,
        best_of_n: int = 1,
        best_of_selector: Optional[
            "Callable[[List[Tuple[str, LLMResponse]]], int]"
        ] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run the LLM through the resolved chain.

        Args:
            messages: The chat messages to send.
            strategy: Routing strategy (quality / cost / fastest / task-based).
            task: Optional task hint or role name.
            best_of_n: When > 1, run the first ``N`` healthy providers in
                parallel and pick the best response (STAGE 2.14). Useful for
                high-stakes tasks where redundancy + cherry-picking the
                strongest answer is worth the extra spend.
            best_of_selector: Optional ``(list[(name, response)]) -> int``
                callable returning the index of the winning response. Default
                picks the **longest non-empty response** as a cheap proxy for
                "most thorough" — good enough for code generation and
                refactor explanations. Override for evaluation-driven
                selection (e.g. compile-and-test) in higher layers.
            **kwargs: Forwarded to each provider's ``complete``.
        """
        chain = self._get_provider_chain(strategy, task)
        if not chain:
            raise ValueError("No LLM providers available in the routing chain.")

        if best_of_n > 1:
            return await self._run_best_of_n(
                chain=chain,
                messages=messages,
                strategy=strategy,
                n=best_of_n,
                selector=best_of_selector,
                **kwargs,
            )

        last_error: Optional[Exception] = None
        for provider_name in chain:
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            start = time.perf_counter()
            try:
                logger.info(
                    "Routing → %s (%s) [strategy=%s]",
                    provider_name,
                    provider.model_name,
                    strategy.value,
                )
                response = await provider.complete(messages, **kwargs)
                duration_ms = (time.perf_counter() - start) * 1000
                await self._record(
                    provider_name,
                    provider.model_name,
                    duration_ms,
                    response,
                    success=True,
                    strategy=strategy.value,
                )
                return response
            except Exception as exc:
                duration_ms = (time.perf_counter() - start) * 1000
                logger.warning(
                    "Provider %s failed in %.0fms: %s",
                    provider_name,
                    duration_ms,
                    exc,
                )
                await self._record(
                    provider_name,
                    provider.model_name,
                    duration_ms,
                    None,
                    success=False,
                    error=str(exc),
                    strategy=strategy.value,
                )
                last_error = exc

        logger.error("All providers in chain failed. Last error: %s", last_error)
        raise RuntimeError(f"LLM routing failed. Last error: {last_error}")

    # ----------------------------------------------------- best-of-N (STAGE 2.14)
    async def _run_best_of_n(
        self,
        *,
        chain: List[str],
        messages: List[LLMMessage],
        strategy: RoutingStrategy,
        n: int,
        selector: Optional[
            "Callable[[List[Tuple[str, LLMResponse]]], int]"
        ] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Race the first ``n`` providers in the chain and pick the best.

        Failures don't poison the result — they are recorded as usual and
        we just pick from whoever returned successfully. If everyone fails
        we raise, mirroring the sequential path.
        """
        candidates: List[Tuple[str, BaseLLMProvider]] = []
        seen: set = set()
        for name in chain:
            if name in seen:
                continue
            provider = self.providers.get(name)
            if provider is None:
                continue
            candidates.append((name, provider))
            seen.add(name)
            if len(candidates) >= n:
                break
        if not candidates:
            raise ValueError("No healthy providers available for best-of-N.")

        async def _one(
            name: str, provider: BaseLLMProvider
        ) -> Tuple[str, Optional[LLMResponse], Optional[Exception]]:
            start = time.perf_counter()
            try:
                response = await provider.complete(messages, **kwargs)
                duration_ms = (time.perf_counter() - start) * 1000
                await self._record(
                    name,
                    provider.model_name,
                    duration_ms,
                    response,
                    success=True,
                    strategy=f"{strategy.value}+best_of_{n}",
                )
                return name, response, None
            except Exception as exc:
                duration_ms = (time.perf_counter() - start) * 1000
                await self._record(
                    name,
                    provider.model_name,
                    duration_ms,
                    None,
                    success=False,
                    error=str(exc),
                    strategy=f"{strategy.value}+best_of_{n}",
                )
                return name, None, exc

        logger.info(
            "best-of-%d racing %s [strategy=%s]",
            n,
            [c[0] for c in candidates],
            strategy.value,
        )
        results = await asyncio.gather(
            *[_one(name, prov) for name, prov in candidates],
            return_exceptions=False,
        )
        successes: List[Tuple[str, LLMResponse]] = [
            (n_, r) for (n_, r, e) in results if r is not None and e is None
        ]
        if not successes:
            errors = [str(e) for (_n, _r, e) in results if e is not None]
            raise RuntimeError(
                f"best-of-{n} failed across all candidates: {errors}"
            )

        idx = (
            selector(successes)
            if selector is not None
            else self._default_best_of_selector(successes)
        )
        idx = max(0, min(idx, len(successes) - 1))
        winner_name, winner_response = successes[idx]
        logger.info(
            "best-of-%d winner: %s (out of %d successes)",
            n,
            winner_name,
            len(successes),
        )
        return winner_response

    @staticmethod
    def _default_best_of_selector(
        successes: List[Tuple[str, LLMResponse]],
    ) -> int:
        """Pick the longest non-empty response.

        Length is a crude but surprisingly robust proxy for thoroughness in
        code-editing / refactor tasks: degenerate responses ("ok",
        "I don't know") are short, while well-reasoned answers tend to be
        longer. Higher layers can override this via the ``best_of_selector``
        argument when they have access to ground-truth signals (compile,
        test, lint).
        """
        best_idx = 0
        best_len = -1
        for i, (_name, resp) in enumerate(successes):
            content = (resp.content or "").strip()
            if len(content) > best_len:
                best_idx = i
                best_len = len(content)
        return best_idx

    # ------------------------------------------------------------ recording
    async def _record(
        self,
        provider_name: str,
        model_name: str,
        duration_ms: float,
        response: Optional[LLMResponse],
        *,
        success: bool,
        error: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        async with self._lock:
            stats = self.stats.setdefault(provider_name, ProviderStats())
            stats.total_calls += 1
            stats.total_latency_ms += duration_ms
            ts = datetime.utcnow().isoformat()
            if success and response is not None:
                stats.success_count += 1
                stats.consecutive_failures = 0
                stats.total_tokens += response.total_tokens
                stats.total_cost_usd += response.cost
                stats.last_success_at = ts
                self.recent_calls.appendleft(
                    CallRecord(
                        provider=provider_name,
                        model=model_name,
                        timestamp=ts,
                        duration_ms=round(duration_ms, 2),
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        total_tokens=response.total_tokens,
                        cost_usd=round(response.cost, 6),
                        success=True,
                        strategy=strategy,
                    )
                )
            else:
                stats.failure_count += 1
                stats.consecutive_failures += 1
                stats.last_error = error
                stats.last_failure_at = ts
                self.recent_calls.appendleft(
                    CallRecord(
                        provider=provider_name,
                        model=model_name,
                        timestamp=ts,
                        duration_ms=round(duration_ms, 2),
                        success=False,
                        error=error,
                        strategy=strategy,
                    )
                )

    # ------------------------------------------------------------ selections
    def get_selections(self) -> Dict[str, str]:
        """Return the active role → provider mapping."""
        return self.selection_store.get_all().to_dict()

    def set_selection(self, role: str, provider_name: Optional[str]) -> None:
        """Assign a provider to a routing role (or clear it with None)."""
        if provider_name and provider_name not in self.providers and provider_name != "":
            # Allow setting a name that's currently disabled — it will become
            # active again as soon as the user re-enables the provider.
            cfg = self.registry.get(provider_name)
            if cfg is None:
                raise ValueError(f"Unknown provider: {provider_name}")
        self.selection_store.set_role(role, provider_name)

    def set_selections(self, assignments: Dict[str, str]) -> None:
        """Bulk-assign multiple roles."""
        for role, name in assignments.items():
            self.set_selection(role, name)

    # ------------------------------------------------------------ role-aware provider
    def get_provider_for(
        self,
        role: Optional[str] = None,
        strategy: RoutingStrategy = RoutingStrategy.QUALITY_FIRST,
        task: Optional[str] = None,
    ) -> Optional[BaseLLMProvider]:
        """Return the provider that will *actually* serve the given role.

        This honours user-configured selections, then the auto-built strategy
        chain, and finally health filtering.  Returns the live provider
        instance so callers (e.g. TokenManager) can ask it for its context
        window or token counter.
        """
        # If an explicit role is given, fold it into the chain resolution by
        # passing it as the task hint.
        chain = self._get_provider_chain(strategy, role or task)
        for name in chain:
            provider = self.providers.get(name)
            if provider is not None:
                return provider
        return None

    # ------------------------------------------------------------ introspection
    def list_provider_configs(self, redact_secret: bool = True) -> List[Dict[str, Any]]:
        """Return all known provider configs (including disabled ones)."""
        return [c.to_dict(redact_secret=redact_secret) for c in self.registry.list_providers()]

    def get_status(self) -> Dict[str, Any]:
        """Aggregate router state for the /model-status endpoint."""
        total_calls = sum(s.total_calls for s in self.stats.values())
        total_cost = sum(s.total_cost_usd for s in self.stats.values())
        total_tokens = sum(s.total_tokens for s in self.stats.values())
        return {
            "status": "active",
            "providers_count": len(self.providers),
            "providers": list(self.providers.keys()),
            "quality_chain": self.quality_chain,
            "cost_chain": self.cost_chain,
            "selections": self.get_selections(),
            "valid_roles": list(VALID_ROLES),
            "totals": {
                "total_calls": total_calls,
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 6),
            },
            "providers_detail": {
                name: {
                    "model": provider.model_name,
                    "type": type(provider).__name__,
                    "stats": self.stats[name].to_dict(),
                    "context_window": _safe_get_context_window(provider),
                    "config": (
                        self.configs[name].to_dict()
                        if name in self.configs else None
                    ),
                }
                for name, provider in self.providers.items()
            },
            "recent_calls": [call.__dict__ for call in list(self.recent_calls)[:10]],
        }


def _safe_get_context_window(provider: BaseLLMProvider) -> int:
    try:
        return int(provider.get_context_window() or 0)
    except Exception:
        return 0
