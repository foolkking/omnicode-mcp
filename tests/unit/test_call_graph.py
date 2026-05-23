"""STAGE 11.3 — Unit tests for the call graph (STAGE 3.8 + 3.10)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnicode.ast_engine.graph import CallEdge, CallGraph, CallGraphBuilder
from omnicode.ast_engine.parser import UnifiedASTParser


@pytest.fixture(scope="module")
def parser():
    return UnifiedASTParser()


@pytest.fixture
def builder(parser):
    return CallGraphBuilder(parser)


# ---------------------------------------------------------------------------
# Static behaviour
# ---------------------------------------------------------------------------
class TestCallGraphCore:
    def test_add_edge_builds_indices(self):
        graph = CallGraph()
        graph.add_edge(CallEdge(caller="A", callee="B", line=1))
        graph.add_edge(CallEdge(caller="A", callee="C", line=2))
        graph.add_edge(CallEdge(caller="B", callee="C", line=3))
        assert sorted(graph.callees_of("A")) == ["B", "C"]
        assert graph.callers_of("C") == ["A", "B"]
        assert graph.callees_of("nobody") == []
        assert graph.callers_of("nobody") == []

    def test_stats(self):
        graph = CallGraph()
        for i, (a, b) in enumerate([("a", "b"), ("a", "c"), ("b", "c")]):
            graph.add_edge(CallEdge(caller=a, callee=b, line=i, file_path="f.py"))
        s = graph.stats()
        assert s["total_edges"] == 3
        assert s["files_indexed"] == 1


# ---------------------------------------------------------------------------
# Incremental updates (STAGE 3.10)
# ---------------------------------------------------------------------------
class TestIncrementalUpdate:
    def test_remove_file_compacts_indices(self, tmp_path: Path, builder):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("def caller():\n    other()\n")
        f2.write_text("def other():\n    third()\n")

        graph = builder.build_for_paths([str(tmp_path)])
        before = graph.stats()
        assert before["total_edges"] >= 2

        # Drop edges from f2 — the 'other → third' edge should disappear.
        removed = graph.remove_file(str(f2))
        assert removed >= 1
        # in/out indices must reflect removal
        assert "third" not in graph.in_index or not graph.in_index["third"]

    def test_update_file_after_modification(self, tmp_path: Path, builder):
        f1 = tmp_path / "x.py"
        f1.write_text("def caller():\n    helper()\n    other()\n")
        graph = builder.build_for_paths([str(tmp_path)])
        # Initially caller → other should be an edge.
        assert "other" in graph.callees_of("caller")

        # Modify the file — replace `other()` with `totally_new()`.
        f1.write_text("def caller():\n    helper()\n    totally_new()\n")
        delta = builder.update_file(graph, str(f1))
        assert delta["removed"] >= 1
        assert delta["added"] >= 1
        assert "totally_new" in graph.callees_of("caller")
        # Sanity: the edge that originally pointed to 'other' from f1 is gone
        for e in graph.edges:
            if e.caller == "caller" and e.callee == "other":
                # If still present it must have come from a DIFFERENT file
                assert e.file_path != str(f1)

    def test_update_file_handles_deletion(self, tmp_path: Path, builder):
        f1 = tmp_path / "x.py"
        f1.write_text("def caller():\n    inner()\n")
        graph = builder.build_for_paths([str(tmp_path)])
        assert any(e.callee == "inner" for e in graph.edges)

        # Delete the file on disk and call update_file.
        os.remove(f1)
        delta = builder.update_file(graph, str(f1))
        assert delta["removed"] >= 1
        assert delta["added"] == 0
        # No remaining edge should reference the deleted file.
        assert all(
            (e.file_path is None) or os.path.basename(e.file_path) != "x.py"
            for e in graph.edges
        )

    def test_update_file_unsupported_extension_is_no_op(
        self, tmp_path: Path, builder
    ):
        f = tmp_path / "data.unknown"
        f.write_text("garbage")
        graph = CallGraph()
        delta = builder.update_file(graph, str(f))
        assert delta == {"removed": 0, "added": 0}

    def test_remove_file_unknown_returns_zero(self):
        graph = CallGraph()
        assert graph.remove_file("/does/not/exist.py") == 0
