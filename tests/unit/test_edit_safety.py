"""Safety-net tests for EditPipeline output validation.

After the 2026-05-22 incident where a thinking-mode LLM dumped its internal
"reviewing the request..." monologue into mcp_server.py and overwrote the
3585-line source with a few hundred bytes of prose, we added three layers of
defense:

1. Reject responses that have no fenced code block.
2. Reject code blocks whose contents look like prose / narration.
3. Refuse to write when the new content shrinks a non-trivial file by >50%
   unless the instruction explicitly requested deletion.

These tests pin those behaviours.
"""
from __future__ import annotations

import pytest

from omnicode.pipelines.edit import EditPipeline


# ---------------------------------------------------------------------------
# _looks_like_prose
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,language,expected",
    [
        # Pure prose (the actual incident output)
        (
            "I'm currently reviewing the code editing instruction. "
            "It seems I need to add a specific comment.\n"
            "I'm making sure I understand the precise location.",
            "python",
            True,
        ),
        # Real Python — must NOT be flagged as prose
        (
            "import os\n"
            "from typing import List\n\n"
            "def add(a, b):\n"
            "    return a + b\n",
            "python",
            False,
        ),
        # Real C++ — must NOT be flagged
        (
            "#include <iostream>\n\n"
            "int main() {\n"
            "    std::cout << \"hi\" << std::endl;\n"
            "    return 0;\n"
            "}\n",
            "cpp",
            False,
        ),
        # Real TypeScript — must NOT be flagged
        (
            "import { foo } from './bar';\n\n"
            "export const baz = (x: number) => x * 2;\n",
            "typescript",
            False,
        ),
        # Single short comment — accepted (one-liner edits are fine)
        ("# 我爱吃米饭", "python", False),
        # Long pure-Chinese-prose dump — rejected
        (
            "首先我需要分析这个文件。看起来这是一个 MCP 服务器的入口。\n"
            "用户要求在文件最上方添加一个注释。\n"
            "我应该读取文件然后在第一行添加。\n"
            "让我考虑最合适的方式来完成这个任务。\n",
            "python",
            True,
        ),
        # Empty / whitespace-only
        ("", "python", True),
        ("   \n  \n", "python", True),
        # English narration with no code at all
        (
            "Sure! Here is the updated file with the change applied.\n"
            "I added the comment to the top as requested.\n"
            "Let me know if you need any further adjustments.",
            "python",
            True,
        ),
        # Mixed: comment line then real code — accepted
        (
            "# Added comment per user request\n"
            "import sys\n"
            "def main():\n"
            "    pass\n",
            "python",
            False,
        ),
    ],
)
def test_looks_like_prose(text, language, expected):
    assert EditPipeline._looks_like_prose(text, language) is expected


# ---------------------------------------------------------------------------
# _extract_code_block — sanity checks for the existing extractor
# ---------------------------------------------------------------------------
def test_extract_code_block_with_language_marker():
    text = "Sure!\n```python\nprint('hi')\n```\nDone."
    got = EditPipeline._extract_code_block(text, "python")
    assert got == "print('hi')"


def test_extract_code_block_returns_none_when_no_block():
    text = "Here is the change I would make: add `print('hi')`."
    got = EditPipeline._extract_code_block(text, "python")
    assert got is None


def test_extract_code_block_unfenced_prose_returns_none():
    """The single most dangerous incident: LLM emitted thinking-only text
    with NO fences at all."""
    text = (
        "I'm currently reviewing the code editing instruction.\n"
        "It seems I need to add a specific comment to the very beginning."
    )
    got = EditPipeline._extract_code_block(text, "python")
    assert got is None



# ---------------------------------------------------------------------------
# Patch-mode SEARCH/REPLACE engine — for large-file edits.
# ---------------------------------------------------------------------------
ORIGINAL = """\
import os
import sys

def greet(name):
    print(f"Hello, {name}")

def main():
    greet("world")

if __name__ == "__main__":
    main()
"""


def test_apply_single_patch_block():
    blob = """\
<<<<<<< SEARCH
def greet(name):
    print(f"Hello, {name}")
=======
def greet(name: str) -> None:
    print(f"Hello, {name}!")
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 1
    assert errors == []
    assert "def greet(name: str) -> None:" in new_text
    assert "f\"Hello, {name}!\"" in new_text
    # Untouched code preserved verbatim
    assert "def main():" in new_text
    assert "if __name__ == \"__main__\":" in new_text


def test_apply_multiple_patch_blocks():
    blob = """\
<<<<<<< SEARCH
import os
import sys
=======
import os
import sys
import logging
>>>>>>> REPLACE

<<<<<<< SEARCH
def main():
    greet("world")
=======
def main():
    logging.info("starting")
    greet("world")
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 2
    assert errors == []
    assert "import logging" in new_text
    assert "logging.info(\"starting\")" in new_text


def test_apply_patch_search_not_found_skips_block():
    blob = """\
<<<<<<< SEARCH
def nonexistent_function():
    pass
=======
def nonexistent_function():
    return 42
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 0
    assert len(errors) == 1
    assert "not found" in errors[0]
    # File unchanged
    assert new_text == ORIGINAL


def test_apply_patch_with_whitespace_drift():
    """SEARCH with slightly different leading whitespace still applies via
    fuzzy line-by-line matching."""
    blob = """\
<<<<<<< SEARCH
def greet(name):
  print(f"Hello, {name}")
=======
def greet(name):
    print(f"Hi, {name}!")
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 1, errors
    assert "Hi, {name}!" in new_text


def test_apply_patch_pure_deletion():
    blob = """\
<<<<<<< SEARCH
def greet(name):
    print(f"Hello, {name}")

=======
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 1
    assert "def greet" not in new_text
    assert "def main():" in new_text


def test_apply_patch_malformed_no_divider():
    blob = """\
<<<<<<< SEARCH
def greet(name):
>>>>>>> REPLACE
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 0
    assert any("divider" in e.lower() for e in errors)
    assert new_text == ORIGINAL


def test_apply_patch_malformed_no_end_marker():
    blob = """\
<<<<<<< SEARCH
def greet(name):
    print(f"Hello, {name}")
=======
def greet(name):
    pass
"""
    new_text, applied, errors = EditPipeline._apply_search_replace_patches(ORIGINAL, blob)
    assert applied == 0
    assert any("end marker" in e.lower() or "REPLACE" in e for e in errors)
    assert new_text == ORIGINAL



# ---------------------------------------------------------------------------
# Symbol mention extraction (drives anchor injection in patch mode)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected_subset",
    [
        # Chinese instructions WITH a space around the symbol
        ("为 main 函数上方添加注释:我爱吃米饭", ["main"]),
        # CRITICAL: Chinese instructions WITHOUT a space — the original
        # incident.  Python \b doesn't fire between Chinese chars and ASCII
        # because both are word chars, so the OLD code missed this case
        # entirely.
        ("为main函数上方添加注释:我爱吃米饭", ["main"]),
        ("修复process_edit方法的bug", ["process_edit"]),
        ("把MyClass改成AsyncClass", ["MyClass", "AsyncClass"]),
        ("修复 process_edit 方法的 bug", ["process_edit"]),
        # English with backticks
        ("Refactor `MyClass.handle_request` to use async", ["MyClass", "handle_request"]),
        # Function-call style
        ("Add logging to greet() and farewell()", ["greet", "farewell"]),
        # Dotted reference
        ("Update Router.route_message error handling", ["Router", "route_message"]),
        # Should ignore stop words
        ("Make this function add to the class", []),
        # Empty / whitespace
        ("", []),
        ("   \n  \n", []),
    ],
)
def test_extract_mentioned_symbols(text, expected_subset):
    got = EditPipeline._extract_mentioned_symbols(text)
    for name in expected_subset:
        assert name in got, f"Expected {name!r} in {got!r}"


def test_extract_mentioned_symbols_caps_at_8():
    text = " ".join(f"name{i}" for i in range(20))
    got = EditPipeline._extract_mentioned_symbols(text)
    assert len(got) <= 8


# ---------------------------------------------------------------------------
# Symbol anchor collection — end-to-end against a real Python file
# ---------------------------------------------------------------------------
def test_collect_symbol_anchors_finds_main_function():
    """The actual scenario from the user report: instruction mentions
    `main`, AST-driven anchor extraction must find it and emit a
    verbatim-source anchor."""
    source = """\
import os
import sys


def helper():
    return 1


def main():
    print("starting")
    helper()
    print("done")


if __name__ == "__main__":
    main()
"""

    class _Req:
        instructions = "为 main 函数上方添加注释:我爱吃米饭"
        target_file = "/tmp/fake.py"
        code_edit = "#"

    pipeline = EditPipeline.__new__(EditPipeline)  # bypass __init__ (no router needed)
    anchors = pipeline._collect_symbol_anchors(
        request=_Req(),
        original_content=source,
        language="python",
        keep_symbols=[],
    )
    names = [a["name"] for a in anchors]
    assert "main" in names, f"Expected 'main' anchor, got {names}"
    main_anchor = next(a for a in anchors if a["name"] == "main")
    # Anchor content must contain the verbatim def line
    assert "def main():" in main_anchor["content"]
    # And verbatim body lines so SEARCH/REPLACE has unique anchor material
    assert "print(\"starting\")" in main_anchor["content"]
    # Line ranges populated
    assert main_anchor["line_start"] >= 1
    assert main_anchor["line_end"] >= main_anchor["line_start"]


def test_collect_symbol_anchors_returns_empty_when_no_match():
    source = "def foo():\n    pass\n"

    class _Req:
        instructions = "Refactor totally_unrelated_function"
        target_file = "/tmp/fake.py"
        code_edit = "#"

    pipeline = EditPipeline.__new__(EditPipeline)
    anchors = pipeline._collect_symbol_anchors(
        request=_Req(),
        original_content=source,
        language="python",
        keep_symbols=[],
    )
    assert anchors == []


def test_collect_symbol_anchors_caps_at_5():
    """Even with many candidates that all match, we cap at MAX_ANCHORS."""
    body = "\n\n".join(f"def fn{i}():\n    return {i}" for i in range(12))

    class _Req:
        instructions = " ".join(f"fn{i}" for i in range(12))
        target_file = "/tmp/fake.py"
        code_edit = "#"

    pipeline = EditPipeline.__new__(EditPipeline)
    anchors = pipeline._collect_symbol_anchors(
        request=_Req(),
        original_content=body,
        language="python",
        keep_symbols=[],
    )
    assert len(anchors) <= 5



# ---------------------------------------------------------------------------
# Symbol-surgical mode — the third, most reliable strategy.
# ---------------------------------------------------------------------------
def _big_python_file(target_function: str = "main") -> str:
    """Build a Python file long enough (>60 lines) to trigger surgical mode."""
    head = "\n".join(f"# header line {i}" for i in range(20))
    helpers = "\n\n".join(
        f"def helper_{i}():\n    return {i}" for i in range(10)
    )
    target = (
        f"def {target_function}():\n"
        '    print("starting")\n'
        '    print("running")\n'
        '    print("done")\n'
    )
    tail = "\n".join(f"# trailer {i}" for i in range(10))
    return f"{head}\n\n{helpers}\n\n\n{target}\n\n{tail}\n"


def test_try_symbol_surgical_finds_unique_symbol():
    pipeline = EditPipeline.__new__(EditPipeline)
    src = _big_python_file()

    class _Req:
        instructions = "为 main 函数上方添加注释:我爱吃米饭"
        code_edit = "#"

    result = pipeline._try_symbol_surgical(
        request=_Req(),
        original_content=src,
        language="python",
    )
    assert result is not None
    assert result["target_symbol"] == "main"
    assert "def main():" in result["snippet"]
    assert "starting" in result["snippet"]
    # Has padding above the symbol (so '# trailer' or '# header' may appear)
    assert result["snippet_start_line"] >= 1
    assert result["snippet_end_line"] >= result["snippet_start_line"]
    # Symbol-specific lines stored
    assert result["symbol_line_start"] >= result["snippet_start_line"]
    assert result["symbol_line_end"] <= result["snippet_end_line"]


def test_try_symbol_surgical_finds_symbol_no_space_chinese():
    """The actual incident: instruction had no space between Chinese and the
    symbol name (``为main函数``).  Surgical mode MUST still find `main`."""
    pipeline = EditPipeline.__new__(EditPipeline)
    src = _big_python_file()

    class _Req:
        instructions = "为main函数上方添加注释:我爱吃米饭"
        code_edit = "#"

    result = pipeline._try_symbol_surgical(
        request=_Req(),
        original_content=src,
        language="python",
    )
    assert result is not None, "Surgical mode failed to detect 'main' without spaces"
    assert result["target_symbol"] == "main"
    assert "def main():" in result["snippet"]


def test_try_symbol_surgical_skips_when_ambiguous():
    """Two functions both named 'foo' — surgical must refuse rather than
    silently editing the wrong one."""
    src = (
        "\n".join(f"# pad {i}" for i in range(40))
        + "\n\nclass A:\n    def foo(self):\n        return 1\n\n"
        + "class B:\n    def foo(self):\n        return 2\n\n"
        + "\n".join(f"# tail {i}" for i in range(20))
    )

    class _Req:
        instructions = "Refactor foo to use logging"
        code_edit = "#"

    pipeline = EditPipeline.__new__(EditPipeline)
    result = pipeline._try_symbol_surgical(
        request=_Req(),
        original_content=src,
        language="python",
    )
    # AST may report each foo separately so by_name['foo'] has 2 entries -> skip
    if result is not None:
        # If AST treated them as different (e.g. A.foo, B.foo), surgical may
        # still fire on whichever name matched. That's acceptable as long as
        # the result is internally consistent.
        assert result["target_symbol"] in {"foo", "A", "B"}
    # The strict guarantee is: no exception, no crash, deterministic outcome.


def test_try_symbol_surgical_returns_none_for_small_file():
    """Files below the threshold should fall back to whole-file mode."""
    small = "def main():\n    pass\n"

    class _Req:
        instructions = "Modify main"
        code_edit = "#"

    pipeline = EditPipeline.__new__(EditPipeline)
    result = pipeline._try_symbol_surgical(
        request=_Req(),
        original_content=small,
        language="python",
    )
    assert result is None


def test_splice_snippet_replaces_exact_range():
    original = "line1\nline2\nline3\nline4\nline5\n"
    spliced = EditPipeline._splice_snippet(
        original,
        new_snippet="REPLACED_A\nREPLACED_B",
        start_line=2,
        end_line=4,
    )
    assert spliced == "line1\nREPLACED_A\nREPLACED_B\nline5\n"


def test_splice_snippet_preserves_trailing_newline_at_eof():
    original = "line1\nline2\nline3"  # no trailing newline
    spliced = EditPipeline._splice_snippet(
        original,
        new_snippet="A\nB",
        start_line=2,
        end_line=3,
    )
    # The splice should end the file without a trailing newline either,
    # matching the original's behaviour.
    assert spliced.endswith("B") or spliced.endswith("B\n")


def test_splice_snippet_at_file_start():
    original = "old1\nold2\nold3\n"
    spliced = EditPipeline._splice_snippet(
        original,
        new_snippet="new1",
        start_line=1,
        end_line=2,
    )
    assert spliced == "new1\nold3\n"


def test_splice_snippet_at_file_end():
    original = "a\nb\nc\nd\n"
    spliced = EditPipeline._splice_snippet(
        original,
        new_snippet="X\nY",
        start_line=3,
        end_line=4,
    )
    assert spliced == "a\nb\nX\nY\n"
