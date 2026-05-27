"""STAGE 11.5 — Unit tests for the inheritance graph (STAGE 3.11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode.ast_engine.inheritance import (
    InheritanceGraphBuilder,
)
from omnicode.ast_engine.parser import UnifiedASTParser


@pytest.fixture(scope="module")
def parser():
    return UnifiedASTParser()


@pytest.fixture
def builder(parser):
    return InheritanceGraphBuilder(parser)


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
class TestPythonInheritance:
    def test_simple_extends(self, builder):
        code = (
            "class Animal:\n    pass\n\n"
            "class Dog(Animal):\n    pass\n\n"
            "class Puppy(Dog):\n    pass\n"
        )
        graph = builder.build_for_content(code, "python")
        assert graph.base_classes_of("Dog") == ["Animal"]
        assert graph.base_classes_of("Puppy") == ["Dog"]
        assert graph.subclasses_of("Animal") == ["Dog"]
        # Transitive
        assert "Animal" in graph.ancestors_of("Puppy")
        assert "Dog"    in graph.ancestors_of("Puppy")
        assert "Puppy"  in graph.descendants_of("Animal")

    def test_multiple_bases(self, builder):
        code = (
            "class A: pass\n"
            "class B: pass\n"
            "class C(A, B): pass\n"
        )
        graph = builder.build_for_content(code, "python")
        assert sorted(graph.base_classes_of("C")) == ["A", "B"]

    def test_no_base_no_edges(self, builder):
        graph = builder.build_for_content("class Foo: pass\n", "python")
        assert graph.edges == []
        assert graph.base_classes_of("Foo") == []


# ---------------------------------------------------------------------------
# JS / TS
# ---------------------------------------------------------------------------
class TestJsTsInheritance:
    def test_javascript_extends(self, builder):
        code = (
            "class Base {}\n"
            "class Child extends Base {}\n"
            "class Grandchild extends Child {}\n"
        )
        graph = builder.build_for_content(code, "javascript")
        assert graph.base_classes_of("Child") == ["Base"]
        assert "Base" in graph.ancestors_of("Grandchild")

    def test_typescript_implements_and_extends(self, builder):
        code = (
            "interface Speaker {}\n"
            "class Animal {}\n"
            "class Dog extends Animal implements Speaker {}\n"
        )
        graph = builder.build_for_content(code, "typescript")
        bases = graph.base_classes_of("Dog")
        assert "Animal" in bases
        assert "Speaker" in bases
        # Verify edge kinds
        kinds = {(e.subclass, e.base): e.kind for e in graph.edges}
        assert kinds[("Dog", "Animal")] == "extends"
        assert kinds[("Dog", "Speaker")] == "implements"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------
class TestJavaInheritance:
    def test_class_extends_and_implements(self, builder):
        code = (
            "interface Walks {}\n"
            "interface Talks {}\n"
            "class Animal {}\n"
            "class Person extends Animal implements Walks, Talks {}\n"
        )
        graph = builder.build_for_content(code, "java")
        bases = graph.base_classes_of("Person")
        assert "Animal" in bases
        assert "Walks" in bases
        assert "Talks" in bases
        kinds = {(e.subclass, e.base): e.kind for e in graph.edges}
        assert kinds[("Person", "Animal")] == "extends"
        assert kinds[("Person", "Walks")] == "implements"


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------
class TestCppInheritance:
    def test_class_with_one_base(self, builder):
        code = (
            "class Base { public: int x; };\n"
            "class Derived : public Base { public: int y; };\n"
        )
        graph = builder.build_for_content(code, "cpp")
        assert graph.base_classes_of("Derived") == ["Base"]

    def test_multiple_inheritance(self, builder):
        code = (
            "class A {};\n"
            "class B {};\n"
            "class C : public A, public B {};\n"
        )
        graph = builder.build_for_content(code, "cpp")
        bases = graph.base_classes_of("C")
        assert "A" in bases and "B" in bases


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------
class TestRustInheritance:
    def test_impl_trait_for_struct(self, builder):
        code = (
            "trait Speaker {}\n"
            "struct Dog;\n"
            "impl Speaker for Dog {}\n"
        )
        graph = builder.build_for_content(code, "rust")
        assert "Speaker" in graph.base_classes_of("Dog")
        kinds = {e.kind for e in graph.edges}
        assert "impls" in kinds


# ---------------------------------------------------------------------------
# Incremental updates
# ---------------------------------------------------------------------------
class TestIncrementalInheritance:
    def test_update_file_adds_then_removes_edge(
        self, tmp_path: Path, builder
    ):
        f1 = tmp_path / "x.py"
        f1.write_text("class A: pass\nclass B(A): pass\n")
        graph = builder.build_for_paths([str(tmp_path)])
        assert "A" in graph.base_classes_of("B")

        # Modify: drop the inheritance
        f1.write_text("class A: pass\nclass B: pass\n")
        delta = builder.update_file(graph, str(f1))
        assert delta["removed"] >= 1
        assert "A" not in graph.base_classes_of("B")

    def test_remove_file_compacts(self, tmp_path: Path, builder):
        f1 = tmp_path / "x.py"
        f1.write_text("class A: pass\nclass B(A): pass\n")
        graph = builder.build_for_paths([str(tmp_path)])
        assert graph.stats()["total_edges"] >= 1
        graph.remove_file(str(f1))
        assert graph.stats()["total_edges"] == 0
        # Adding a different file again should work
        f2 = tmp_path / "y.py"
        f2.write_text("class P: pass\nclass Q(P): pass\n")
        builder.update_file(graph, str(f2))
        assert "P" in graph.base_classes_of("Q")
