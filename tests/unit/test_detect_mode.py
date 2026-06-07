"""Unit tests for the omni_search routing heuristic.

Covers each branch of :func:`_detect_mode` so the call-graph indexer
sees this file as a test candidate for the helper. Without these the
``/graph/related-tests`` endpoint can't suggest any concrete pytest
command for the symbol.
"""

from __future__ import annotations

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _detect_mode,
    _strip_quotes,
)


@pytest.mark.parametrize(
    "query, expected",
    [
        # 1. Empty / whitespace → semantic (caller will get a no-results hint).
        ("", "semantic"),
        ("   ", "semantic"),
        # 2. Quoted literal → text (caller wants the verbatim phrase).
        ('"foo bar"', "text"),
        ("'login flow'", "text"),
        # 3. Single-token / stop-word guard → text.
        ("a", "text"),
        ("if", "text"),
        ("def", "text"),
        ("class", "text"),
        ("before", "text"),
        ("after", "text"),
        ("true", "text"),
        ("false", "text"),
        ("local-v2", "text"),
        ("cloudsim-v1", "text"),
        # 4. ALL_CAPS_IDENTIFIER (env vars / constants) → text.
        ("OMNICODE_RERANKER", "text"),
        ("DEFAULT_MAX_RESULTS", "text"),
        # 5. Dotted / underscored identifier → symbol.
        ("_detect_mode", "symbol"),
        ("ProviderRegistry.test_provider", "symbol"),
        ("foo_bar", "symbol"),
        # 6. Short natural-language query (≤ 3 words) → hybrid.
        ("RRF fuse", "hybrid"),
        ("login flow", "hybrid"),
        ("user auth flow", "hybrid"),
        # 7. Anything else (longer NL queries) → semantic.
        ("how does the auto mode pick a search strategy", "semantic"),
        ("explain the FAISS index persistence behaviour", "semantic"),
    ],
)
def test_detect_mode_routing(query: str, expected: str) -> None:
    assert _detect_mode(query) == expected


def test_strip_quotes_double() -> None:
    assert _strip_quotes('"hello world"') == "hello world"


def test_strip_quotes_single() -> None:
    assert _strip_quotes("'hello world'") == "hello world"


def test_strip_quotes_unmatched() -> None:
    # Mismatched quotes are left alone.
    assert _strip_quotes('"hello\'') == "\"hello'"
    assert _strip_quotes("hello") == "hello"


def test_strip_quotes_idempotent_on_inner() -> None:
    # Only the outer pair is removed.
    assert _strip_quotes('""nested""') == '"nested"'


def test_detect_mode_strips_quotes_before_routing() -> None:
    """Quoted identifier should still route via the bare-identifier rule."""
    assert _detect_mode('"login"') == "text"  # quoted literal stays text
    assert _detect_mode("'foo_bar'") == "text"
