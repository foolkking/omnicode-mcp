"""Tests for the omni_read fix: nested function recursion and JS-style
arrow / object-method name inference.

These complement test_ast_parser.py without needing the FastAPI app
(so they don't trip on a running MCP server holding the .data DBs).
"""
from __future__ import annotations

import pytest

from omnicode.ast_engine.parser import UnifiedASTParser


@pytest.fixture(scope="module")
def parser() -> UnifiedASTParser:
    return UnifiedASTParser()


# ---------------------------------------------------------------------------
# Nested-function recursion (so that closures / inner registrations show up
# in the outline)
# ---------------------------------------------------------------------------
def test_python_nested_function_appears_in_symbol_list(parser):
    code = (
        "def outer():\n"
        "    def inner_one():\n"
        "        return 1\n"
        "    def inner_two():\n"
        "        return 2\n"
        "    return inner_one, inner_two\n"
    )
    syms = parser.extract_symbols(code, "python")
    names = {s["name"] for s in syms}
    assert "outer" in names
    assert "inner_one" in names, names
    assert "inner_two" in names, names

    # inner functions must record their parent so the outline can render
    # the hierarchy.
    inner_one = next(s for s in syms if s["name"] == "inner_one")
    assert inner_one["parent"] == "outer"


def test_python_decorator_inner_function_appears(parser):
    """Mirrors how register_high_level_tools() registers MCP tools as
    inner functions of a decorator-style wrapper."""
    code = (
        "def register_tools(app):\n"
        "    @app.tool()\n"
        "    def omni_read(file: str):\n"
        "        return file\n"
        "    @app.tool()\n"
        "    def omni_search(q: str):\n"
        "        return q\n"
    )
    syms = parser.extract_symbols(code, "python")
    names = {s["name"] for s in syms}
    assert "omni_read" in names, names
    assert "omni_search" in names, names


# ---------------------------------------------------------------------------
# JS arrow / object-method name inference
# ---------------------------------------------------------------------------
def test_js_arrow_const_assignment_gets_variable_name(parser):
    code = (
        "const greet = (name) => `hi ${name}`;\n"
        "let times2 = function (n) { return n * 2; };\n"
    )
    syms = parser.extract_symbols(code, "javascript")
    names = {s["name"] for s in syms}
    assert "greet" in names, names
    assert "times2" in names, names
    # Should NOT contain a useless <anonymous>.
    assert "<anonymous>" not in names, names


def test_js_object_literal_method_gets_property_name(parser):
    code = (
        "const handlers = {\n"
        "  onClick: () => console.log('click'),\n"
        "  onSubmit: function (e) { e.preventDefault(); }\n"
        "};\n"
    )
    syms = parser.extract_symbols(code, "javascript")
    names = {s["name"] for s in syms}
    assert "onClick" in names, names
    assert "onSubmit" in names, names


def test_typescript_arrow_const_keeps_name(parser):
    code = (
        "export const fetcher = async (url: string): Promise<string> => {\n"
        "  return url;\n"
        "};\n"
    )
    syms = parser.extract_symbols(code, "typescript")
    names = {s["name"] for s in syms}
    assert "fetcher" in names, names
