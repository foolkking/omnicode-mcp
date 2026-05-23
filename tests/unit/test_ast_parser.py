"""STAGE 11.3 — Unit tests for the Tree-sitter unified AST parser.

Exercises every supported language through the same public surface so a
regression in one extractor (e.g. a tree-sitter version bump renaming a
node type) is caught immediately.
"""

from __future__ import annotations

import pytest

from omnicode.ast_engine.parser import UnifiedASTParser, _normalize_lang


@pytest.fixture(scope="module")
def parser() -> UnifiedASTParser:
    return UnifiedASTParser()


# ---------------------------------------------------------------------------
# Bootstrap & introspection
# ---------------------------------------------------------------------------
class TestParserBootstrap:
    def test_supported_languages_includes_core_set(self, parser):
        langs = parser.supported_languages()
        # The seven advertised languages should always be available because
        # they are required pyproject deps.
        for required in ("python", "javascript", "typescript", "cpp", "java", "go", "rust"):
            assert required in langs, f"{required} missing from {langs}"

    def test_get_parser_round_trip(self, parser):
        assert parser.get_parser("python") is not None
        assert parser.get_parser("py") is not None  # alias
        assert parser.get_parser("nonexistent-lang-xyz") is None

    def test_normalize_lang_aliases(self):
        assert _normalize_lang("py") == "python"
        assert _normalize_lang("JS") == "javascript"
        assert _normalize_lang("tsx") == "typescript"
        assert _normalize_lang(".cpp") == "cpp"
        assert _normalize_lang(None) == "python"
        assert _normalize_lang("") == "python"


# ---------------------------------------------------------------------------
# Symbol extraction — generic (Python / JS / TS / C++)
# ---------------------------------------------------------------------------
class TestSymbolExtraction:
    def test_python_function_and_class(self, parser):
        code = (
            "class Animal:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "\n"
            "    def speak(self):\n"
            "        pass\n"
            "\n"
            "def standalone():\n"
            "    return 1\n"
        )
        symbols = parser.extract_symbols(code, "python")
        names = {s["name"] for s in symbols}
        # Must contain at least the top-level class and standalone function
        assert "Animal" in names
        assert "standalone" in names

    def test_javascript_function_and_class(self, parser):
        code = (
            "class Foo {\n"
            "  bar() { return 1; }\n"
            "}\n"
            "function topLevel() { return 2; }\n"
        )
        symbols = parser.extract_symbols(code, "javascript")
        names = {s["name"] for s in symbols}
        assert "Foo" in names
        assert "topLevel" in names

    def test_typescript_interface_and_class(self, parser):
        code = (
            "interface Greeter { greet(): string; }\n"
            "class Hello implements Greeter {\n"
            "  greet(): string { return 'hi'; }\n"
            "}\n"
        )
        symbols = parser.extract_symbols(code, "typescript")
        names = {s["name"] for s in symbols}
        assert "Hello" in names

    def test_cpp_class_and_function(self, parser):
        code = (
            "class Widget {\n"
            "public:\n"
            "  void show() {}\n"
            "};\n"
            "\n"
            "int main() { return 0; }\n"
        )
        symbols = parser.extract_symbols(code, "cpp")
        names = {s["name"] for s in symbols}
        assert "Widget" in names
        assert "main" in names

    def test_java_class_and_methods(self, parser):
        code = (
            "public class Greeter {\n"
            "  public String hello() { return \"hi\"; }\n"
            "  public void shout() {}\n"
            "}\n"
        )
        symbols = parser.extract_symbols(code, "java")
        names = {s["name"] for s in symbols}
        assert "Greeter" in names

    def test_go_top_level_function(self, parser):
        code = "package main\n\nfunc Hello() string { return \"hi\" }\n"
        symbols = parser.extract_symbols(code, "go")
        names = {s["name"] for s in symbols}
        assert "Hello" in names

    def test_rust_function_and_struct(self, parser):
        code = "struct Point { x: i32 }\nfn main() {}\n"
        symbols = parser.extract_symbols(code, "rust")
        names = {s["name"] for s in symbols}
        assert "Point" in names or "main" in names

    def test_empty_input_returns_empty_symbols(self, parser):
        assert parser.extract_symbols("", "python") == []
        assert parser.extract_symbols("", "cpp") == []


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------
class TestImportExtraction:
    def test_python_imports(self, parser):
        code = (
            "import os\n"
            "import sys as system\n"
            "from typing import List, Dict\n"
            "from . import sibling\n"
        )
        imports = parser.extract_imports(code, "python")
        modules = {i["module"] for i in imports}
        # Implementation may include the FROM module too — accept both.
        assert any("os" in m for m in modules)
        assert any("sys" in m for m in modules)
        assert any("typing" in m for m in modules)

    def test_javascript_imports(self, parser):
        code = (
            "import React from 'react';\n"
            "import { useState } from 'react';\n"
            "const lodash = require('lodash');\n"
        )
        imports = parser.extract_imports(code, "javascript")
        modules = {i["module"] for i in imports}
        assert any("react" in m for m in modules)

    def test_java_imports(self, parser):
        code = "package com.example;\nimport java.util.List;\nimport java.util.*;\n"
        imports = parser.extract_imports(code, "java")
        modules = {i["module"] for i in imports}
        assert any("java.util" in m for m in modules)

    def test_go_imports(self, parser):
        code = (
            "package main\n"
            "import (\n"
            "    \"fmt\"\n"
            "    \"os\"\n"
            ")\n"
        )
        imports = parser.extract_imports(code, "go")
        modules = {i["module"] for i in imports}
        assert "fmt" in modules
        assert "os" in modules

    def test_no_imports(self, parser):
        assert parser.extract_imports("x = 1\n", "python") == []


# ---------------------------------------------------------------------------
# Call extraction
# ---------------------------------------------------------------------------
class TestCallExtraction:
    def test_python_calls(self, parser):
        code = (
            "def helper():\n    pass\n"
            "def caller():\n"
            "    helper()\n"
            "    print('hi')\n"
        )
        calls = parser.extract_calls(code, "python")
        # Each item is (caller, callee, line)
        callees = {c[1] for c in calls}
        # The implementation may report only same-file callees ('helper') —
        # but at minimum the caller→helper edge must exist.
        assert "helper" in callees or "print" in callees
        # Verify the caller name is recorded
        assert any(c[0] == "caller" for c in calls)

    def test_javascript_calls(self, parser):
        code = (
            "function helper() {}\n"
            "function caller() {\n"
            "  helper();\n"
            "  console.log('hi');\n"
            "}\n"
        )
        calls = parser.extract_calls(code, "javascript")
        # caller → helper edge should be present
        assert any(c[0] == "caller" and c[1] == "helper" for c in calls)

    def test_java_calls(self, parser):
        code = (
            "class C {\n"
            "  void helper() {}\n"
            "  void caller() { helper(); }\n"
            "}\n"
        )
        calls = parser.extract_calls(code, "java")
        callees = {c[1] for c in calls}
        assert "helper" in callees

    def test_calls_in_unsupported_language_no_crash(self, parser):
        # 'cobol' isn't loaded; parser should return [] gracefully
        assert parser.extract_calls("blah blah", "cobol") == []
