"""
Python guard (STAGE 6.2)
========================
Runs mypy, ruff and bandit in parallel against a Python file and returns a
structured :class:`GuardResult`.

Each tool is invoked with a JSON output format whenever supported, so we can
parse precise line numbers and rule codes.  Tools that aren't installed are
silently skipped and added to ``tools_skipped``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import List, Optional, Tuple

from ..models import GuardIssue, GuardResult, IssueSeverity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------
async def _run(
    cmd: List[str], cwd: Optional[str] = None, timeout: float = 30.0
) -> Tuple[int, str, str]:
    """Run a subprocess asynchronously, returning (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"timeout after {timeout}s"
        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return 127, "", "tool not installed"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Ruff parsing
# ---------------------------------------------------------------------------
_RUFF_LEVEL_MAP = {
    "E": IssueSeverity.ERROR,  # pycodestyle errors
    "F": IssueSeverity.ERROR,  # pyflakes errors
    "W": IssueSeverity.WARNING,
    "C": IssueSeverity.WARNING,
    "B": IssueSeverity.WARNING,  # bugbear
    "S": IssueSeverity.WARNING,  # bandit-rules in ruff
    "N": IssueSeverity.INFO,
    "D": IssueSeverity.INFO,
}


def _ruff_severity(code: str) -> IssueSeverity:
    if not code:
        return IssueSeverity.WARNING
    return _RUFF_LEVEL_MAP.get(code[:1].upper(), IssueSeverity.WARNING)


async def run_ruff(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run ``ruff check --output-format=json`` on a single file."""
    if not _has_tool("ruff"):
        return [], False
    code, stdout, _stderr = await _run(
        ["ruff", "check", "--output-format=json", file_path]
    )
    issues: List[GuardIssue] = []
    if code in (0, 1) and stdout.strip():
        try:
            data = json.loads(stdout)
            for entry in data:
                rule = entry.get("code") or ""
                msg = entry.get("message", "")
                location = entry.get("location") or {}
                issues.append(
                    GuardIssue(
                        tool="ruff",
                        code=rule,
                        severity=_ruff_severity(rule),
                        message=msg,
                        line=location.get("row"),
                        column=location.get("column"),
                        file_path=entry.get("filename") or file_path,
                    )
                )
        except json.JSONDecodeError:
            # Ruff fell back to text mode (older versions)
            for line in stdout.splitlines():
                m = re.match(r"^(?P<f>.+?):(?P<l>\d+):(?P<c>\d+): (?P<code>\S+) (?P<msg>.+)$", line)
                if m:
                    issues.append(
                        GuardIssue(
                            tool="ruff",
                            code=m.group("code"),
                            severity=_ruff_severity(m.group("code")),
                            message=m.group("msg"),
                            line=int(m.group("l")),
                            column=int(m.group("c")),
                            file_path=m.group("f"),
                        )
                    )
    return issues, True


# ---------------------------------------------------------------------------
# Mypy parsing
# ---------------------------------------------------------------------------
_MYPY_LEVEL_MAP = {
    "error": IssueSeverity.ERROR,
    "warning": IssueSeverity.WARNING,
    "note": IssueSeverity.INFO,
}

_MYPY_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s+(?P<lvl>error|warning|note):\s+(?P<msg>.+?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)


async def run_mypy(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run mypy with text output and parse line-by-line."""
    if not _has_tool("mypy"):
        return [], False
    code, stdout, _stderr = await _run(
        [
            "mypy",
            "--no-color-output",
            "--show-error-codes",
            "--no-error-summary",
            file_path,
        ]
    )
    issues: List[GuardIssue] = []
    if code in (0, 1, 2):
        for line in stdout.splitlines():
            m = _MYPY_LINE_RE.match(line)
            if not m:
                continue
            issues.append(
                GuardIssue(
                    tool="mypy",
                    code=m.group("code"),
                    severity=_MYPY_LEVEL_MAP.get(m.group("lvl"), IssueSeverity.WARNING),
                    message=m.group("msg"),
                    line=int(m.group("line")),
                    column=int(m.group("col")) if m.group("col") else None,
                    file_path=m.group("file"),
                )
            )
    return issues, True


# ---------------------------------------------------------------------------
# Bandit parsing
# ---------------------------------------------------------------------------
_BANDIT_LEVEL_MAP = {
    "HIGH": IssueSeverity.ERROR,
    "MEDIUM": IssueSeverity.WARNING,
    "LOW": IssueSeverity.INFO,
    "UNDEFINED": IssueSeverity.INFO,
}


async def run_bandit(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run bandit with JSON output."""
    if not _has_tool("bandit"):
        return [], False
    code, stdout, _stderr = await _run(
        ["bandit", "-q", "-f", "json", file_path]
    )
    issues: List[GuardIssue] = []
    if code in (0, 1) and stdout.strip():
        try:
            data = json.loads(stdout)
            for r in data.get("results", []):
                issues.append(
                    GuardIssue(
                        tool="bandit",
                        code=r.get("test_id"),
                        severity=_BANDIT_LEVEL_MAP.get(
                            (r.get("issue_severity") or "").upper(),
                            IssueSeverity.WARNING,
                        ),
                        message=f"{r.get('issue_text', '')} [confidence={r.get('issue_confidence')}]",
                        line=r.get("line_number"),
                        column=None,
                        file_path=r.get("filename") or file_path,
                    )
                )
        except json.JSONDecodeError:
            logger.debug("bandit returned non-JSON output")
    return issues, True


# ---------------------------------------------------------------------------
# Public façade
# ---------------------------------------------------------------------------
class PythonGuard:
    """Runs ruff + mypy + bandit in parallel and aggregates the results."""

    @staticmethod
    async def check_async(file_path: str) -> GuardResult:
        if not os.path.exists(file_path):
            return GuardResult(
                is_clean=False,
                errors=f"file not found: {file_path}",
                warnings="",
                issues=[
                    GuardIssue(
                        tool="python_guard",
                        severity=IssueSeverity.ERROR,
                        message=f"file not found: {file_path}",
                        file_path=file_path,
                    )
                ],
                tools_run=[],
                tools_skipped=["ruff", "mypy", "bandit"],
            )

        # Run all three tools in parallel for speed.
        ruff_task = asyncio.create_task(run_ruff(file_path))
        mypy_task = asyncio.create_task(run_mypy(file_path))
        bandit_task = asyncio.create_task(run_bandit(file_path))

        (ruff_issues, ruff_ran), (mypy_issues, mypy_ran), (bandit_issues, bandit_ran) = await asyncio.gather(
            ruff_task, mypy_task, bandit_task
        )

        all_issues: List[GuardIssue] = []
        all_issues.extend(ruff_issues)
        all_issues.extend(mypy_issues)
        all_issues.extend(bandit_issues)

        tools_run: List[str] = []
        tools_skipped: List[str] = []
        for name, ran in (("ruff", ruff_ran), ("mypy", mypy_ran), ("bandit", bandit_ran)):
            (tools_run if ran else tools_skipped).append(name)

        # Build legacy-compatible flat strings.
        err_lines = [i.format() for i in all_issues if i.severity == IssueSeverity.ERROR]
        warn_lines = [
            i.format()
            for i in all_issues
            if i.severity in (IssueSeverity.WARNING, IssueSeverity.INFO, IssueSeverity.HINT)
        ]

        return GuardResult(
            is_clean=len(err_lines) == 0,
            errors="\n".join(err_lines),
            warnings="\n".join(warn_lines),
            issues=all_issues,
            tools_run=tools_run,
            tools_skipped=tools_skipped,
        )

    # ------------------------------------------------------------------
    # Synchronous façade (kept for legacy callers)
    # ------------------------------------------------------------------
    @staticmethod
    def check(file_path: str) -> dict:
        """Synchronous wrapper that mirrors the legacy dict return shape."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Cannot do nested loop.run_until_complete().  Schedule a future.
            future = asyncio.run_coroutine_threadsafe(
                PythonGuard.check_async(file_path), loop
            )
            result = future.result(timeout=60)
        else:
            result = asyncio.run(PythonGuard.check_async(file_path))
        return {
            "is_clean": result.is_clean,
            "errors": result.errors,
            "warnings": result.warnings,
            "issues": [i.dict() for i in result.issues],
            "tools_run": result.tools_run,
            "tools_skipped": result.tools_skipped,
            "summary": result.summary(),
        }
