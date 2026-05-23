"""Java symbol / import / call extraction (STAGE 3.7)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_language() -> Optional[Any]:
    """Return the tree-sitter Java language object, or None if not installed."""
    try:
        import tree_sitter_java  # type: ignore

        return tree_sitter_java.language()
    except ImportError:
        logger.debug("tree-sitter-java not installed")
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _node_text(source_bytes: bytes, node: Any) -> str:
    try:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _find_child(node: Any, types: Tuple[str, ...]) -> Optional[Any]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _find_name(node: Any) -> Optional[Any]:
    """Return the first identifier child of ``node`` (commonly the symbol name)."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier"):
            return child
    return None


def _line(node: Any) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_symbols(tree: Any, source: str) -> List[Dict[str, Any]]:
    """Walk the AST and return a list of symbol dicts."""
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    symbols: List[Dict[str, Any]] = []

    def walk(node: Any, parent: Optional[str] = None) -> None:
        ntype = node.type
        if ntype in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
            name_node = _find_name(node)
            name = _node_text(src_bytes, name_node) if name_node else "<anonymous>"
            start, end = _line(node)
            symbols.append(
                {
                    "name": name,
                    "type": ntype.replace("_declaration", ""),
                    "line_start": start,
                    "line_end": end,
                    "parent": parent,
                    "language": "java",
                }
            )
            new_parent = name
            for child in node.children:
                walk(child, new_parent)
            return
        if ntype in ("method_declaration", "constructor_declaration"):
            name_node = _find_name(node)
            name = _node_text(src_bytes, name_node) if name_node else "<anonymous>"
            start, end = _line(node)
            params_node = _find_child(node, ("formal_parameters",))
            sig = f"{name}{_node_text(src_bytes, params_node)}" if params_node else name
            symbols.append(
                {
                    "name": name,
                    "type": "method" if ntype == "method_declaration" else "constructor",
                    "line_start": start,
                    "line_end": end,
                    "parent": parent,
                    "signature": sig,
                    "language": "java",
                }
            )
            return
        for child in node.children:
            walk(child, parent)

    walk(tree.root_node)
    return symbols


def extract_imports(tree: Any, source: str) -> List[Dict[str, Any]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    imports: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if node.type == "import_declaration":
            text = _node_text(src_bytes, node).rstrip(";").strip()
            # Trim 'import' prefix
            if text.startswith("import"):
                module = text[len("import") :].strip()
            else:
                module = text
            imports.append(
                {
                    "module": module,
                    "raw": text,
                    "line": node.start_point[0] + 1,
                    "language": "java",
                }
            )
            return
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return imports


def extract_calls(tree: Any, source: str) -> List[Tuple[str, str, int]]:
    """Return list of (caller, callee, line)."""
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    edges: List[Tuple[str, str, int]] = []

    def walk(node: Any, current_method: Optional[str] = None) -> None:
        ntype = node.type
        new_method = current_method
        if ntype in ("method_declaration", "constructor_declaration"):
            name_node = _find_name(node)
            new_method = _node_text(src_bytes, name_node) if name_node else current_method
        if ntype == "method_invocation":
            # Java method invocation: object.name(args)  OR  name(args)
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child  # last 'identifier' is usually the method name
            if name_node is not None and current_method is not None:
                callee = _node_text(src_bytes, name_node)
                edges.append((current_method, callee, node.start_point[0] + 1))
        for child in node.children:
            walk(child, new_method)

    walk(tree.root_node)
    return edges
