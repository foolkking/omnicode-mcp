"""STAGE 11.4 — Unit tests for the Smart Token Compressor."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from omnicode.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, Role
from omnicode.llm.token_manager import (
    PRESERVED_MARKERS,
    CommentStripper,
    ContextItem,
    ContextPruner,
    CostGuard,
    FunctionFolder,
    TokenManager,
)


# ---------------------------------------------------------------------------
# Fake provider — same shape as the LLMRouter tests
# ---------------------------------------------------------------------------
class _FakeProvider(BaseLLMProvider):
    def __init__(self, model_name="fake", context_window: int = 4096) -> None:
        super().__init__(model_name)
        self._cw = context_window

    async def complete(self, messages, temperature=0.1, max_tokens=None, **kw) -> LLMResponse:
        return LLMResponse(content="ok", model_name=self.model_name)

    async def stream(self, messages, temperature=0.1, max_tokens=None, **kw) -> AsyncIterator[str]:  # noqa: D401
        yield "ok"

    def count_tokens(self, text: str) -> int:
        # Cheap and deterministic: 1 token per 4 chars (rounded up).
        return max(1, (len(text) + 3) // 4)

    def get_context_window(self) -> int:
        return self._cw


# ---------------------------------------------------------------------------
# CommentStripper
# ---------------------------------------------------------------------------
class TestCommentStripper:
    def test_python_strips_hash_comments_keeps_todo(self):
        code = (
            "x = 1  # plain noise comment\n"
            "y = 2  # TODO: refactor this\n"
            "z = 3  # FIXME: edge case\n"
            "w = 4\n"
        )
        out = CommentStripper.strip(code, "python")
        assert "plain noise comment" not in out
        assert "TODO" in out
        assert "FIXME" in out
        # Code lines should still be present
        assert "x = 1" in out
        assert "z = 3" in out

    def test_python_drops_module_docstring(self):
        code = (
            '"""This is just a description."""\n'
            "import os\n"
            "x = 1\n"
        )
        out = CommentStripper.strip(code, "python")
        assert "This is just a description" not in out
        assert "import os" in out

    def test_python_keeps_docstring_with_security_marker(self):
        code = (
            '"""SECURITY: this module sanitises user input."""\n'
            "x = 1\n"
        )
        out = CommentStripper.strip(code, "python")
        # Lines containing a preserved marker must survive
        assert "SECURITY" in out

    def test_c_family_block_and_line_comments(self):
        code = (
            "int main() {\n"
            "    /* boring block */\n"
            "    int x = 1;  // discardable\n"
            "    int y = 2;  // TODO: fix\n"
            "    /* HACK: keep me */\n"
            "    return x + y;\n"
            "}\n"
        )
        out = CommentStripper.strip(code, "cpp")
        assert "boring block" not in out
        assert "discardable" not in out
        assert "TODO" in out
        assert "HACK" in out
        assert "return x + y" in out

    def test_javascript_inline_comment_inside_string_preserved(self):
        # A `//` inside a string literal must NOT be stripped.
        code = "const url = 'https://example.com/path';\nconst x = 1; // strip me\n"
        out = CommentStripper.strip(code, "javascript")
        assert "https://example.com/path" in out
        assert "strip me" not in out

    def test_html_block_comment(self):
        code = "<div>hi</div><!-- DEPRECATED legacy markup --><p>bye</p>"
        out = CommentStripper.strip(code, "html")
        # DEPRECATED is in the preserved marker list — comment should survive
        assert "DEPRECATED" in out

    def test_unknown_language_falls_back_to_c_like(self):
        code = "thing();  // boring\n"
        out = CommentStripper.strip(code, "totally-unknown-lang")
        assert "boring" not in out
        assert "thing()" in out

    def test_preserved_markers_set_is_complete(self):
        for marker in ("TODO", "FIXME", "HACK", "NOTE", "XXX",
                       "BUG", "WARNING", "DEPRECATED", "SECURITY"):
            assert marker in PRESERVED_MARKERS


# ---------------------------------------------------------------------------
# FunctionFolder
# ---------------------------------------------------------------------------
class TestFunctionFolder:
    def test_python_folds_unrelated_function(self):
        code = (
            "def keep_me(x):\n"
            "    a = 1\n"
            "    b = 2\n"
            "    return a + b\n"
            "\n"
            "def fold_me(y):\n"
            "    body_line_a = 1\n"
            "    body_line_b = 2\n"
            "    body_line_c = 3\n"
            "    return body_line_a + body_line_b + body_line_c\n"
        )
        out = FunctionFolder.fold(code, "python", keep_symbols={"keep_me"})
        # The kept function should retain its body.
        assert "a = 1" in out
        # The folded function must keep its signature but drop the body.
        assert "def fold_me" in out
        assert "body_line_a = 1" not in out

    def test_python_handles_unparseable_input(self):
        code = "def broken(\n  this is not valid python\n"
        # Must NOT raise — fall back to returning the original source unchanged
        out = FunctionFolder.fold(code, "python")
        assert out == code

    def test_c_like_folds_function_body(self):
        code = (
            "int keep_me() {\n"
            "    return 1;\n"
            "}\n"
            "\n"
            "int fold_me() {\n"
            "    int x = 1;\n"
            "    int y = 2;\n"
            "    return x + y;\n"
            "}\n"
        )
        out = FunctionFolder.fold(code, "cpp", keep_symbols={"keep_me"})
        # Both signatures should still appear
        assert "int keep_me" in out
        assert "int fold_me" in out
        # The folded body should be replaced with a placeholder
        assert "{ /* … */ }" in out or "int x = 1" not in out

    def test_empty_input(self):
        assert FunctionFolder.fold("", "python") == ""


# ---------------------------------------------------------------------------
# ContextPruner
# ---------------------------------------------------------------------------
class TestContextPruner:
    def test_high_priority_kept_first(self):
        provider = _FakeProvider(context_window=200)  # tiny window
        pruner = ContextPruner(provider, usable_ratio=1.0)

        items = [
            ContextItem(
                content="A" * 80, priority=10, role="context", language="python", label="low"
            ),
            ContextItem(
                content="B" * 80, priority=100, role="instruction", language="python", label="high"
            ),
        ]
        kept, report = pruner.prune(items)
        labels = [i.label for i in kept]
        assert "high" in labels
        # report must contain at least one action per input
        assert len(report["actions"]) == 2

    def test_pruner_returns_input_order(self):
        """The pruner sorts internally by priority but should return the
        kept items in the ORIGINAL input order."""
        provider = _FakeProvider(context_window=10_000)  # plenty of room
        pruner = ContextPruner(provider, usable_ratio=1.0)

        items = [
            ContextItem(content="x", priority=1, language="python", label="first"),
            ContextItem(content="y", priority=99, language="python", label="second"),
        ]
        kept, _ = pruner.prune(items)
        assert [i.label for i in kept] == ["first", "second"]

    def test_low_priority_dropped_when_budget_tight(self):
        # ContextPruner enforces min_window=1024 by default. To genuinely
        # test the drop path, pass min_window=10 explicitly.
        provider = _FakeProvider(context_window=20)
        pruner = ContextPruner(provider, usable_ratio=1.0, min_window=10)
        # Total usable ~= 10 tokens (4 chars/token via FakeProvider).
        items = [
            ContextItem(content="hi", priority=100, language="python", label="keep"),
            ContextItem(content="A" * 800, priority=1, language="python", label="drop"),
        ]
        kept, report = pruner.prune(items)
        # Only the high-priority short item survives.
        assert len(kept) == 1
        assert kept[0].label == "keep"
        # The dropped item shows up in the action report
        actions = {a["label"]: a["strategy"] for a in report["actions"]}
        # Could be 'drop' or 'truncate' to a tiny shred — at minimum NOT 'keep'
        assert actions.get("drop") in ("drop", "truncate", "fold", "strip")

    def test_pruner_strategy_escalation(self):
        """Force a tight enough budget that the big item gets escalated past 'keep'."""
        provider = _FakeProvider(context_window=20)
        pruner = ContextPruner(provider, usable_ratio=1.0, min_window=15)
        big = (
            "# this is a noisy comment\n"
            "def big_function():\n"
            "    line_a = 1\n"
            "    line_b = 2\n"
            "    return line_a + line_b\n"
        ) * 6
        items = [
            ContextItem(content="instr", priority=100, role="instruction"),
            ContextItem(
                content=big, priority=20, language="python", label="big_one"
            ),
        ]
        kept, report = pruner.prune(items)
        actions = {a["label"]: a["strategy"] for a in report["actions"]}
        # The big item must NOT survive as 'keep' under a 15-token budget.
        assert actions.get("big_one") in ("strip", "fold", "truncate", "drop")

    def test_token_count_helper(self):
        provider = _FakeProvider()
        pruner = ContextPruner(provider)
        # Empty string is 0 tokens
        assert pruner.count_tokens("") == 0
        # Non-empty string > 0
        assert pruner.count_tokens("hello world") > 0


# ---------------------------------------------------------------------------
# CostGuard
# ---------------------------------------------------------------------------
class TestCostGuard:
    def test_check_messages_under_cap(self):
        provider = _FakeProvider(context_window=10_000)
        guard = CostGuard(provider)
        result = guard.check_messages(
            [LLMMessage(role=Role.USER, content="short prompt")]
        )
        assert result["ok"] is True
        assert result["warning"] is None
        assert result["total_tokens"] > 0

    def test_check_messages_over_cap(self):
        provider = _FakeProvider(context_window=200)  # tiny
        guard = CostGuard(provider, hard_cap_tokens=10)
        result = guard.check_messages(
            [LLMMessage(role=Role.USER, content="A" * 1000)]
        )
        assert result["ok"] is False
        assert result["warning"] is not None
        assert "chunked dispatch required" in result["warning"]

    def test_chunk_text_returns_chunks(self):
        provider = _FakeProvider()
        guard = CostGuard(provider)
        chunks = guard.chunk_text("X" * 5000, max_chunk_tokens=100)
        assert len(chunks) >= 2
        # Each chunk should be a non-empty string
        for c in chunks:
            assert isinstance(c, str) and c

    def test_chunk_text_short_input_returns_one_chunk(self):
        provider = _FakeProvider()
        guard = CostGuard(provider)
        chunks = guard.chunk_text("hello world", max_chunk_tokens=4096)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Façade — TokenManager
# ---------------------------------------------------------------------------
class TestTokenManagerFacade:
    def test_compress_for_llm_returns_pruned_items(self):
        tm = TokenManager(_FakeProvider(context_window=400))
        items = [
            ContextItem(content="instr", priority=100, role="instruction"),
            ContextItem(content="A" * 200, priority=10, language="python"),
        ]
        kept, report = tm.compress_for_llm(items, reserved_tokens=10)
        assert isinstance(kept, list)
        assert "actions" in report
        assert report["items_in"] == 2
        assert report["items_kept"] == len(kept)

    def test_for_role_uses_router_pinned_provider(self):
        """TokenManager.for_role must construct the manager around the
        provider that the router resolves for the requested role."""
        wide  = _FakeProvider(model_name="wide-model", context_window=100_000)
        slim  = _FakeProvider(model_name="slim-model", context_window=4_096)
        # Mock router stub — emulates LLMRouter.get_provider_for
        class StubRouter:
            providers = {"wide": wide, "slim": slim}

            def get_provider_for(self, role=None, strategy=None, task=None):
                if role == "edit":
                    return slim
                return wide

        tm = TokenManager.for_role(StubRouter(), role="edit")
        info = tm.budget_info()
        assert info["model"] == "slim-model"
        assert info["max_window"] == 4_096

    def test_for_role_handles_no_get_provider_for_attr(self):
        """When the router doesn't expose get_provider_for, fall back to any
        provider that's available."""
        wide = _FakeProvider(model_name="only-model", context_window=8_192)
        class MinimalRouter:
            providers = {"only": wide}

        tm = TokenManager.for_role(MinimalRouter(), role="edit")
        assert tm.budget_info()["model"] == "only-model"

    def test_for_role_raises_when_router_has_nothing(self):
        class EmptyRouter:
            providers = {}

        with pytest.raises(ValueError, match="no providers"):
            TokenManager.for_role(EmptyRouter(), role="edit")

    def test_count_tokens_consistent(self):
        tm = TokenManager(_FakeProvider())
        assert tm.count_tokens("") == 0
        assert tm.count_tokens("hello") == tm.pruner.count_tokens("hello")

    def test_compress_context_legacy_api(self):
        """The legacy ``compress_context`` API should still work."""
        tm = TokenManager(_FakeProvider(context_window=10_000))
        items = [
            {"content": "the quick brown fox", "priority": 10, "id": "x"},
        ]
        out = tm.compress_context(items, query="search query", language="python")
        assert isinstance(out, list)
        assert len(out) == 1
        assert "content" in out[0]
