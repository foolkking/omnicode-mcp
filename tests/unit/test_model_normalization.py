"""Unit tests for LLMRouter._normalize_model_name.

Background:
    LiteLLM picks its backend from the model-name *prefix*.  A bare
    ``gemini-1.5-flash`` defaults to Vertex AI, which needs GCP ADC creds
    and dies on most user machines with ``DefaultCredentialsError``.
    The router therefore normalises model names based on the configured
    provider_type so users typing the friendly short name still hit the
    right backend.
"""
from __future__ import annotations

import pytest

from omnicode.llm.provider_registry import ProviderConfig
from omnicode.llm.router import LLMRouter


@pytest.mark.parametrize(
    "model,provider_type,expected",
    [
        # Bare gemini -> google AI Studio (most common UI typo)
        ("gemini-3.5-flash",       "gemini",            "gemini/gemini-3.5-flash"),
        ("gemini-1.5-pro",         "gemini",            "gemini/gemini-1.5-pro"),
        ("gemini-2.5-flash",       "google-ai-studio",  "gemini/gemini-2.5-flash"),
        # Already-prefixed names pass through untouched
        ("gemini/gemini-1.5-pro",  "gemini",            "gemini/gemini-1.5-pro"),
        ("vertex_ai/gemini-2.0",   "vertex_ai",         "vertex_ai/gemini-2.0"),
        # Anthropic
        ("claude-3-opus-20240229", "anthropic",         "claude-3-opus-20240229"),
        ("anthropic/claude-3-haiku","anthropic",        "anthropic/claude-3-haiku"),
        # OpenAI bare ids
        ("gpt-4o",                 "openai",            "gpt-4o"),
        ("gpt-4o-mini",            "openai-compatible", "gpt-4o-mini"),
        # DeepSeek
        ("deepseek-chat",          "deepseek",          "deepseek/deepseek-chat"),
        ("deepseek/deepseek-coder","deepseek",          "deepseek/deepseek-coder"),
        # Ollama
        ("llama3",                 "ollama",            "ollama/llama3"),
        # Gemini-* heuristic fires even when the user mis-set the type
        # (provided no api_base is set — see test_api_base_overrides_heuristic)
        ("gemini-1.5-flash",       "openai-compatible", "gemini/gemini-1.5-flash"),
        # Empty model => empty (defensive)
        ("",                       "gemini",            ""),
        # Whitespace gets stripped
        ("  gpt-4o  ",             "openai",            "gpt-4o"),
    ],
)
def test_normalize_model_name(model, provider_type, expected):
    cfg = ProviderConfig(name="t", model=model, provider_type=provider_type)
    assert LLMRouter._normalize_model_name(cfg) == expected


@pytest.mark.parametrize(
    "model,api_base,expected",
    [
        # Self-hosted OpenAI-compatible Gemini gateway → MUST go through
        # openai/ transport, NOT google's public domain.
        ("gemini-2.5-pro",      "http://127.0.0.1:2048/v1", "openai/gemini-2.5-pro"),
        ("gemini-1.5-flash",    "http://127.0.0.1:2048/v1", "openai/gemini-1.5-flash"),
        # Ollama-style local server with custom name
        ("llama3",              "http://localhost:11434/v1","openai/llama3"),
        # vLLM / LM Studio
        ("Qwen/Qwen2.5-7B",     "http://localhost:8000/v1", "openai/Qwen/Qwen2.5-7B"),
        # Already-prefixed openai/ stays as-is
        ("openai/gpt-4o",       "http://localhost:8000/v1", "openai/gpt-4o"),
        # Custom corporate gateway with claude name shouldn't go to Anthropic.
        ("claude-3-opus",       "https://corp.example/v1",  "openai/claude-3-opus"),
    ],
)
def test_api_base_overrides_heuristic(model, api_base, expected):
    """When the user sets api_base they explicitly want a self-hosted /
    OpenAI-compatible endpoint.  We must NOT route to a public provider
    just because the model name starts with `gemini-` or `claude-`."""
    cfg = ProviderConfig(
        name="local-gateway",
        model=model,
        api_base=api_base,
        provider_type="openai-compatible",
    )
    assert LLMRouter._normalize_model_name(cfg) == expected


def test_unknown_provider_type_falls_back_to_bare():
    cfg = ProviderConfig(name="x", model="some-random-model", provider_type="totally-made-up")
    assert LLMRouter._normalize_model_name(cfg) == "some-random-model"


def test_openai_compatible_with_already_prefixed_name():
    """Already-prefixed names pass through even with custom api_base."""
    cfg = ProviderConfig(
        name="custom",
        model="my-org/llama-70b",
        provider_type="openai-compatible",
        api_base="https://api.example.com/v1",
    )
    # Already contains "/" so it's treated as already-routable.  The api_base
    # branch sees "openai/" isn't the prefix but adds it anyway since the
    # rule is: api_base set → openai/ prefix.
    got = LLMRouter._normalize_model_name(cfg)
    assert got == "openai/my-org/llama-70b"



# ---------------------------------------------------------------------------
# Placeholder API key filter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        # Empty / very short -> not real
        (None, False),
        ("", False),
        ("short", False),
        # Common placeholders shipped in .env.example
        ("your_openai_api_key_here", False),
        ("your_anthropic_api_key_here", False),
        ("your-gemini-key", False),
        ("<your-key-here>", False),
        ("PLACEHOLDER", False),
        ("replace_me", False),
        ("change-me-please", False),
        ("xxxxxxxxx", False),
        ("example-key", False),
        ("api_key_goes_here", False),
        # Real-looking keys -> accepted
        ("sk-proj-abcd1234efgh5678ijkl", True),
        ("AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ", True),
        ("sk-ant-api03-aBcDe-_fghi", True),
        ("ds-12345abcdef", True),
        # Edge case: short but real-looking? still rejected (< 8 chars)
        ("ab12cd", False),
        ("ab12cd34", True),
    ],
)
def test_is_real_key(value, expected):
    assert LLMRouter._is_real_key(value) is expected
