"""Rust symbol / import / call extraction (STAGE 3.7)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_language() -> Optional[Any]:
    try:
        import tree_sitter_rust  # type: ignore

        return tree_sitter_rust.language()
    except ImportError:
        logger.debug("tree-sitter-rust not installed")
        return None


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


def _name_field(node: Any) -> Optional[Any]:
    """Return the 'name' field if available."""
    try:
        return node.child_by_field_name("name")
    except Exception:
        return None


def _line(node: Any) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def extract_symbols(tree: Any, source: str) -> List[Dict[str, Any]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    symbols: List[Dict[str, Any]] = []

    SYMBOL_TYPES = {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "type_item": "type",
        "const_item": "const",
        "static_item": "static",
    }

    def name_of(node: Any) -> str:
        name_node = _name_field(node)
        if name_node is not None:
            return _node_text(src_bytes, name_node)
        # Fallback search
        for child in node.children:
            if child.type in ("identifier", "type_identifier"):
                return _node_text(src_bytes, child)
        return "<anonymous>"

    def walk(node: Any, parent: Optional[str] = None) -> None:
        ntype = node.type
        if ntype in SYMBOL_TYPES:
            symbol_type = SYMBOL_TYPES[ntype]
            name = name_of(node) if ntype != "impl_item" else None
            if ntype == "impl_item":
                # impl block: try type_identifier under it
                t_node = _find_child(node, ("type_identifier", "generic_type", "scoped_type_identifier"))
                name = _node_text(src_bytes, t_node) if t_node else "<impl>"
            start, end = _line(node)
            symbols.append(
                {
                    "name": name or "<anonymous>",
                    "type": symbol_type,
                    "line_start": start,
                    "line_end": end,
                    "parent": parent,
                    "language": "rust",
                }
            )
            new_parent = name if ntype in ("impl_item", "trait_item", "mod_item", "struct_item") else parent
            for child in node.children:
                walk(child, new_parent)
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
        if node.type == "use_declaration":
            text = _node_text(src_bytes, node).rstrip(";").strip()
            module = text[len("use ") :].strip() if text.startswith("use ") else text
            imports.append(
                {
                    "module": module,
                    "raw": text,
                    "line": node.start_point[0] + 1,
                    "language": "rust",
                }
            )
            return
        if node.type == "extern_crate_declaration":
            text = _node_text(src_bytes, node).rstrip(";").strip()
            imports.append(
                {
                    "module": text,
                    "raw": text,
                    "line": node.start_point[0] + 1,
                    "language": "rust",
                }
            )
            return
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return imports


def extract_calls(tree: Any, source: str) -> List[Tuple[str, str, int]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    edges: List[Tuple[str, str, int]] = []

    def func_name(node: Any) -> str:
        n = _name_field(node)
        if n is not None:
            return _node_text(src_bytes, n)
        return "anonymous"

    def walk(node: Any, current: Optional[str]) -> None:
        new_current = current
        if node.type == "function_item":
            new_current = func_name(node)
        if node.type == "call_expression":
            fn_node = node.children[0] if node.children else None
            if fn_node is not None and current is not None:
                callee = _node_text(src_bytes, fn_node)
                edges.append((current, callee, node.start_point[0] + 1))
        for child in node.children:
            walk(child, new_current)

    walk(tree.root_node, None)
    return edges
