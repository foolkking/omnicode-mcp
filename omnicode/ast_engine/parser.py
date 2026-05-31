"""
Unified AST Parser (Tree-sitter)
================================
Single entry point for parsing 7+ languages and extracting symbols / imports /
function-call relations.  Each language module under ``languages/`` provides
its own extractor, and this parser dispatches to the right one based on the
language string.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import tree_sitter
from pydantic import BaseModel

from .languages import (
    extract_go_calls,
    extract_go_imports,
    extract_go_symbols,
    extract_java_calls,
    extract_java_imports,
    extract_java_symbols,
    extract_rust_calls,
    extract_rust_imports,
    extract_rust_symbols,
    get_go_language,
    get_java_language,
    get_rust_language,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models (light-weight DTOs)
# ---------------------------------------------------------------------------
class Symbol(BaseModel):
    name: str
    symbol_type: str
    start_line: int
    end_line: int
    parent: Optional[str] = None
    language: Optional[str] = None
    docstring: Optional[str] = None
    signature: Optional[str] = None


class Import(BaseModel):
    module: str
    line: int
    raw: Optional[str] = None
    language: Optional[str] = None


class CallEdge(BaseModel):
    caller: str
    callee: str
    line: int
    language: Optional[str] = None





# ---------------------------------------------------------------------------
# Language → extractor map
# ---------------------------------------------------------------------------
_LANG_ALIASES = {
    "py": "python",
    "python": "python",
    "js": "javascript",
    "jsx": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "typescript": "typescript",
    "cpp": "cpp",
    "cxx": "cpp",
    "cc": "cpp",
    "hpp": "cpp",
    "h": "cpp",
    "c++": "cpp",
    "java": "java",
    "go": "go",
    "rs": "rust",
    "rust": "rust",
}


def _normalize_lang(language: Optional[str]) -> str:
    if not language:
        return "python"
    return _LANG_ALIASES.get(language.strip().lower().lstrip("."), language.strip().lower().lstrip("."))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class UnifiedASTParser:
    """Tree-sitter based unified AST parser."""

    def __init__(self) -> None:
        self.parsers: Dict[str, tree_sitter.Parser] = {}
        self._initialize_parsers()

    # --------------------------------------------------------- bootstrap
    def _initialize_parsers(self) -> None:
        try:
            import tree_sitter_python  # type: ignore

            self.parsers["python"] = tree_sitter.Parser(
                tree_sitter.Language(tree_sitter_python.language())
            )
            logger.info("Loaded tree-sitter parser: python")
        except Exception as exc:
            logger.warning("Failed to load python parser: %s", exc)

        try:
            import tree_sitter_javascript  # type: ignore

            self.parsers["javascript"] = tree_sitter.Parser(
                tree_sitter.Language(tree_sitter_javascript.language())
            )
            logger.info("Loaded tree-sitter parser: javascript")
        except Exception as exc:
            logger.warning("Failed to load javascript parser: %s", exc)

        try:
            import tree_sitter_typescript  # type: ignore

            self.parsers["typescript"] = tree_sitter.Parser(
                tree_sitter.Language(tree_sitter_typescript.language_typescript())
            )
            logger.info("Loaded tree-sitter parser: typescript")
        except Exception as exc:
            logger.warning("Failed to load typescript parser: %s", exc)

        try:
            import tree_sitter_cpp  # type: ignore

            self.parsers["cpp"] = tree_sitter.Parser(
                tree_sitter.Language(tree_sitter_cpp.language())
            )
            logger.info("Loaded tree-sitter parser: cpp")
        except Exception as exc:
            logger.warning("Failed to load cpp parser: %s", exc)

        # Java / Go / Rust come from sub-modules (optional)
        for name, getter in (
            ("java", get_java_language),
            ("go", get_go_language),
            ("rust", get_rust_language),
        ):
            try:
                lang_obj = getter()
                if lang_obj is None:
                    continue
                self.parsers[name] = tree_sitter.Parser(tree_sitter.Language(lang_obj))
                logger.info("Loaded tree-sitter parser: %s", name)
            except Exception as exc:
                logger.warning("Failed to load %s parser: %s", name, exc)

    # --------------------------------------------------------- introspection
    def supported_languages(self) -> List[str]:
        return sorted(self.parsers.keys())

    def get_parser(self, language: str) -> Optional[tree_sitter.Parser]:
        return self.parsers.get(_normalize_lang(language))

    # --------------------------------------------------------- parse
    def parse(self, code: str, language: str) -> Optional[tree_sitter.Tree]:
        parser = self.get_parser(language)
        if parser is None:
            logger.debug("No parser for language: %s", language)
            return None
        code_bytes = code.encode("utf-8") if isinstance(code, str) else code
        try:
            return parser.parse(code_bytes)
        except Exception as exc:
            logger.warning("Tree-sitter parse failed for %s: %s", language, exc)
            return None

    # --------------------------------------------------------- symbols
    def extract_symbols(self, code: str, language: str) -> List[Dict[str, Any]]:
        lang = _normalize_lang(language)
        tree = self.parse(code, lang)
        if tree is None:
            return []
        if lang == "java":
            return extract_java_symbols(tree, code)
        if lang == "go":
            return extract_go_symbols(tree, code)
        if lang == "rust":
            return extract_rust_symbols(tree, code)
        return _generic_extract_symbols(tree, code, lang)

    # --------------------------------------------------------- imports
    def extract_imports(self, code: str, language: str) -> List[Dict[str, Any]]:
        lang = _normalize_lang(language)
        tree = self.parse(code, lang)
        if tree is None:
            return []
        if lang == "java":
            return extract_java_imports(tree, code)
        if lang == "go":
            return extract_go_imports(tree, code)
        if lang == "rust":
            return extract_rust_imports(tree, code)
        return _generic_extract_imports(tree, code, lang)

    # --------------------------------------------------------- calls
    def extract_calls(self, code: str, language: str) -> List[Tuple[str, str, int]]:
        lang = _normalize_lang(language)
        tree = self.parse(code, lang)
        if tree is None:
            return []
        if lang == "java":
            return extract_java_calls(tree, code)
        if lang == "go":
            return extract_go_calls(tree, code)
        if lang == "rust":
            return extract_rust_calls(tree, code)
        return _generic_extract_calls(tree, code, lang)


# ---------------------------------------------------------------------------
# Generic extractors for python / js / ts / cpp
# ---------------------------------------------------------------------------
_GENERIC_FUNC_TYPES = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "arrow_function",
    # NOTE: do NOT include the bare type "function" here — in tree-sitter-javascript
    # that's the leaf keyword token inside a ``function_expression`` and matching
    # it as a function would create phantom <anonymous> entries when we recurse
    # into function bodies.
    "function_expression",
    "generator_function_declaration",
}
_GENERIC_CLASS_TYPES = {
    "class_definition",
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "class_specifier",      # C++
    "struct_specifier",     # C++
    "namespace_definition", # C++ namespace
    "type_alias_declaration",  # TS / Java
}


def _node_text(source_bytes: bytes, node: Any) -> str:
    try:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _find_first_identifier(node: Any) -> Optional[Any]:
    """Locate the symbol's name node.

    For most languages the name sits as a direct child of the
    function/class definition, but C++ wraps function names in
    ``function_declarator`` (and sometimes inside ``pointer_declarator`` /
    ``reference_declarator``).  We look one level deeper for those cases.
    """
    # Pass 1 — direct children
    for child in node.children:
        if child.type in (
            "identifier",
            "type_identifier",
            "property_identifier",
            "field_identifier",
        ):
            return child
    # Pass 2 — descend into common C++ declarator wrappers
    for child in node.children:
        if child.type in (
            "function_declarator",
            "pointer_declarator",
            "reference_declarator",
            "init_declarator",
        ):
            inner = _find_first_identifier(child)
            if inner is not None:
                return inner
    return None


def _js_anon_assignment_name(node: Any, src_bytes: bytes) -> Optional[Tuple[str, str]]:
    """For JS/TS, derive a name for anonymous arrow/function expressions.

    Patterns recognised:

    * ``const foo = () => {}``      → ("foo", "function")
    * ``var foo = function () {}``  → ("foo", "function")
    * ``methodName: () => {}``      → ("methodName", "method")  (object literal)
    * ``methodName: function() {}`` → ("methodName", "method")
    * ``foo = () => {}``            → ("foo", "function")        (assignment)

    Returns ``(name, kind)`` or ``None`` if the parent shape isn't a known
    "anonymous-function-with-an-implicit-name" pattern.
    """
    parent = getattr(node, "parent", None)
    if parent is None:
        return None
    ptype = parent.type

    # const/let/var foo = () => {}
    if ptype == "variable_declarator":
        for child in parent.children:
            if child.type in ("identifier", "property_identifier"):
                return _node_text(src_bytes, child), "function"

    # foo = () => {}     (assignment_expression)
    if ptype == "assignment_expression":
        for child in parent.children:
            if child.type in (
                "identifier",
                "property_identifier",
                "member_expression",
            ):
                txt = _node_text(src_bytes, child)
                # member expressions like a.b.c → take last segment
                clean = txt.split(".")[-1].strip()
                if clean:
                    return clean, "function"
            if child.type == "=":
                break

    # methodName: () => {} | methodName: function () {}
    # in tree-sitter-javascript object literals these wrap as `pair`.
    if ptype in ("pair", "property_signature"):
        for child in parent.children:
            if child.type in ("property_identifier", "string", "identifier"):
                raw = _node_text(src_bytes, child)
                clean = raw.strip().strip("\"'")
                if clean:
                    return clean, "method"
            if child.type == ":":
                break

    return None


def _generic_extract_symbols(tree: Any, source: str, language: str) -> List[Dict[str, Any]]:
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    symbols: List[Dict[str, Any]] = []

    is_js_like = language in ("javascript", "typescript")

    def walk(node: Any, parent: Optional[str] = None) -> None:
        ntype = node.type
        if ntype in _GENERIC_CLASS_TYPES:
            name_node = _find_first_identifier(node)
            name = _node_text(src_bytes, name_node) if name_node else "<anonymous>"
            symbols.append(
                {
                    "name": name,
                    "type": "class" if "class" in ntype else ntype.replace("_declaration", "").replace("_definition", ""),
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "parent": parent,
                    "language": language,
                }
            )
            for child in node.children:
                walk(child, name)
            return
        if ntype in _GENERIC_FUNC_TYPES:
            name_node = _find_first_identifier(node)
            name = _node_text(src_bytes, name_node) if name_node else ""

            inferred_kind: Optional[str] = None
            # JS/TS arrow / function expression with no direct identifier
            # — try to derive a name from the parent declarator/pair.
            if is_js_like and ntype in ("arrow_function", "function_expression"):
                # For arrow / function expressions tree-sitter happily
                # returns the formal parameter as the first identifier
                # child (``s => f(s)`` would then be named "s"), so we
                # IGNORE the direct-identifier scan here and rely solely
                # on the parent-declarator heuristic.  Anonymous lambdas
                # without an inferable name are intentionally dropped
                # from the symbol index — they pollute symbol-search
                # results without giving the AI anything to grep on.
                guess = _js_anon_assignment_name(node, src_bytes)
                if guess is not None:
                    name, inferred_kind = guess
                else:
                    # Anonymous callback — don't emit, but DO recurse so
                    # any named helper defined inside still surfaces.
                    for child in node.children:
                        walk(child, parent)
                    return
            elif (not name or name == "") and is_js_like:
                guess = _js_anon_assignment_name(node, src_bytes)
                if guess is not None:
                    name, inferred_kind = guess

            if not name:
                name = "<anonymous>"

            # Filter junk single-character names that always mean
            # "tree-sitter caught a formal parameter, not a function".
            if len(name) <= 1 and name != "_":
                for child in node.children:
                    walk(child, parent)
                return

            kind = inferred_kind or ("method" if parent else "function")
            symbols.append(
                {
                    "name": name,
                    "type": kind,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "parent": parent,
                    "language": language,
                }
            )
            # Recurse into the function body so nested helpers
            # (closures, IIFEs, decorators, MCP-tool inner functions)
            # still surface in the outline.  We treat the current
            # function as the parent of any nested symbols.
            for child in node.children:
                walk(child, name)
            return
        for child in node.children:
            walk(child, parent)

    walk(tree.root_node)
    return symbols


def _generic_extract_imports(tree: Any, source: str, language: str) -> List[Dict[str, Any]]:
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    imports: List[Dict[str, Any]] = []
    IMPORT_TYPES = {
        "import_statement",
        "import_from_statement",
        "import_declaration",
        "preproc_include",  # C/C++ #include
    }
    # Stop recursing once we hit a function / class body — local imports
    # inside a function (``def foo(): import asyncio``) are noise for the
    # caller asking "what does this module depend on".
    BODY_TYPES = _GENERIC_FUNC_TYPES | _GENERIC_CLASS_TYPES

    def walk(node: Any) -> None:
        if node.type in IMPORT_TYPES:
            text = _node_text(src_bytes, node).strip()
            imports.append(
                {
                    "module": text,
                    "raw": text,
                    "line": node.start_point[0] + 1,
                    "language": language,
                }
            )
            return
        if node.type in BODY_TYPES:
            return
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return imports


def _generic_extract_calls(tree: Any, source: str, language: str) -> List[Tuple[str, str, int]]:
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    edges: List[Tuple[str, str, int]] = []
    CALL_TYPES = {"call", "call_expression"}

    def func_name(node: Any) -> str:
        name_node = _find_first_identifier(node)
        return _node_text(src_bytes, name_node) if name_node else "anonymous"

    def walk(node: Any, current: Optional[str]) -> None:
        new_current = current
        if node.type in _GENERIC_FUNC_TYPES:
            new_current = func_name(node)
        if node.type in CALL_TYPES and new_current is not None:
            fn_node = node.children[0] if node.children else None
            if fn_node is not None:
                callee = _node_text(src_bytes, fn_node)
                # Cleanup a.b.c() -> c
                clean = callee.split(".")[-1].strip()
                if clean:
                    edges.append((new_current, clean, node.start_point[0] + 1))
        for child in node.children:
            walk(child, new_current)

    walk(tree.root_node, None)
    return edges


__all__ = [
    "UnifiedASTParser",
    "Symbol",
    "Import",
    "CallEdge",
]
