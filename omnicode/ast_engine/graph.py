"""
Function call graph (STAGE 3.8 + 3.10)
======================================
Builds a directional call graph (caller → callee) for one or many files
and supports the canonical AST queries:

* "Who calls X?"  →  ``callers_of(symbol)``
* "What does X call?"  →  ``callees_of(symbol)``

STAGE 3.10 — incremental indexing
---------------------------------
Every edge knows the file it came from. The :meth:`CallGraph.update_file`
helper drops just the edges originating in a specific file and re-runs the
parser on the new content, avoiding a full graph rebuild.  This is what
allows the search service to update the graph on every save without paying
the O(N) walk-the-codebase cost.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from pydantic import BaseModel

from .parser import UnifiedASTParser

logger = logging.getLogger(__name__)


class CallEdge(BaseModel):
    caller: str
    callee: str
    line: int
    file_path: Optional[str] = None
    language: Optional[str] = None


@dataclass
class CallGraph:
    """In-memory directional call graph with incremental update support."""

    edges: List[CallEdge] = field(default_factory=list)
    out_index: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    in_index: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    # Maps absolute file path -> indices into ``edges`` so we can drop them.
    _by_file: Dict[str, Set[int]] = field(default_factory=lambda: defaultdict(set))
    _lock: "threading.RLock" = field(default_factory=threading.RLock, repr=False, compare=False)

    # ------------------------------------------------------------------
    def add_edge(self, edge: CallEdge) -> None:
        with self._lock:
            idx = len(self.edges)
            self.edges.append(edge)
            self.out_index[edge.caller].add(edge.callee)
            self.in_index[edge.callee].add(edge.caller)
            if edge.file_path:
                self._by_file[os.path.abspath(edge.file_path)].add(idx)

    def callers_of(self, symbol: str) -> List[str]:
        """Return the symbols that call ``symbol`` (in-edges)."""
        return sorted(self.in_index.get(symbol, set()))

    def callees_of(self, symbol: str) -> List[str]:
        """Return the symbols called by ``symbol`` (out-edges)."""
        return sorted(self.out_index.get(symbol, set()))

    def edges_for(self, symbol: str, direction: str = "out") -> List[CallEdge]:
        if direction == "in":
            return [e for e in self.edges if e.callee == symbol]
        return [e for e in self.edges if e.caller == symbol]

    def to_dict(self) -> Dict[str, object]:
        return {
            "total_edges": len(self.edges),
            "total_callers": len(self.out_index),
            "total_callees": len(self.in_index),
            "files_indexed": len(self._by_file),
            "edges": [e.dict() for e in self.edges],
        }

    def stats(self) -> Dict[str, int]:
        """Lightweight diagnostic — no full edge list."""
        return {
            "total_edges": len(self.edges),
            "total_callers": len(self.out_index),
            "total_callees": len(self.in_index),
            "files_indexed": len(self._by_file),
        }

    # ------------------------------------------------------------------ incremental
    def remove_file(self, file_path: str) -> int:
        """Drop every edge that came from ``file_path``.

        Returns the number of edges removed.  Indices into ``self.edges`` are
        compacted so the rest of the graph stays consistent.
        """
        with self._lock:
            abs_path = os.path.abspath(file_path)
            edge_indices = self._by_file.pop(abs_path, set())
            if not edge_indices:
                return 0

            kept_edges: List[CallEdge] = []
            keep_mask = [True] * len(self.edges)
            for i in edge_indices:
                if 0 <= i < len(keep_mask):
                    keep_mask[i] = False

            # Rebuild edges + adjust indices in _by_file so they still point
            # to the right positions after compaction.
            old_to_new: Dict[int, int] = {}
            for old_idx, edge in enumerate(self.edges):
                if keep_mask[old_idx]:
                    old_to_new[old_idx] = len(kept_edges)
                    kept_edges.append(edge)
            self.edges = kept_edges

            # Rebuild out/in indices from scratch — cheaper and safer than
            # trying to mutate them edge-by-edge.
            self.out_index = defaultdict(set)
            self.in_index = defaultdict(set)
            for e in self.edges:
                self.out_index[e.caller].add(e.callee)
                self.in_index[e.callee].add(e.caller)

            # Translate _by_file indices through old_to_new
            new_by_file: Dict[str, Set[int]] = defaultdict(set)
            for f, idxs in self._by_file.items():
                translated = {old_to_new[i] for i in idxs if i in old_to_new}
                if translated:
                    new_by_file[f] = translated
            self._by_file = new_by_file
            return len(edge_indices)

    def files_indexed(self) -> List[str]:
        with self._lock:
            return sorted(self._by_file.keys())

    # ------------------------------------------------------------------
    def render_ascii(self, max_nodes: int = 50) -> str:
        """Cheap ASCII rendering for human inspection."""
        if not self.edges:
            return "(empty call graph)"
        callers = sorted(self.out_index.keys())[:max_nodes]
        lines = ["📞 Call Graph (caller → callees):"]
        for c in callers:
            targets = sorted(self.out_index[c])
            arrow_targets = ", ".join(targets[:8]) + (
                f"  …(+{len(targets) - 8} more)" if len(targets) > 8 else ""
            )
            lines.append(f"  ├── {c} → {arrow_targets}")
        if len(self.out_index) > max_nodes:
            lines.append(f"  └── … and {len(self.out_index) - max_nodes} more callers")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class CallGraphBuilder:
    """Build (and incrementally update) a :class:`CallGraph`."""

    DEFAULT_EXTS = (
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cpp",
        ".cc",
        ".c",
        ".h",
        ".hpp",
        ".java",
        ".go",
        ".rs",
    )

    EXT_LANG_MAP = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".c": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
    }

    SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git", "dist", "build", "target"}

    def __init__(self, parser: UnifiedASTParser) -> None:
        self.parser = parser

    # ------------------------------------------------------------------ files
    def build_for_content(
        self,
        content: str,
        language: str,
        file_path: Optional[str] = None,
    ) -> CallGraph:
        graph = CallGraph()
        self._add_from_content(graph, content, language, file_path)
        return graph

    def build_for_file(self, file_path: str) -> CallGraph:
        graph = CallGraph()
        self._add_from_file(graph, file_path)
        return graph

    def build_for_paths(
        self,
        paths: Iterable[str],
        max_files: int = 500,
        extensions: Optional[Iterable[str]] = None,
    ) -> CallGraph:
        graph = CallGraph()
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
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS and not d.startswith(".")]
                for fn in files:
                    if files_processed >= max_files:
                        break
                    if not fn.lower().endswith(exts):
                        continue
                    full = os.path.join(root, fn)
                    self._add_from_file(graph, full)
                    files_processed += 1
                if files_processed >= max_files:
                    break
            if files_processed >= max_files:
                break

        return graph

    # ------------------------------------------------------------------ incremental
    def update_file(self, graph: CallGraph, file_path: str) -> Dict[str, int]:
        """Re-index a single file in-place. Returns ``{removed, added}`` counts.

        If the file no longer exists on disk we just drop its edges. This
        keeps the graph in sync with deletes too.
        """
        removed = graph.remove_file(file_path)
        added = 0
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            language = self.EXT_LANG_MAP.get(ext)
            if language is None:
                logger.debug("update_file: unsupported language for %s", file_path)
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
    def _add_from_file(self, graph: CallGraph, file_path: str) -> None:
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
        graph: CallGraph,
        content: str,
        language: str,
        file_path: Optional[str],
    ) -> None:
        try:
            calls = self.parser.extract_calls(content, language)
        except Exception as exc:
            logger.debug("extract_calls failed for %s: %s", file_path or "?", exc)
            return
        for caller, callee, line in calls:
            graph.add_edge(
                CallEdge(
                    caller=caller,
                    callee=callee,
                    line=line,
                    file_path=file_path,
                    language=language,
                )
            )


# ---------------------------------------------------------------------------
# Backwards-compat shim used by some legacy callers
# ---------------------------------------------------------------------------
class CodeGraph:
    """Backwards-compat thin wrapper around :class:`CallGraphBuilder`."""

    def __init__(self, parser: UnifiedASTParser) -> None:
        self.builder = CallGraphBuilder(parser)

    def build_call_graph(self, code: str, language: str) -> List[CallEdge]:
        graph = self.builder.build_for_content(code, language)
        return graph.edges
