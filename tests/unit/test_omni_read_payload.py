"""Unit tests for the omni_read MCP tool's payload builder.

Covers the contract changes made when fixing the omni_read regression:
* Structured JSON output (default ``format=json``).
* ``token_estimate`` and ``truncated`` fields populated for every mode.
* ``mode=range`` without ``start_line`` returns a structured error
  rather than silently falling back to ``full``.
* ``mode=relevant_chunks`` requires ``query`` and forwards it.
* ``mode=full`` truncates above the soft token budget with a clear hint.
* Outline rendering surfaces both top-level and nested symbols.

These tests exercise the helpers directly so they don't need to spin up
the FastAPI app (whose ``.data`` shards may be locked by a running MCP
server).
"""
from __future__ import annotations

import json

from omnicode_adapters.mcp_server.high_level_tools import (
    _approx_token_count,
    _build_read_payload,
    _emit_read_error,
    _format_outline_text,
    _truncate_with_lines,
)


# ---------------------------------------------------------------------------
# _emit_read_error
# ---------------------------------------------------------------------------
def test_emit_read_error_json_shape():
    raw = _emit_read_error(file="x.py", mode="range", error="missing start_line", fmt="json")
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["file"] == "x.py"
    assert payload["mode"] == "range"
    assert "missing start_line" in payload["error"]


def test_emit_read_error_text_shape():
    out = _emit_read_error(file="x.py", mode="range", error="missing start_line", fmt="text")
    assert out.startswith("❌")
    assert "range" in out
    assert "missing" in out


# ---------------------------------------------------------------------------
# _truncate_with_lines
# ---------------------------------------------------------------------------
def test_truncate_with_lines_under_budget_returns_unchanged():
    content = "a\nb\nc\n"
    out, was_trunc, kept = _truncate_with_lines(content, max_tokens=1000)
    assert out == content
    assert was_trunc is False


def test_truncate_with_lines_above_budget_cuts_on_line_boundary():
    big = ("hello world line\n" * 5000)
    out, was_trunc, kept = _truncate_with_lines(big, max_tokens=200)
    assert was_trunc is True
    assert _approx_token_count(out) <= 220  # ~200 with a small safety margin
    # cut on a newline so we never split mid-line
    assert out.endswith("hello world line") or out.endswith("\n") or out == ""


# ---------------------------------------------------------------------------
# _build_read_payload — outline mode
# ---------------------------------------------------------------------------
def test_build_payload_outline_includes_symbols_and_token_estimate():
    backend = {
        "language": "python",
        "total_lines": 120,
        "symbols": [
            {
                "name": "create_app",
                "kind": "function",
                "lines": [10, 25],
                "signature": "def create_app() -> FastAPI:",
                "doc": "Create the FastAPI app.",
            },
            {
                "name": "lifespan",
                "kind": "function",
                "lines": [27, 35],
                "parent": "create_app",
            },
        ],
        "symbol_count": 2,
    }
    payload = _build_read_payload(
        file="main.py",
        requested_mode="outline",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query=None,
        max_tokens=8000,
    )
    assert payload["ok"] is True
    assert payload["file"] == "main.py"
    assert payload["mode"] == "outline"
    assert payload["language"] == "python"
    assert payload["total_lines"] == 120
    assert payload["symbol_count"] == 2
    names = {s["name"] for s in payload["symbols"]}
    assert "create_app" in names
    assert "lifespan" in names
    # outline carries a content rendering for the LLM
    assert "create_app" in payload["content"]
    # token_estimate is populated and roughly matches the rendered text
    assert payload["token_estimate"] >= 1
    assert payload["truncated"] is False
    # outline should suggest helpful next actions
    assert any("symbol=" in n for n in payload.get("next_actions", []))


# ---------------------------------------------------------------------------
# _build_read_payload — full mode with truncation
# ---------------------------------------------------------------------------
def test_build_payload_full_truncates_above_budget():
    big_content = ("print('x')\n" * 4000)  # ~10 chars * 4000 = 40KB
    backend = {
        "language": "python",
        "total_lines": 4000,
        "content": big_content,
        "start_line": 1,
        "end_line": 4000,
    }
    payload = _build_read_payload(
        file="big.py",
        requested_mode="full",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query=None,
        max_tokens=2000,
    )
    assert payload["mode"] == "full"
    assert payload["truncated"] is True
    assert "truncation_hint" in payload
    assert payload["token_estimate"] <= 2400  # within budget + safety
    # next_actions points at range/outline as escape hatches
    actions = payload.get("next_actions", [])
    assert any("range" in a for a in actions)


def test_build_payload_full_within_budget_does_not_truncate():
    backend = {
        "language": "python",
        "total_lines": 5,
        "content": "a = 1\n",
        "start_line": 1,
        "end_line": 5,
    }
    payload = _build_read_payload(
        file="tiny.py",
        requested_mode="full",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query=None,
        max_tokens=8000,
    )
    assert payload["truncated"] is False
    assert payload["content"] == "a = 1\n"


# ---------------------------------------------------------------------------
# _build_read_payload — relevant_chunks
# ---------------------------------------------------------------------------
def test_build_payload_relevant_chunks_carries_query_and_chunks():
    backend = {
        "language": "python",
        "total_lines": 200,
        "query": "auth middleware",
        "chunks": [
            {"symbol_name": "AuthMiddleware", "score": 0.92, "line_start": 3, "line_end": 28},
            {"symbol_name": "verify_token", "score": 0.81, "line_start": 30, "line_end": 60},
        ],
        "result_count": 2,
    }
    payload = _build_read_payload(
        file="api/middleware.py",
        requested_mode="relevant_chunks",
        data=backend,
        start_line=None,
        end_line=None,
        symbol=None,
        query="auth middleware",
        max_tokens=8000,
    )
    assert payload["mode"] == "relevant_chunks"
    assert payload["query"] == "auth middleware"
    assert payload["result_count"] == 2
    assert len(payload["chunks"]) == 2
    assert payload["truncated"] is False
    assert payload["token_estimate"] >= 1


# ---------------------------------------------------------------------------
# _format_outline_text
# ---------------------------------------------------------------------------
def test_format_outline_text_renders_nested_symbols():
    syms = [
        {"name": "Outer", "kind": "class", "lines": [1, 30]},
        {
            "name": "method_a",
            "kind": "method",
            "lines": [3, 10],
            "parent": "Outer",
            "signature": "def method_a(self):",
        },
    ]
    out = _format_outline_text("foo.py", "python", 30, syms, "outline")
    assert "Outer" in out
    assert "method_a" in out
    # nested symbols get a tree-style prefix
    assert "└─" in out
