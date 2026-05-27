"""
Impact Analysis — assess the blast radius of modifying a symbol.

Provides:
    get_impact_radius(symbol, depth) — BFS from symbol through call graph
    find_entrypoints(symbol) — which top-level entry points reach this symbol
    find_dead_symbols() — symbols with 0 callers (potential dead code)
    suggest_related_tests(symbol) — test files that likely cover this symbol
    assess_risk(symbol) — low/medium/high risk rating

All functions work against the existing call graph builder and do NOT
require an LLM.  They're designed to be called by omni_analyze() and
the /patch/validate endpoint to give AI editors pre-edit context.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ImpactAnalyzer:
    """Analyzes the impact of modifying a symbol using the call graph."""

    def __init__(self, working_dir: str):
        self.working_dir = working_dir

    async def get_impact_radius(
        self,
        symbol: str,
        depth: int = 2,
        max_files: int = 200,
        scope_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """BFS from symbol through the call graph up to `depth` hops.

        Returns all symbols that could be affected by a change to `symbol`.
        """
        graph = await self._build_graph(max_files, scope_path)
        if not graph:
            return {"error": "Could not build call graph"}

        # BFS outward (callees — what this symbol affects)
        affected = self._bfs(symbol, depth, graph["out_index"])
        # BFS inward (callers — what depends on this symbol)
        dependents = self._bfs(symbol, depth, graph["in_index"])

        # Unique files involved
        all_symbols = affected | dependents | {symbol}
        files = set()
        for edge in graph["edges"]:
            if edge.get("caller") in all_symbols or edge.get("callee") in all_symbols:
                fp = edge.get("file_path", "")
                if fp:
                    rel = os.path.relpath(fp, self.working_dir) if os.path.isabs(fp) else fp
                    files.add(rel.replace("\\", "/"))

        return {
            "symbol": symbol,
            "depth": depth,
            "affected_symbols": list(affected),
            "dependent_symbols": list(dependents),
            "affected_count": len(affected),
            "dependent_count": len(dependents),
            "files_involved": sorted(files),
            "files_count": len(files),
            "total_blast_radius": len(all_symbols),
        }

    async def find_entrypoints(
        self, symbol: str, max_files: int = 200
    ) -> Dict[str, Any]:
        """Find top-level entry points that eventually call this symbol.

        Entry points are symbols with 0 callers (roots of the call graph).
        """
        graph = await self._build_graph(max_files)
        if not graph:
            return {"error": "Could not build call graph"}

        in_index = graph["in_index"]
        out_index = graph["out_index"]

        # Find all roots (0 callers)
        all_nodes = set(in_index.keys()) | set(out_index.keys())
        roots = {n for n in all_nodes if not in_index.get(n)}

        # BFS backward from symbol to find which roots reach it
        reachable_from = self._bfs_reverse(symbol, 20, in_index)
        entry_points = roots & reachable_from

        return {
            "symbol": symbol,
            "entry_points": sorted(entry_points),
            "count": len(entry_points),
            "total_roots_in_graph": len(roots),
        }

    async def find_dead_symbols(self, max_files: int = 200) -> Dict[str, Any]:
        """Find symbols with 0 callers (potential dead code).

        Excludes known entry patterns: main, __init__, test_*, app, etc.
        """
        graph = await self._build_graph(max_files)
        if not graph:
            return {"error": "Could not build call graph"}

        in_index = graph["in_index"]
        out_index = graph["out_index"]
        all_nodes = set(in_index.keys()) | set(out_index.keys())

        # Entry-point patterns to exclude
        ENTRY_PATTERNS = {
            "main", "app", "create_app", "__init__", "__main__",
            "setup", "teardown", "conftest",
        }

        dead = []
        for node in all_nodes:
            if in_index.get(node):
                continue  # has callers — not dead
            # Exclude known entry points and test functions
            lower = node.lower()
            if any(p in lower for p in ENTRY_PATTERNS):
                continue
            if lower.startswith("test_"):
                continue
            dead.append(node)

        dead.sort()
        return {
            "dead_symbols": dead[:200],
            "count": len(dead),
            "total_symbols": len(all_nodes),
            "note": "Symbols with 0 callers (excluding entry points and tests)",
        }

    async def suggest_related_tests(
        self, symbol: str, max_files: int = 200
    ) -> Dict[str, Any]:
        """Suggest test files that likely cover this symbol."""
        graph = await self._build_graph(max_files)
        if not graph:
            return {"error": "Could not build call graph"}

        # Find all callers (transitive, depth 3)
        callers = self._bfs(symbol, 3, graph["in_index"])
        callers.add(symbol)

        # Find test files that reference any of these symbols
        test_files = set()
        for edge in graph["edges"]:
            caller = edge.get("caller", "")
            fp = edge.get("file_path", "")
            if not fp:
                continue
            rel = os.path.relpath(fp, self.working_dir) if os.path.isabs(fp) else fp
            rel = rel.replace("\\", "/")

            # Is this a test file?
            if "test" in rel.lower() and (
                caller.startswith("test_") or caller in callers
            ):
                test_files.add(rel)

        # Also check by filename convention
        symbol_lower = symbol.lower()
        for root, dirs, files in os.walk(self.working_dir):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".data", ".venv"}]
            for f in files:
                if f.startswith("test_") and f.endswith(".py"):
                    if symbol_lower in f.lower():
                        rel = os.path.relpath(os.path.join(root, f), self.working_dir)
                        test_files.add(rel.replace("\\", "/"))

        suggested = sorted(test_files)
        commands = [f"pytest {t}" for t in suggested[:5]]

        return {
            "symbol": symbol,
            "test_files": suggested,
            "count": len(suggested),
            "suggested_commands": commands,
        }

    async def assess_risk(
        self, symbol: str, max_files: int = 200
    ) -> Dict[str, Any]:
        """Assess the risk level of modifying a symbol.

        Risk factors:
          - Number of direct callers
          - Number of files affected
          - Whether it's called from tests
          - Whether it's a public API
        """
        impact = await self.get_impact_radius(symbol, depth=2, max_files=max_files)
        if "error" in impact:
            return impact

        tests = await self.suggest_related_tests(symbol, max_files=max_files)

        caller_count = impact["dependent_count"]
        file_count = impact["files_count"]
        test_count = tests.get("count", 0)

        # Risk scoring
        score = 0
        reasons = []

        if caller_count > 10:
            score += 3
            reasons.append(f"High caller count ({caller_count})")
        elif caller_count > 3:
            score += 2
            reasons.append(f"Moderate caller count ({caller_count})")
        elif caller_count > 0:
            score += 1

        if file_count > 5:
            score += 2
            reasons.append(f"Affects {file_count} files")
        elif file_count > 2:
            score += 1

        if test_count == 0:
            score += 2
            reasons.append("No test coverage found")
        elif test_count < 2:
            score += 1
            reasons.append("Limited test coverage")

        risk = "high" if score >= 5 else "medium" if score >= 3 else "low"

        return {
            "symbol": symbol,
            "risk": risk,
            "risk_score": score,
            "reasons": reasons,
            "direct_callers": caller_count,
            "files_affected": file_count,
            "test_coverage": test_count,
            "suggested_checks": tests.get("suggested_commands", []),
        }

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    async def _build_graph(
        self, max_files: int = 200, scope_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Build call graph using existing AST infrastructure."""
        try:
            from omnicode.ast_engine.parser import UnifiedASTParser
            from omnicode.ast_engine.graph import CallGraphBuilder

            parser = UnifiedASTParser()
            builder = CallGraphBuilder(parser)

            target = self.working_dir
            if scope_path:
                target = os.path.join(self.working_dir, scope_path)

            graph = builder.build_for_paths([target], max_files=max_files)

            # Build adjacency indices
            in_index: Dict[str, Set[str]] = {}
            out_index: Dict[str, Set[str]] = {}

            edges_raw = []
            for edge in graph.edges:
                d = edge.model_dump() if hasattr(edge, "model_dump") else edge.dict()
                caller = d.get("caller", "")
                callee = d.get("callee", "")
                edges_raw.append(d)

                if callee not in in_index:
                    in_index[callee] = set()
                in_index[callee].add(caller)

                if caller not in out_index:
                    out_index[caller] = set()
                out_index[caller].add(callee)

            return {
                "edges": edges_raw,
                "in_index": in_index,
                "out_index": out_index,
            }
        except Exception as e:
            logger.warning(f"Failed to build call graph: {e}")
            return None

    @staticmethod
    def _bfs(start: str, depth: int, adjacency: Dict[str, Set[str]]) -> Set[str]:
        """BFS from start through adjacency map."""
        visited = set()
        frontier = {start}
        for _ in range(depth):
            next_frontier = set()
            for node in frontier:
                for neighbor in adjacency.get(node, set()):
                    if neighbor not in visited and neighbor != start:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        return visited

    @staticmethod
    def _bfs_reverse(start: str, depth: int, in_index: Dict[str, Set[str]]) -> Set[str]:
        """BFS backward (through callers) from start."""
        visited = set()
        frontier = {start}
        for _ in range(depth):
            next_frontier = set()
            for node in frontier:
                for caller in in_index.get(node, set()):
                    if caller not in visited and caller != start:
                        visited.add(caller)
                        next_frontier.add(caller)
            frontier = next_frontier
            if not frontier:
                break
        return visited
