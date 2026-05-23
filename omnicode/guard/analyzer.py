"""
Proactive Guard analyzer (STAGE 6.1 + 6.2 integration)
======================================================
Routes a file to the right language guard and returns a unified
:class:`GuardResult`.  Languages without a dedicated guard return a benign
"no-op" result so the upstream pipelines never fail because of missing tools.
"""

from __future__ import annotations

import logging
import os

from .models import GuardIssue, GuardResult, IssueSeverity
from .tools.cpp_guard import CPPGuard
from .tools.js_guard import JSGuard
from .tools.python_guard import PythonGuard

logger = logging.getLogger(__name__)


class ProactiveGuard:
    """Runs static analysis on code to provide a safety net."""

    PYTHON_EXTS = (".py", ".pyi")
    JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
    CPP_EXTS = (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx", ".cu")

    def __init__(self) -> None:
        pass

    async def check(self, file_path: str) -> GuardResult:
        """Run appropriate checks based on file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in self.PYTHON_EXTS:
            return await PythonGuard.check_async(file_path)
        if ext in self.JS_EXTS:
            return await JSGuard.check_async(file_path)
        if ext in self.CPP_EXTS:
            return await CPPGuard.check_async(file_path)
        return _no_op(file_path, f"No checks available for {file_path}")

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    async def check_python(self, file_path: str) -> GuardResult:
        """Backward-compat shim — same behaviour as :meth:`check` for .py files."""
        return await PythonGuard.check_async(file_path)


def _no_op(file_path: str, msg: str) -> GuardResult:
    return GuardResult(
        is_clean=True,
        errors="",
        warnings=msg,
        issues=[
            GuardIssue(
                tool="proactive_guard",
                severity=IssueSeverity.INFO,
                message=msg,
                file_path=file_path,
            )
        ],
        tools_run=[],
        tools_skipped=["ruff", "mypy", "bandit"],
    )


__all__ = ["ProactiveGuard", "GuardResult", "GuardIssue", "IssueSeverity"]
