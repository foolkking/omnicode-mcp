"""STAGE 11.1 — Unit tests for LLMRouter routing & selection logic.

These tests use a tiny in-memory `FakeProvider` so no LiteLLM / network
traffic is required. The LLMRouter is constructed with an injected
ProviderRegistry / ProviderSelectionStore that point at fresh tmp DBs.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict, List, Optional

import pytest

from omnicode.llm.base import BaseLLMProvider, LLMMessage, LLMResponse
from omnicode.llm.provider_registry import ProviderConfig, ProviderRegistry
from omnicode.llm.provider_selection import ProviderSelectionStore
from omnicode.llm.router import LLMRouter, RoutingStrategy


# ---------------------------------------------------------------------------
# Fake provider — never touches the network
# ---------------------------------------------------------------------------
class FakeProvider(BaseLLMProvider):
    """Deterministic provider used by the LLMRouter unit tests."""

    def __init__(
        self,
        model_name: str = "fake-model",
        api_key: Optional[str] = None,
        context_window: int = 4096,
        fail: bool = False,
        response_text: str = "ok",
    ) -> None:
        super().__init__(model_name, api_key)
        self._context_window = context_window
        self._fail = fail
        self._response_text = response_text
        self.calls = 0

    async def complete(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"Fake provider {self.model_name} configured to fail")
        return LLMResponse(
            content=self._response_text,
            model_name=self.model_name,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost=0.0,
        )

    async def stream(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        if self._fail:
            raise RuntimeError(f"Fake provider {self.model_name} configured to fail")
        yield self._response_text

    def count_tokens(self, text: str) -> int:
        # 4-char-per-token approximation is fine for unit tests
        return max(1, len(text) // 4)

    def get_context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_router(
    tmp_path,
    *,
    providers: List[ProviderConfig],
    selections: Optional[Dict[str, str]] = None,
    fake_overrides: Optional[Dict[str, FakeProvider]] = None,
) -> LLMRouter:
    """Construct an LLMRouter wired to fresh per-test SQLite stores."""
    reg = ProviderRegistry(str(tmp_path / "providers.db"))
    sel = ProviderSelectionStore(str(tmp_path / "selections.db"))
    for cfg in providers:
        reg.upsert(cfg)
    for role, name in (selections or {}).items():
        sel.set_role(role, name)

    # Avoid env-key built-ins polluting the test by stubbing the sync method.
    router = LLMRouter.__new__(LLMRouter)
    router.settings = type("S", (), {
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "DEFAULT_LLM_MODEL": "fake-model",
    })()
    router.providers = {}
    router.configs = {}
    router.quality_chain = []
    router.cost_chain = []
    router.stats = {}
    from collections import deque
    router.recent_calls = deque(maxlen=32)
    router._lock = asyncio.Lock()
    router.registry = reg
    router.selection_store = sel
    # Override _build_provider so we never try to construct a real LiteLLM
    # provider during unit tests.
    fakes = fake_overrides or {}
    def fake_builder(cfg: ProviderConfig):
        if cfg.name in fakes:
            return fakes[cfg.name]
        return FakeProvider(model_name=cfg.model)
    router._build_provider = fake_builder  # type: ignore[assignment]
    router._reload_from_registry()
    return router


# ---------------------------------------------------------------------------
# Routing chain assembly
# ---------------------------------------------------------------------------
class TestRoutingChain:
    def test_quality_chain_priority(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="claude", model="claude-3", group="quality"),
                ProviderConfig(name="cheap",  model="cheap-1",  group="cost"),
            ],
        )
        chain = router._get_provider_chain(RoutingStrategy.QUALITY_FIRST)
        assert chain[0] == "claude"
        # cost provider eventually appears as fallback
        assert "cheap" in chain

    def test_cost_chain_priority(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="quality1", model="q-1", group="quality"),
                ProviderConfig(name="cheap1",   model="c-1", group="cost"),
            ],
        )
        chain = router._get_provider_chain(RoutingStrategy.COST_OPTIMIZED)
        assert chain[0] == "cheap1"

    def test_fastest_uses_cost_chain_only(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="big",   model="big",   group="quality"),
                ProviderConfig(name="small", model="small", group="cost"),
            ],
        )
        chain = router._get_provider_chain(RoutingStrategy.FASTEST)
        assert "small" in chain
        # quality-only provider should be filtered out under FASTEST
        assert "big" not in chain

    def test_balanced_provider_in_both_chains(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="bal", model="bal", group="balanced"),
            ],
        )
        chain_q = router._get_provider_chain(RoutingStrategy.QUALITY_FIRST)
        chain_c = router._get_provider_chain(RoutingStrategy.COST_OPTIMIZED)
        assert "bal" in chain_q
        assert "bal" in chain_c

    def test_explicit_role_promotion(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="quality1", model="q-1", group="quality"),
                ProviderConfig(name="my-edit",  model="custom-edit", group="cost"),
            ],
            selections={"edit": "my-edit"},
        )
        chain = router._get_provider_chain(RoutingStrategy.TASK_BASED, task="edit")
        # Explicit selection MUST sit at index 0
        assert chain[0] == "my-edit"

    def test_default_assignment_is_secondary(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="primary",   model="p", group="quality"),
                ProviderConfig(name="my-default", model="d", group="cost"),
            ],
            selections={"default": "my-default"},
        )
        chain = router._get_provider_chain(RoutingStrategy.QUALITY_FIRST)
        # default provider should be promoted ahead of the auto-built quality chain
        assert chain[0] == "my-default"

    def test_unhealthy_pushed_to_back(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
            ],
        )
        # Mark provider 'a' as unhealthy.
        router.stats["a"].consecutive_failures = 5
        chain = router._get_provider_chain(RoutingStrategy.QUALITY_FIRST)
        # Healthy 'b' should now precede unhealthy 'a'.
        assert chain.index("b") < chain.index("a")


# ---------------------------------------------------------------------------
# get_provider_for + budget-aware lookup (STAGE 4.7)
# ---------------------------------------------------------------------------
class TestGetProviderFor:
    def test_returns_pinned_provider(self, tmp_path):
        wide = FakeProvider(model_name="wide-model", context_window=200_000)
        slim = FakeProvider(model_name="slim-model", context_window=8_192)
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="wide", model="wide", group="quality"),
                ProviderConfig(name="slim", model="slim", group="cost"),
            ],
            selections={"edit": "slim"},
            fake_overrides={"wide": wide, "slim": slim},
        )
        provider = router.get_provider_for(role="edit")
        assert provider.model_name == "slim-model"
        assert provider.get_context_window() == 8_192

    def test_falls_back_to_chain_when_unset(self, tmp_path):
        wide = FakeProvider(model_name="wide-model", context_window=200_000)
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="wide", model="wide", group="quality"),
            ],
            fake_overrides={"wide": wide},
        )
        provider = router.get_provider_for(role="edit")
        assert provider.model_name == "wide-model"


# ---------------------------------------------------------------------------
# complete() - fallback semantics
# ---------------------------------------------------------------------------
class TestComplete:
    @pytest.mark.asyncio
    async def test_first_provider_wins(self, tmp_path):
        ok = FakeProvider(model_name="ok", response_text="hello")
        backup = FakeProvider(model_name="backup", response_text="should-not-run")
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="ok",     model="ok",     group="quality"),
                ProviderConfig(name="backup", model="backup", group="quality"),
            ],
            # Pin "ok" as the default so the chain is deterministic regardless
            # of registry sort order (which is alphabetical by name).
            selections={"default": "ok"},
            fake_overrides={"ok": ok, "backup": backup},
        )
        resp = await router.complete([LLMMessage(role="user", content="hi")])
        assert resp.content == "hello"
        assert ok.calls == 1 and backup.calls == 0
        assert router.stats["ok"].success_count == 1

    @pytest.mark.asyncio
    async def test_falls_through_to_backup_on_failure(self, tmp_path):
        broken = FakeProvider(model_name="broken", fail=True)
        backup = FakeProvider(model_name="backup", response_text="rescued")
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="broken", model="broken", group="quality"),
                ProviderConfig(name="backup", model="backup", group="quality"),
            ],
            # Pin "broken" first so we know it gets tried before "backup"
            selections={"default": "broken"},
            fake_overrides={"broken": broken, "backup": backup},
        )
        resp = await router.complete([LLMMessage(role="user", content="hi")])
        assert resp.content == "rescued"
        assert router.stats["broken"].failure_count == 1
        assert router.stats["broken"].consecutive_failures == 1
        assert router.stats["backup"].success_count == 1

    @pytest.mark.asyncio
    async def test_all_fail_raises(self, tmp_path):
        a = FakeProvider(model_name="a", fail=True)
        b = FakeProvider(model_name="b", fail=True)
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
            ],
            fake_overrides={"a": a, "b": b},
        )
        with pytest.raises(RuntimeError, match="LLM routing failed"):
            await router.complete([LLMMessage(role="user", content="hi")])


# ---------------------------------------------------------------------------
# Selection helpers exposed on the Router
# ---------------------------------------------------------------------------
class TestRouterSelectionHelpers:
    def test_set_selection_unknown_raises(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[ProviderConfig(name="a", model="a")],
        )
        with pytest.raises(ValueError, match="Unknown provider"):
            router.set_selection("edit", "ghost-provider")

    def test_set_selection_disabled_provider_allowed(self, tmp_path):
        """We should be able to pin a disabled provider — it'll re-activate
        as soon as the user re-enables it."""
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="x", model="x", enabled=False),
            ],
        )
        # Disabled providers are still in the registry, just absent from
        # router.providers — set_selection should accept them.
        router.set_selection("edit", "x")
        assert router.get_selections().get("edit") == "x"

    def test_set_selections_bulk(self, tmp_path):
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a"),
                ProviderConfig(name="b", model="b"),
            ],
        )
        router.set_selections({"edit": "a", "scan": "b", "review": "a"})
        sel = router.get_selections()
        assert sel == {"edit": "a", "scan": "b", "review": "a"}



# ---------------------------------------------------------------------------
# Best-of-N parallel routing (STAGE 2.14)
# ---------------------------------------------------------------------------
class TestBestOfN:
    @pytest.mark.asyncio
    async def test_runs_all_candidates_in_parallel(self, tmp_path):
        a = FakeProvider(model_name="a", response_text="short")
        b = FakeProvider(
            model_name="b",
            response_text="this answer is significantly longer than a's",
        )
        c = FakeProvider(model_name="c", response_text="medium answer here")
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
                ProviderConfig(name="c", model="c", group="quality"),
            ],
            selections={"default": "a"},  # pin order: a, then auto-chain
            fake_overrides={"a": a, "b": b, "c": c},
        )
        resp = await router.complete(
            [LLMMessage(role="user", content="hi")], best_of_n=3
        )
        # Default selector picks the longest non-empty response.
        assert resp.content == "this answer is significantly longer than a's"
        # All three candidates were called once.
        assert a.calls == 1 and b.calls == 1 and c.calls == 1

    @pytest.mark.asyncio
    async def test_custom_selector_overrides_default(self, tmp_path):
        a = FakeProvider(model_name="a", response_text="zzz long but boring")
        b = FakeProvider(model_name="b", response_text="short but right")
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
            ],
            selections={"default": "a"},
            fake_overrides={"a": a, "b": b},
        )

        # Selector that prefers the response NOT containing "boring"
        def picky(items):
            for i, (_n, r) in enumerate(items):
                if "boring" not in (r.content or ""):
                    return i
            return 0

        resp = await router.complete(
            [LLMMessage(role="user", content="hi")],
            best_of_n=2,
            best_of_selector=picky,
        )
        assert resp.content == "short but right"

    @pytest.mark.asyncio
    async def test_one_failure_doesnt_kill_the_race(self, tmp_path):
        good = FakeProvider(model_name="good", response_text="rescue")
        broken = FakeProvider(model_name="broken", fail=True)
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="good", model="good", group="quality"),
                ProviderConfig(name="broken", model="broken", group="quality"),
            ],
            selections={"default": "good"},
            fake_overrides={"good": good, "broken": broken},
        )
        resp = await router.complete(
            [LLMMessage(role="user", content="hi")], best_of_n=2
        )
        assert resp.content == "rescue"

    @pytest.mark.asyncio
    async def test_all_fail_raises(self, tmp_path):
        a = FakeProvider(model_name="a", fail=True)
        b = FakeProvider(model_name="b", fail=True)
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
            ],
            selections={"default": "a"},
            fake_overrides={"a": a, "b": b},
        )
        with pytest.raises(RuntimeError, match="best-of-2 failed"):
            await router.complete(
                [LLMMessage(role="user", content="hi")], best_of_n=2
            )

    @pytest.mark.asyncio
    async def test_n_equals_one_uses_sequential_path(self, tmp_path):
        """best_of_n=1 should behave EXACTLY like a normal call (no race)."""
        a = FakeProvider(model_name="a", response_text="hello")
        b = FakeProvider(model_name="b", response_text="should-not-run")
        router = _build_router(
            tmp_path,
            providers=[
                ProviderConfig(name="a", model="a", group="quality"),
                ProviderConfig(name="b", model="b", group="quality"),
            ],
            selections={"default": "a"},
            fake_overrides={"a": a, "b": b},
        )
        resp = await router.complete(
            [LLMMessage(role="user", content="hi")], best_of_n=1
        )
        assert resp.content == "hello"
        assert a.calls == 1 and b.calls == 0

    def test_default_selector_picks_longest(self):
        from omnicode.llm.router import LLMRouter

        items = [
            ("a", LLMResponse(content="short", model_name="a")),
            ("b", LLMResponse(content="this is a longer answer", model_name="b")),
            ("c", LLMResponse(content="med len", model_name="c")),
        ]
        idx = LLMRouter._default_best_of_selector(items)
        assert idx == 1
