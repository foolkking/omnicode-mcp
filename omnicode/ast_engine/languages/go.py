"""Go symbol / import / call extraction (STAGE 3.7)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_language() -> Optional[Any]:
    try:
        import tree_sitter_go  # type: ignore

        return tree_sitter_go.language()
    except ImportError:
        logger.debug("tree-sitter-go not installed")
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


def _line(node: Any) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def extract_symbols(tree: Any, source: str) -> List[Dict[str, Any]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    symbols: List[Dict[str, Any]] = []

    def name_of(node: Any) -> str:
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "field_identifier"):
                return _node_text(src_bytes, child)
        return "<anonymous>"

    def walk(node: Any, parent: Optional[str] = None) -> None:
        ntype = node.type
        if ntype == "function_declaration":
            start, end = _line(node)
            symbols.append(
                {
                    "name": name_of(node),
                    "type": "function",
                    "line_start": start,
                    "line_end": end,
                    "parent": parent,
                    "language": "go",
                }
            )
            return
        if ntype == "method_declaration":
            # Go: func (r *Receiver) Name(args) ...
            method_name = "<anonymous>"
            for child in node.children:
                if child.type == "field_identifier":
                    method_name = _node_text(src_bytes, child)
                    break
            start, end = _line(node)
            symbols.append(
                {
                    "name": method_name,
                    "type": "method",
                    "line_start": start,
                    "line_end": end,
                    "parent": parent,
                    "language": "go",
                }
            )
            return
        if ntype == "type_declaration":
            # Walk children to find type_spec entries
            for spec in node.children:
                if spec.type == "type_spec":
                    type_name = "<anonymous>"
                    type_kind = "type"
                    for child in spec.children:
                        if child.type == "type_identifier":
                            type_name = _node_text(src_bytes, child)
                        elif child.type == "struct_type":
                            type_kind = "struct"
                        elif child.type == "interface_type":
                            type_kind = "interface"
                    start, end = _line(spec)
                    symbols.append(
                        {
                            "name": type_name,
                            "type": type_kind,
                            "line_start": start,
                            "line_end": end,
                            "parent": parent,
                            "language": "go",
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
            for child in node.children:
                if child.type == "import_spec":
                    text = _node_text(src_bytes, child).strip()
                    imports.append(
                        {
                            "module": text.strip('"'),
                            "raw": text,
                            "line": child.start_point[0] + 1,
                            "language": "go",
                        }
                    )
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            text = _node_text(src_bytes, spec).strip()
                            imports.append(
                                {
                                    "module": text.strip('"'),
                                    "raw": text,
                                    "line": spec.start_point[0] + 1,
                                    "language": "go",
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

    def name_of_func_decl(node: Any) -> str:
        for child in node.children:
            if child.type in ("identifier", "field_identifier"):
                return _node_text(src_bytes, child)
        return "anonymous"

    def walk(node: Any, current: Optional[str]) -> None:
        new_current = current
        if node.type in ("function_declaration", "method_declaration"):
            new_current = name_of_func_decl(node)
        if node.type == "call_expression":
            fn_node = node.children[0] if node.children else None
            if fn_node is not None and current is not None:
                callee = _node_text(src_bytes, fn_node)
                edges.append((current, callee, node.start_point[0] + 1))
        for child in node.children:
            walk(child, new_current)

    walk(tree.root_node, None)
    return edges
