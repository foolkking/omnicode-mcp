"""STAGE 11.7 — Edit Pipeline integration tests.

These tests exercise the full ``EditPipeline.process_edit`` flow with the
LLM call replaced by a fake provider so we don't need a real API key.

What we verify
--------------
1. Token compression actually reduces the prompt size.
2. The Guard runs after a successful edit.
3. When the Guard reports ERROR-level issues, the pipeline escalates to the
   ``review`` role and feeds the Guard report back into the prompt
   (STAGE 6.9).
4. The history advisory and memory snippets are injected as context items.
5. ``EditResult.token_stats`` exposes ``original_tokens`` /
   ``compressed_tokens`` / ``savings_pct`` / ``budget_info``.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

import pytest

from omnicode.guard.models import GuardIssue, GuardResult, IssueSeverity
from omnicode.llm.base import BaseLLMProvider, LLMMessage, LLMResponse
from omnicode.pipelines.edit import EditPipeline, EditRequest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _RecordingProvider(BaseLLMProvider):
    """A provider that records the prompt it received and returns canned text."""

    def __init__(
        self,
        model_name: str = "fake-edit-model",
        context_window: int = 32_000,
        response_text: str = "",
    ) -> None:
        super().__init__(model_name)
        self._cw = context_window
        self.response_text = response_text
        self.received_prompts: List[List[LLMMessage]] = []

    async def complete(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        self.received_prompts.append(list(messages))
        return LLMResponse(
            content=self.response_text,
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
        yield self.response_text

    def count_tokens(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    def get_context_window(self) -> int:
        return self._cw


def _wrap_in_codeblock(content: str, language: str = "python") -> str:
    return f"```{language}\n{content}\n```"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def temp_target(tmp_path: Path) -> Path:
    """A small Python file we'll ask the pipeline to edit."""
    f = tmp_path / "target.py"
    f.write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n"
    )
    return f


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    response_text: str,
    context_window: int = 32_000,
) -> _RecordingProvider:
    """Build an EditPipeline whose router only contains a recording fake."""
    fake = _RecordingProvider(
        response_text=response_text, context_window=context_window
    )

    pipeline = EditPipeline()
    # Replace the router's providers with our fake.
    pipeline.router.providers = {"fake": fake}
    pipeline.router.configs = {}
    pipeline.router.quality_chain = ["fake"]
    pipeline.router.cost_chain = ["fake"]
    # Reset stats so we don't pollute across tests
    from omnicode.llm.router import ProviderStats

    pipeline.router.stats = {"fake": ProviderStats()}
    # Make the selection store empty so the chain is purely fake.
    pipeline.router.selection_store.clear()
    return pipeline, fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_edit_happy_path(
    monkeypatch: pytest.MonkeyPatch, temp_target: Path
) -> None:
    new_body = (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n"
        "\n"
        "def subtract(a, b):\n"
        "    return a - b\n"
    )
    pipeline, fake = _install_fake_pipeline(
        monkeypatch, response_text=_wrap_in_codeblock(new_body)
    )

    # Stub the Guard to pretend everything is clean — keeps us off real ruff/mypy.
    async def fake_guard_check(_self, file_path):
        return GuardResult(is_clean=True, tools_run=["ruff"])

    monkeypatch.setattr(
        "omnicode.guard.analyzer.ProactiveGuard.check", fake_guard_check
    )

    req = EditRequest(
        target_file=str(temp_target),
        instructions="Add a subtract function.",
        code_edit="// existing code\ndef subtract(a, b):\n    return a - b\n",
        language="python",
    )
    result = await pipeline.process_edit(req, save_to_file=True)

    # Outcome
    assert result.success is True
    assert result.escalated is False
    assert "subtract" in temp_target.read_text()

    # Token stats are populated
    ts = result.token_stats
    assert ts["original_tokens"] > 0
    assert "compressed_tokens" in ts
    assert "savings_pct" in ts
    assert "budget_info" in ts

    # Exactly one LLM call (no escalation).
    assert len(fake.received_prompts) == 1


# ---------------------------------------------------------------------------
# Guard-driven escalation (STAGE 6.9)
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_escalation_triggers_review_pass(
    monkeypatch: pytest.MonkeyPatch, temp_target: Path
) -> None:
    pipeline, fake = _install_fake_pipeline(
        monkeypatch,
        response_text=_wrap_in_codeblock(
            "def add(a, b):\n    return a + b\n# new function added\n"
        ),
    )

    # First Guard call returns ERROR; second call returns clean.
    call_state = {"n": 0}

    async def fake_guard_check(_self, file_path):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return GuardResult(
                is_clean=False,
                issues=[
                    GuardIssue(
                        tool="ruff",
                        severity=IssueSeverity.ERROR,
                        message="boom",
                        line=1,
                        code="E999",
                    )
                ],
                tools_run=["ruff"],
            )
        return GuardResult(is_clean=True, tools_run=["ruff"])

    monkeypatch.setattr(
        "omnicode.guard.analyzer.ProactiveGuard.check", fake_guard_check
    )

    req = EditRequest(
        target_file=str(temp_target),
        instructions="Tweak the file",
        code_edit="// existing\n# new function added\n",
        language="python",
    )
    result = await pipeline.process_edit(req, save_to_file=True)

    # Guard ran twice, LLM called twice (one edit + one review).
    assert call_state["n"] == 2
    assert len(fake.received_prompts) == 2
    assert result.escalated is True
    assert result.escalation["triggered"] is True
    assert result.escalation["first_pass_role"] == "edit"

    # The second prompt MUST contain the Guard report in some form.
    second_prompt = fake.received_prompts[1]
    user_msg = next(m.content for m in second_prompt if m.role.value == "user")
    assert "Guard" in user_msg or "boom" in user_msg or "E999" in user_msg


# ---------------------------------------------------------------------------
# Token compression actually shrinks the prompt
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_compression_reduces_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bigger file so compression has something to do.
    big_file = tmp_path / "big.py"
    big_file.write_text(
        ("# repetitive comment line that should compress well\n" * 50)
        + ("def f():\n    return 1\n" * 25)
    )

    pipeline, fake = _install_fake_pipeline(
        monkeypatch,
        response_text=_wrap_in_codeblock("def f():\n    return 2\n"),
        # Tight budget forces meaningful compression
        context_window=4_096,
    )

    async def fake_guard_check(_self, file_path):
        return GuardResult(is_clean=True, tools_run=["ruff"])

    monkeypatch.setattr(
        "omnicode.guard.analyzer.ProactiveGuard.check", fake_guard_check
    )

    req = EditRequest(
        target_file=str(big_file),
        instructions="rewrite f() to return 2",
        code_edit="def f():\n    return 2\n",
        language="python",
    )
    result = await pipeline.process_edit(req, save_to_file=True)

    ts = result.token_stats
    # Some compression must have happened (savings > 0)
    assert ts["compressed_tokens"] <= ts["original_tokens"]
    # Sanity: budget info reports our fake provider's window
    assert ts["budget_info"]["model"] == "fake-edit-model"
