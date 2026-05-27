"""STAGE 11.6 — Unit tests for the Proactive Guard layer.

Covers:
  - GuardResult / GuardIssue formatting
  - PythonGuard parsing of injected ruff JSON / mypy text / bandit JSON
  - JSGuard graceful degradation when neither tool is installed
  - ProactiveGuard.check dispatches to the right backend per file extension
"""

from __future__ import annotations

import pytest

from omnicode.guard.analyzer import ProactiveGuard
from omnicode.guard.models import GuardIssue, GuardResult, IssueSeverity
from omnicode.guard.tools import python_guard as py_module
from omnicode.guard.tools.js_guard import JSGuard


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class TestGuardModels:
    def test_format_includes_location_and_code(self):
        issue = GuardIssue(
            tool="ruff",
            code="E501",
            severity=IssueSeverity.ERROR,
            message="line too long",
            line=42,
            column=80,
            file_path="x.py",
        )
        s = issue.format()
        assert "ruff:42:80" in s
        assert "[E501]" in s
        assert "line too long" in s

    def test_summary_counts_per_severity(self):
        result = GuardResult(
            issues=[
                GuardIssue(tool="ruff", severity=IssueSeverity.ERROR,   message="a"),
                GuardIssue(tool="ruff", severity=IssueSeverity.ERROR,   message="b"),
                GuardIssue(tool="mypy", severity=IssueSeverity.WARNING, message="c"),
                GuardIssue(tool="mypy", severity=IssueSeverity.INFO,    message="d"),
            ],
            tools_run=["ruff", "mypy"],
        )
        assert result.error_count == 2
        assert result.warning_count == 1
        assert "2 errors" in result.summary()
        assert "1 warnings" in result.summary()


# ---------------------------------------------------------------------------
# PythonGuard parsing helpers — we don't actually run ruff/mypy/bandit
# ---------------------------------------------------------------------------
class TestPythonGuardParsing:
    """Tests that exercise the guard's PARSING logic in isolation."""

    @pytest.mark.asyncio
    async def test_ruff_parses_json_output(self, monkeypatch, tmp_path):
        # Force ruff "available" but stub the subprocess to return controlled JSON
        ruff_payload = """[
            {
                "code": "E501",
                "message": "line too long",
                "location": {"row": 10, "column": 80},
                "filename": "x.py"
            },
            {
                "code": "F401",
                "message": "unused import",
                "location": {"row": 1, "column": 1},
                "filename": "x.py"
            }
        ]"""

        async def fake_run(cmd, cwd=None, timeout=30.0):
            return 1, ruff_payload, ""

        monkeypatch.setattr(py_module, "_run", fake_run)
        monkeypatch.setattr(py_module, "_has_tool", lambda name: name == "ruff")

        issues, ran = await py_module.run_ruff("x.py")
        assert ran is True
        assert len(issues) == 2
        codes = {i.code for i in issues}
        assert codes == {"E501", "F401"}
        # E-class severity should be ERROR; F-class also ERROR per the level map
        assert all(i.severity == IssueSeverity.ERROR for i in issues)
        # Line/column round-trip
        e501 = next(i for i in issues if i.code == "E501")
        assert e501.line == 10 and e501.column == 80

    @pytest.mark.asyncio
    async def test_ruff_skipped_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(py_module, "_has_tool", lambda name: False)
        issues, ran = await py_module.run_ruff("x.py")
        assert ran is False
        assert issues == []


# ---------------------------------------------------------------------------
# JSGuard
# ---------------------------------------------------------------------------
class TestJSGuard:
    @pytest.mark.asyncio
    async def test_no_tools_installed_reports_skipped(self, tmp_path, monkeypatch):
        target = tmp_path / "sample.ts"
        target.write_text("export const x: number = 1;\n")

        # Force tool resolution to "nothing found" no matter what.
        from omnicode.guard.tools import js_guard as js_module
        monkeypatch.setattr(
            js_module,
            "_resolve_tool_with_fallback",
            lambda name, cwd: (None, False),
        )

        result = await JSGuard.check_async(str(target))
        assert result.is_clean is False  # we couldn't verify
        # Both eslint and tsc should appear in skipped because the file is .ts
        assert "eslint" in result.tools_skipped
        assert "tsc" in result.tools_skipped
        assert "Install via" in result.warnings or "node_modules" in result.warnings.lower() or "npx" in result.warnings.lower()

    @pytest.mark.asyncio
    async def test_eslint_only_for_js_files(self, tmp_path, monkeypatch):
        """Plain .js files should not invoke tsc at all."""
        target = tmp_path / "sample.js"
        target.write_text("module.exports = 1;\n")

        from omnicode.guard.tools import js_guard as js_module
        monkeypatch.setattr(
            js_module,
            "_resolve_tool_with_fallback",
            lambda name, cwd: (None, False),
        )

        result = await JSGuard.check_async(str(target))
        # tsc must NOT be reported as run or skipped for .js files
        assert "tsc" not in result.tools_run
        assert "tsc" not in result.tools_skipped
        assert "eslint" in result.tools_skipped


# ---------------------------------------------------------------------------
# ProactiveGuard dispatch
# ---------------------------------------------------------------------------
class TestProactiveGuard:
    @pytest.mark.asyncio
    async def test_python_dispatches_to_python_guard(self, tmp_path, monkeypatch):
        target = tmp_path / "x.py"
        target.write_text("x = 1\n")

        called = {"py": False}

        async def fake_python_check(file_path):
            called["py"] = True
            return GuardResult(is_clean=True, tools_run=["ruff"])

        monkeypatch.setattr(
            "omnicode.guard.analyzer.PythonGuard.check_async", staticmethod(fake_python_check)
        )

        guard = ProactiveGuard()
        result = await guard.check(str(target))
        assert called["py"] is True
        assert result.tools_run == ["ruff"]

    @pytest.mark.asyncio
    async def test_typescript_dispatches_to_js_guard(self, tmp_path, monkeypatch):
        target = tmp_path / "x.ts"
        target.write_text("export const x = 1\n")

        called = {"js": False}

        async def fake_js_check(file_path):
            called["js"] = True
            return GuardResult(is_clean=True, tools_run=["eslint"])

        monkeypatch.setattr(
            "omnicode.guard.analyzer.JSGuard.check_async", staticmethod(fake_js_check)
        )

        guard = ProactiveGuard()
        result = await guard.check(str(target))
        assert called["js"] is True
        assert result.tools_run == ["eslint"]

    @pytest.mark.asyncio
    async def test_unknown_extension_no_op(self, tmp_path):
        target = tmp_path / "data.xyz"
        target.write_text("???")
        guard = ProactiveGuard()
        result = await guard.check(str(target))
        assert result.is_clean is True
        # The no_op path adds a single INFO-severity issue
        assert any(i.severity == IssueSeverity.INFO for i in result.issues)



# ---------------------------------------------------------------------------
# CppGuard (STAGE 6.4)
# ---------------------------------------------------------------------------
class TestCppGuardParsing:
    """Test cppcheck XML parsing in isolation."""

    def test_parses_well_formed_xml(self, tmp_path):
        target = tmp_path / "x.c"
        target.write_text("int main() { return 0; }\n")

        from omnicode.guard.tools.cpp_guard import _parse_cppcheck_xml

        # Realistic cppcheck XML 2 output
        xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
        <results version="2">
          <cppcheck version="2.7"/>
          <errors>
            <error id="memleak" severity="error" msg="Memory leak: p" verbose="...">
              <location file="{target.name}" line="10" column="5"/>
            </error>
            <error id="uninitvar" severity="warning" msg="Uninitialized variable: x">
              <location file="{target.name}" line="15" column="9"/>
            </error>
            <error id="unusedFunction" severity="style" msg="The function 'foo' is never used">
              <location file="{target.name}" line="20" column="1"/>
            </error>
          </errors>
        </results>
        """
        # cppcheck emits relative paths in `file=`, so we run from the file's
        # parent directory. Mimic that here.
        # Patch xml.file to absolute so the filter accepts it
        xml_text = xml_text.replace(
            f'file="{target.name}"', f'file="{str(target).replace(chr(92), "/")}"'
        )
        issues = _parse_cppcheck_xml(xml_text, str(target))
        assert len(issues) == 3
        codes = {i.code for i in issues}
        assert codes == {"memleak", "uninitvar", "unusedFunction"}
        memleak = next(i for i in issues if i.code == "memleak")
        assert memleak.severity == IssueSeverity.ERROR
        assert memleak.line == 10
        # 'style' should map to WARNING
        unused = next(i for i in issues if i.code == "unusedFunction")
        assert unused.severity == IssueSeverity.WARNING

    def test_filters_out_other_files(self, tmp_path):
        """Issues reported in headers cppcheck followed should be dropped."""
        target = tmp_path / "x.c"
        target.write_text("int x;\n")
        other = tmp_path / "other.h"
        other.write_text("int y;\n")

        from omnicode.guard.tools.cpp_guard import _parse_cppcheck_xml

        xml_text = f"""<?xml version="1.0"?>
        <results version="2">
          <errors>
            <error id="a" severity="warning" msg="in target">
              <location file="{str(target).replace(chr(92), "/")}" line="1" column="1"/>
            </error>
            <error id="b" severity="warning" msg="in other header">
              <location file="{str(other).replace(chr(92), "/")}" line="1" column="1"/>
            </error>
          </errors>
        </results>"""
        issues = _parse_cppcheck_xml(xml_text, str(target))
        # We must keep only the issue whose location matches target
        assert len(issues) == 1
        assert issues[0].code == "a"

    def test_handles_malformed_xml(self):
        from omnicode.guard.tools.cpp_guard import _parse_cppcheck_xml

        assert _parse_cppcheck_xml("not <real /> xml at all", "/tmp/x.c") == []
        assert _parse_cppcheck_xml("", "/tmp/x.c") == []

    @pytest.mark.asyncio
    async def test_run_cppcheck_returns_skipped_when_missing(self, monkeypatch):
        """When cppcheck isn't installed, run_cppcheck returns ([], False)."""
        from omnicode.guard.tools import cpp_guard as cpp_module

        monkeypatch.setattr(cpp_module, "_has_tool", lambda name: False)
        issues, ran = await cpp_module.run_cppcheck("/tmp/whatever.c")
        assert ran is False
        assert issues == []

    @pytest.mark.asyncio
    async def test_check_async_friendly_when_no_tools(self, tmp_path, monkeypatch):
        """CppGuard.check_async should produce a usable GuardResult even
        when cppcheck isn't installed."""
        from omnicode.guard.tools import cpp_guard as cpp_module
        from omnicode.guard.tools.cpp_guard import CppGuard

        target = tmp_path / "x.cpp"
        target.write_text("int main() { return 0; }\n")
        monkeypatch.setattr(cpp_module, "_has_tool", lambda name: False)

        result = await CppGuard.check_async(str(target))
        assert "cppcheck" in result.tools_skipped
        assert "Install" in result.warnings or "cppcheck" in result.warnings.lower()
        assert result.is_clean is False
