"""
JS / TS guard (STAGE 6.3)
=========================
Runs ESLint and (for ``.ts*`` files) the TypeScript compiler in parallel and
returns a structured :class:`GuardResult`.

Tool resolution is a little more involved than for Python because Node-based
CLIs have several common installation locations:

    1.  ``node_modules/.bin``  — local install (preferred).
    2.  global ``$PATH``        — `npm install -g` or `volta`.
    3.  ``npx``                 — last-resort fallback that downloads on demand.

We try each in order and only mark a tool as *run* when a real binary
actually existed (i.e. we never let `npx` go to the network without the user
opting in via ``OMNICODE_NPX_FALLBACK=1``).

ESLint output is parsed via ``--format=json``; ``tsc --noEmit`` runs in
``--pretty=false`` mode so we can scrape the canonical
``path(line,col): error TSxxxx: message`` lines with a simple regex.
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
# Subprocess + tool resolution helpers
# ---------------------------------------------------------------------------
async def _run(
    cmd: List[str], cwd: Optional[str] = None, timeout: float = 45.0
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


def _resolve_tool(name: str, cwd: str) -> Optional[List[str]]:
    """Return the command-line prefix that should be used to invoke ``name``.

    Resolution order:
        1.  ``<cwd>/node_modules/.bin/<name>[.cmd|.ps1]`` — local install
        2.  global ``$PATH``
    Returns ``None`` if no suitable binary is found.
    """
    # 1. local node_modules — walk up parents looking for a project root
    cur = os.path.abspath(cwd)
    while True:
        candidate_dir = os.path.join(cur, "node_modules", ".bin")
        if os.path.isdir(candidate_dir):
            for ext in ("", ".cmd", ".ps1", ".bat"):
                candidate = os.path.join(candidate_dir, name + ext)
                if os.path.isfile(candidate):
                    return [candidate]
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 2. global PATH
    found = shutil.which(name)
    if found:
        return [found]
    return None


def _resolve_tool_with_fallback(name: str, cwd: str) -> Tuple[Optional[List[str]], bool]:
    """Return (command_prefix, used_npx) — npx only when env-flag is set."""
    direct = _resolve_tool(name, cwd)
    if direct is not None:
        return direct, False
    if os.environ.get("OMNICODE_NPX_FALLBACK") == "1":
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "--", name], True
    return None, False


# ---------------------------------------------------------------------------
# ESLint
# ---------------------------------------------------------------------------
_ESLINT_SEVERITY = {
    0: IssueSeverity.INFO,    # off (shouldn't appear)
    1: IssueSeverity.WARNING, # warn
    2: IssueSeverity.ERROR,   # error
}


async def run_eslint(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run ESLint with JSON output. Returns (issues, ran_successfully)."""
    cwd = os.path.dirname(os.path.abspath(file_path)) or "."
    cmd_prefix, _used_npx = _resolve_tool_with_fallback("eslint", cwd)
    if cmd_prefix is None:
        return [], False

    cmd = list(cmd_prefix) + ["--format=json", "--no-color", file_path]
    code, stdout, stderr = await _run(cmd, cwd=cwd, timeout=60)

    # ESLint exits 0 (no problems), 1 (problems found) or 2 (config error).
    # In all cases the JSON should still be on stdout.
    if not stdout.strip():
        if code in (0, 1):
            return [], True
        logger.debug("eslint produced no JSON output (code=%s, stderr=%s)", code, stderr[:200])
        return [], False

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.debug("eslint JSON parse failed: %s", exc)
        return [], False

    issues: List[GuardIssue] = []
    for file_report in payload:
        for msg in file_report.get("messages", []) or []:
            sev = _ESLINT_SEVERITY.get(int(msg.get("severity", 1)), IssueSeverity.WARNING)
            rule_id = msg.get("ruleId")
            issues.append(
                GuardIssue(
                    tool="eslint",
                    code=str(rule_id) if rule_id else None,
                    severity=sev,
                    message=str(msg.get("message", "")),
                    line=int(msg.get("line") or 0) or None,
                    column=int(msg.get("column") or 0) or None,
                    file_path=file_report.get("filePath") or file_path,
                )
            )
    return issues, True


# ---------------------------------------------------------------------------
# TypeScript compiler
# ---------------------------------------------------------------------------
# tsc canonical output:
#   src/foo.ts(12,5): error TS2322: Type 'X' is not assignable to type 'Y'.
_TSC_LINE_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<sev>error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>.+)$",
    re.IGNORECASE,
)
_TSC_SEVERITY = {
    "error":   IssueSeverity.ERROR,
    "warning": IssueSeverity.WARNING,
}


async def run_tsc(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run ``tsc --noEmit`` on a single file. Returns (issues, ran_successfully).

    The compiler is a project-wide tool so we cannot easily isolate a single
    file — we run it in the discovered tsconfig project root and then filter
    the diagnostics down to the file the user asked about.
    """
    abs_target = os.path.abspath(file_path)
    cwd = _find_project_root(abs_target)
    cmd_prefix, _used_npx = _resolve_tool_with_fallback("tsc", cwd)
    if cmd_prefix is None:
        return [], False

    # Project mode if a tsconfig exists; otherwise single-file mode.
    has_tsconfig = os.path.isfile(os.path.join(cwd, "tsconfig.json"))
    cmd = list(cmd_prefix) + ["--noEmit", "--pretty", "false"]
    if has_tsconfig:
        cmd.extend(["--project", cwd])
    else:
        cmd.append(abs_target)

    code, stdout, stderr = await _run(cmd, cwd=cwd, timeout=90)
    combined = (stdout or "") + ("\n" + stderr if stderr else "")
    if not combined.strip() and code in (0, 1):
        return [], True

    issues: List[GuardIssue] = []
    target_norm = os.path.normcase(abs_target)
    for line in combined.splitlines():
        m = _TSC_LINE_RE.match(line.strip())
        if not m:
            continue
        # Only report issues for the file we were asked about.
        reported_path = os.path.normcase(
            os.path.abspath(os.path.join(cwd, m.group("file")))
        )
        if reported_path != target_norm:
            continue
        issues.append(
            GuardIssue(
                tool="tsc",
                code=m.group("code"),
                severity=_TSC_SEVERITY.get(m.group("sev").lower(), IssueSeverity.ERROR),
                message=m.group("msg").strip(),
                line=int(m.group("line")),
                column=int(m.group("col")),
                file_path=abs_target,
            )
        )
    return issues, True


def _find_project_root(start_path: str) -> str:
    """Walk up from ``start_path`` until a tsconfig.json or package.json is found."""
    cur = os.path.dirname(start_path) if os.path.isfile(start_path) else start_path
    while True:
        if (
            os.path.isfile(os.path.join(cur, "tsconfig.json"))
            or os.path.isfile(os.path.join(cur, "package.json"))
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.dirname(start_path) or os.getcwd()
        cur = parent


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------
class JSGuard:
    """Static analysis for JavaScript and TypeScript files."""

    JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
    TS_EXTS = (".ts", ".tsx", ".mts", ".cts")

    @staticmethod
    async def check_async(file_path: str) -> GuardResult:
        ext = os.path.splitext(file_path)[1].lower()
        is_ts = ext in JSGuard.TS_EXTS
        # Run ESLint always (covers JS and TS); run tsc for TS files only.
        eslint_task = asyncio.create_task(run_eslint(file_path))
        tsc_task: Optional[asyncio.Task] = None
        if is_ts:
            tsc_task = asyncio.create_task(run_tsc(file_path))

        eslint_issues, eslint_ran = await eslint_task
        tsc_issues: List[GuardIssue] = []
        tsc_ran = False
        if tsc_task is not None:
            tsc_issues, tsc_ran = await tsc_task

        all_issues: List[GuardIssue] = list(eslint_issues) + list(tsc_issues)
        tools_run: List[str] = []
        tools_skipped: List[str] = []
        for name, ran in (("eslint", eslint_ran), ("tsc", tsc_ran or not is_ts)):
            # tsc is only relevant for TS — skip it (without listing it) for JS.
            if name == "tsc" and not is_ts:
                continue
            if ran:
                tools_run.append(name)
            else:
                tools_skipped.append(name)

        errors_text = "\n".join(
            i.format() for i in all_issues if i.severity == IssueSeverity.ERROR
        )
        warnings_text = "\n".join(
            i.format() for i in all_issues if i.severity == IssueSeverity.WARNING
        )
        if not tools_run and tools_skipped:
            warnings_text = (
                f"No JS/TS guards installed (tried: {', '.join(tools_skipped)}). "
                "Install via `npm install --save-dev eslint typescript` or set "
                "OMNICODE_NPX_FALLBACK=1 to allow on-demand installs.\n" + warnings_text
            )

        is_clean = (
            not any(i.severity == IssueSeverity.ERROR for i in all_issues)
            and bool(tools_run)
        )
        return GuardResult(
            is_clean=is_clean,
            errors=errors_text,
            warnings=warnings_text,
            issues=all_issues,
            tools_run=tools_run,
            tools_skipped=tools_skipped,
        )


__all__ = ["JSGuard", "run_eslint", "run_tsc"]
