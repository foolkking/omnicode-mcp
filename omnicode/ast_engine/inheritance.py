"""
Class inheritance hierarchy (STAGE 3.11)
========================================
Builds a directional inheritance graph (subclass → base class) for one or
many files and supports the canonical AST queries:

* ``base_classes_of(symbol)``  → who does ``symbol`` inherit from?
* ``subclasses_of(symbol)``   → who extends / implements ``symbol``?
* ``descendants_of(symbol)``  → all transitive children
* ``ancestors_of(symbol)``    → all transitive parents

Cross-language design
---------------------
We re-use the Tree-sitter parser from :mod:`omnicode.ast_engine.parser` and
inspect the well-known node types each language uses to spell "inheritance":

    Python:    ``argument_list`` directly under ``class_definition``
               (each argument either an identifier or a ``keyword_argument``).
    C++:       ``base_class_clause`` containing one or more
               ``type_identifier`` nodes.
    Java:      ``superclass`` (extends X) and ``super_interfaces``
               (implements I, J, K) sub-nodes of ``class_declaration`` /
               ``interface_declaration``.
    JS / TS:   ``class_heritage`` containing ``extends_clause`` and
               ``implements_clause``.
    Go:        struct embedding via ``field_declaration`` whose
               ``field_identifier`` matches the embedded type — we capture
               anonymous fields whose name resolves to a type.
    Rust:      ``impl_item`` (``impl Trait for Struct``) is treated as
               struct → trait inheritance.

The graph is language-agnostic and incrementally updatable — same design
contract as :class:`omnicode.ast_engine.graph.CallGraph`.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel

from .parser import UnifiedASTParser, _normalize_lang  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
class InheritanceEdge(BaseModel):
    """A single subclass → base class edge."""

    subclass: str
    base: str
    kind: str = "extends"  # 'extends' | 'implements' | 'embeds' | 'impls'
    line: int = 0
    file_path: Optional[str] = None
    language: Optional[str] = None


@dataclass
class InheritanceGraph:
    """In-memory directional inheritance graph with incremental update support."""

    edges: List[InheritanceEdge] = field(default_factory=list)
    # subclass -> set(bases)
    bases_index: Dict[str, Set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # base -> set(subclasses)
    subs_index: Dict[str, Set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    _by_file: Dict[str, Set[int]] = field(
        default_factory=lambda: defaultdict(set)
    )
    _lock: "threading.RLock" = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    # ------------------------------------------------------------------
    def add_edge(self, edge: InheritanceEdge) -> None:
        with self._lock:
            idx = len(self.edges)
            self.edges.append(edge)
            self.bases_index[edge.subclass].add(edge.base)
            self.subs_index[edge.base].add(edge.subclass)
            if edge.file_path:
                self._by_file[os.path.abspath(edge.file_path)].add(idx)

    # ------------------------------------------------------------------ direct queries
    def base_classes_of(self, symbol: str) -> List[str]:
        return sorted(self.bases_index.get(symbol, set()))

    def subclasses_of(self, symbol: str) -> List[str]:
        return sorted(self.subs_index.get(symbol, set()))

    # ------------------------------------------------------------------ transitive queries
    def ancestors_of(self, symbol: str, max_depth: int = 16) -> List[str]:
        return self._bfs(symbol, self.bases_index, max_depth)

    def descendants_of(self, symbol: str, max_depth: int = 16) -> List[str]:
        return self._bfs(symbol, self.subs_index, max_depth)

    @staticmethod
    def _bfs(
        start: str, adj: Dict[str, Set[str]], max_depth: int
    ) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        queue: "deque[Tuple[str, int]]" = deque()
        for n in adj.get(start, set()):
            queue.append((n, 1))
        while queue:
            node, depth = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            out.append(node)
            if depth < max_depth:
                for nxt in adj.get(node, set()):
                    if nxt not in seen:
                        queue.append((nxt, depth + 1))
        return sorted(out)

    # ------------------------------------------------------------------ stats
    def stats(self) -> Dict[str, int]:
        return {
            "total_edges": len(self.edges),
            "total_subclasses": len(self.bases_index),
            "total_base_classes": len(self.subs_index),
            "files_indexed": len(self._by_file),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.stats(),
            "edges": [e.dict() for e in self.edges],
        }

    # ------------------------------------------------------------------ incremental
    def remove_file(self, file_path: str) -> int:
        """Drop every edge that came from ``file_path`` and compact indices."""
        with self._lock:
            abs_path = os.path.abspath(file_path)
            edge_indices = self._by_file.pop(abs_path, set())
            if not edge_indices:
                return 0
            keep_mask = [True] * len(self.edges)
            for i in edge_indices:
                if 0 <= i < len(keep_mask):
                    keep_mask[i] = False
            kept_edges: List[InheritanceEdge] = []
            old_to_new: Dict[int, int] = {}
            for old_idx, edge in enumerate(self.edges):
                if keep_mask[old_idx]:
                    old_to_new[old_idx] = len(kept_edges)
                    kept_edges.append(edge)
            self.edges = kept_edges
            self.bases_index = defaultdict(set)
            self.subs_index = defaultdict(set)
            for e in self.edges:
                self.bases_index[e.subclass].add(e.base)
                self.subs_index[e.base].add(e.subclass)
            new_by_file: Dict[str, Set[int]] = defaultdict(set)
            for f, idxs in self._by_file.items():
                translated = {old_to_new[i] for i in idxs if i in old_to_new}
                if translated:
                    new_by_file[f] = translated
            self._by_file = new_by_file
            return len(edge_indices)

    # ------------------------------------------------------------------ ascii render
    def render_ascii(self, max_nodes: int = 50) -> str:
        if not self.edges:
            return "(empty inheritance graph)"
        lines = ["🧬 Inheritance graph (subclass → base):"]
        subs = sorted(self.bases_index.keys())[:max_nodes]
        for s in subs:
            bases = sorted(self.bases_index[s])
            arrow = ", ".join(bases[:6]) + (
                f"  …(+{len(bases) - 6})" if len(bases) > 6 else ""
            )
            lines.append(f"  ├── {s} → {arrow}")
        if len(self.bases_index) > max_nodes:
            lines.append(
                f"  └── … and {len(self.bases_index) - max_nodes} more subclasses"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-language extractors
# ---------------------------------------------------------------------------
def _node_text(src_bytes: bytes, node: Any) -> str:
    try:
        return src_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _find_class_name(class_node: Any) -> Optional[Any]:
    """Return the identifier child that holds the class name."""
    for child in class_node.children:
        if child.type in (
            "identifier",
            "type_identifier",
            "property_identifier",
        ):
            return child
    # Some grammars wrap the name in an intermediate node
    for child in class_node.children:
        if hasattr(child, "child_by_field_name"):
            n = child.child_by_field_name("name")
            if n is not None:
                return n
    return None


def _walk(node: Any):
    """Yield every descendant node (depth-first)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for ch in reversed(getattr(n, "children", [])):
            stack.append(ch)


# ---------- Python ----------
def _python_inheritance(tree: Any, source: str) -> List[Tuple[str, str, str, int]]:
    """Yield (subclass, base, kind, line) tuples for Python code."""
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    out: List[Tuple[str, str, str, int]] = []
    for node in _walk(tree.root_node):
        if node.type != "class_definition":
            continue
        name_node = _find_class_name(node)
        if name_node is None:
            continue
        cls_name = _node_text(src_bytes, name_node)
        # Look for argument_list or superclasses node
        for child in node.children:
            if child.type in ("argument_list", "superclasses"):
                for arg in child.children:
                    if arg.type in ("identifier", "attribute"):
                        base = _node_text(src_bytes, arg)
                        if base:
                            out.append((cls_name, base, "extends", node.start_point[0] + 1))
                    elif arg.type == "keyword_argument":
                        # metaclass=X — treat metaclass as a soft extends
                        try:
                            kw = arg.child_by_field_name("name")
                            val = arg.child_by_field_name("value")
                        except Exception:
                            kw = val = None
                        if kw and val and _node_text(src_bytes, kw) == "metaclass":
                            base = _node_text(src_bytes, val)
                            if base:
                                out.append((cls_name, base, "metaclass", node.start_point[0] + 1))
                break
    return out


# ---------- C++ ----------
def _cpp_inheritance(tree: Any, source: str) -> List[Tuple[str, str, str, int]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    out: List[Tuple[str, str, str, int]] = []
    for node in _walk(tree.root_node):
        if node.type not in ("class_specifier", "struct_specifier"):
            continue
        name_node = _find_class_name(node)
        if name_node is None:
            continue
        cls_name = _node_text(src_bytes, name_node)
        for child in node.children:
            if child.type == "base_class_clause":
                # Iterate base specifiers
                for sub in _walk(child):
                    if sub.type in ("type_identifier", "qualified_identifier"):
                        base = _node_text(src_bytes, sub)
                        if base and base != cls_name:
                            out.append((cls_name, base, "extends", node.start_point[0] + 1))
    return out


# ---------- Java ----------
def _java_inheritance(tree: Any, source: str) -> List[Tuple[str, str, str, int]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    out: List[Tuple[str, str, str, int]] = []
    for node in _walk(tree.root_node):
        if node.type not in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        ):
            continue
        name_node = _find_class_name(node)
        if name_node is None:
            continue
        cls_name = _node_text(src_bytes, name_node)
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type == "superclass":
                for sub in _walk(child):
                    if sub.type in ("type_identifier", "scoped_type_identifier"):
                        base = _node_text(src_bytes, sub)
                        if base:
                            out.append((cls_name, base, "extends", line))
                            break  # take only the first (the actual superclass)
            elif child.type in ("super_interfaces", "extends_interfaces"):
                for sub in _walk(child):
                    if sub.type in ("type_identifier", "scoped_type_identifier"):
                        base = _node_text(src_bytes, sub)
                        if base:
                            out.append((cls_name, base, "implements", line))
    return out


# ---------- JS / TS ----------
def _js_inheritance(tree: Any, source: str) -> List[Tuple[str, str, str, int]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    out: List[Tuple[str, str, str, int]] = []
    for node in _walk(tree.root_node):
        if node.type not in ("class_declaration", "class_expression", "class"):
            continue
        name_node = _find_class_name(node)
        cls_name = _node_text(src_bytes, name_node) if name_node else "<anonymous>"
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type != "class_heritage":
                continue
            # Two grammar dialects exist:
            #   * Plain JS:  class_heritage → 'extends' + identifier
            #   * TS / newer: class_heritage → extends_clause + implements_clause
            # We handle both by looking at every child of class_heritage and
            # tracking which clause we are currently inside.
            mode = "extends"  # default mode for plain JS
            seen_first_extends_target = False
            for sub in child.children:
                if sub.type == "extends_clause":
                    # Walk into the wrapper and grab the FIRST type-ish identifier
                    for ext_child in _walk(sub):
                        if ext_child.type in (
                            "identifier",
                            "type_identifier",
                            "member_expression",
                        ):
                            base = _node_text(src_bytes, ext_child)
                            if base and base != "extends":
                                out.append((cls_name, base, "extends", line))
                                break
                elif sub.type == "implements_clause":
                    for impl_child in _walk(sub):
                        if impl_child.type in ("type_identifier", "identifier"):
                            base = _node_text(src_bytes, impl_child)
                            if base and base != "implements":
                                out.append((cls_name, base, "implements", line))
                elif sub.type == "extends":
                    # Plain JS keyword node — switch mode to extends
                    mode = "extends"
                elif sub.type == "implements":
                    mode = "implements"
                elif sub.type in ("identifier", "type_identifier", "member_expression"):
                    # Plain JS path: identifiers immediately under class_heritage
                    if mode == "extends" and seen_first_extends_target:
                        # JS only allows a single base — anything past the first is junk
                        continue
                    base = _node_text(src_bytes, sub)
                    if base:
                        kind = "implements" if mode == "implements" else "extends"
                        out.append((cls_name, base, kind, line))
                        if mode == "extends":
                            seen_first_extends_target = True
    return out


# ---------- Rust ----------
def _rust_inheritance(tree: Any, source: str) -> List[Tuple[str, str, str, int]]:
    if tree is None:
        return []
    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    out: List[Tuple[str, str, str, int]] = []
    for node in _walk(tree.root_node):
        if node.type != "impl_item":
            continue
        line = node.start_point[0] + 1
        # impl_item structure:  impl <generics>? <trait>? for <type> { ... }
        trait_name = None
        type_name = None
        # Heuristic: find type_identifier nodes; if there are 2, the first
        # is the trait and the second is the implementor.  If there's only
        # one, this is a plain `impl X { ... }` — no inheritance to record.
        type_idents: List[str] = []
        for child in node.children:
            if child.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
                # generic_type contains a type_identifier inside; recurse
                for sub in _walk(child):
                    if sub.type == "type_identifier":
                        type_idents.append(_node_text(src_bytes, sub))
                        break
                else:
                    type_idents.append(_node_text(src_bytes, child))
        # Distinct names please
        unique = []
        for t in type_idents:
            if t and t not in unique:
                unique.append(t)
        if len(unique) >= 2:
            trait_name, type_name = unique[0], unique[1]
            out.append((type_name, trait_name, "impls", line))
    return out


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class InheritanceGraphBuilder:
    """Build (and incrementally update) an :class:`InheritanceGraph`."""

    EXT_LANG_MAP = {
        ".py":  "python",
        ".js":  "javascript",
        ".jsx": "javascript",
        ".ts":  "typescript",
        ".tsx": "typescript",
        ".cpp": "cpp",
        ".cc":  "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hxx": "cpp",
        ".h":   "cpp",
        ".java": "java",
        ".rs":  "rust",
        # NOTE: we deliberately omit Go here — Go has no class inheritance
        # in the OO sense; struct embedding is handled by code search rather
        # than by this graph.
    }
    DEFAULT_EXTS = tuple(EXT_LANG_MAP.keys())
    SKIP_DIRS = {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        "dist",
        "build",
        "target",
    }

    def __init__(self, parser: UnifiedASTParser) -> None:
        self.parser = parser

    # ------------------------------------------------------------------ extract
    def _extract(
        self, code: str, language: str
    ) -> List[Tuple[str, str, str, int]]:
        lang = _normalize_lang(language)
        tree = self.parser.parse(code, lang)
        if tree is None:
            return []
        if lang == "python":
            return _python_inheritance(tree, code)
        if lang == "cpp":
            return _cpp_inheritance(tree, code)
        if lang == "java":
            return _java_inheritance(tree, code)
        if lang in ("javascript", "typescript"):
            return _js_inheritance(tree, code)
        if lang == "rust":
            return _rust_inheritance(tree, code)
        return []

    def build_for_content(
        self, content: str, language: str, file_path: Optional[str] = None
    ) -> InheritanceGraph:
        graph = InheritanceGraph()
        self._add_from_content(graph, content, language, file_path)
        return graph

    def build_for_file(self, file_path: str) -> InheritanceGraph:
        graph = InheritanceGraph()
        self._add_from_file(graph, file_path)
        return graph

    def build_for_paths(
        self,
        paths: Iterable[str],
        max_files: int = 500,
        extensions: Optional[Iterable[str]] = None,
    ) -> InheritanceGraph:
        graph = InheritanceGraph()
        exts = tuple(extensions or self.DEFAULT_EXTS)
        files_processed = 0
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            if os.path.isfile(p):
                if files_processed >= max_files:
                    break
                self._add_from_file(graph, p)
                files_processed += 1
                continue
            for root, dirs, files in os.walk(p):
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in self.SKIP_DIRS and not d.startswith(".")
                ]
                for fn in files:
                    if files_processed >= max_files:
                        break
                    if not fn.lower().endswith(exts):
                        continue
                    self._add_from_file(graph, os.path.join(root, fn))
                    files_processed += 1
                if files_processed >= max_files:
                    break
            if files_processed >= max_files:
                break
        return graph

    # ------------------------------------------------------------------ incremental
    def update_file(
        self, graph: InheritanceGraph, file_path: str
    ) -> Dict[str, int]:
        removed = graph.remove_file(file_path)
        added = 0
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            language = self.EXT_LANG_MAP.get(ext)
            if language is None:
                return {"removed": removed, "added": 0}
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except Exception as exc:
                logger.debug("update_file: read failed for %s: %s", file_path, exc)
                return {"removed": removed, "added": 0}
            before = len(graph.edges)
            self._add_from_content(graph, content, language, file_path)
            added = len(graph.edges) - before
        return {"removed": removed, "added": added}

    # ------------------------------------------------------------------ helpers
    def _add_from_file(
        self, graph: InheritanceGraph, file_path: str
    ) -> None:
        ext = os.path.splitext(file_path)[1].lower()
        language = self.EXT_LANG_MAP.get(ext)
        if not language:
            return
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except Exception as exc:
            logger.debug("Skip %s: %s", file_path, exc)
            return
        self._add_from_content(graph, content, language, file_path)

    def _add_from_content(
        self,
        graph: InheritanceGraph,
        content: str,
        language: str,
        file_path: Optional[str],
    ) -> None:
        try:
            edges = self._extract(content, language)
        except Exception as exc:
            logger.debug(
                "inheritance extract failed for %s: %s", file_path or "?", exc
            )
            return
        for sub, base, kind, line in edges:
            if not sub or not base:
                continue
            graph.add_edge(
                InheritanceEdge(
                    subclass=sub,
                    base=base,
                    kind=kind,
                    line=line,
                    file_path=file_path,
                    language=language,
                )
            )


__all__ = [
    "InheritanceEdge",
    "InheritanceGraph",
    "InheritanceGraphBuilder",
]
