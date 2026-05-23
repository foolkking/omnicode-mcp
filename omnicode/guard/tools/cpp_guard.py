"""
C / C++ guard (STAGE 6.4)
=========================
Runs cppcheck against a single C/C++ file and parses its XML output into
structured :class:`GuardIssue` instances.

Why cppcheck?
    *  Works without compiling — no need for project-wide ``compile_commands.json``.
    *  Detects the bugs that hurt most: null-pointer dereference, memory
       leaks, uninitialized variables, signed/unsigned mismatches, dead code.
    *  Ships as a single static binary on every major platform.

Output format
-------------
We invoke cppcheck with ``--xml --xml-version=2 --enable=warning,style,performance,portability,information``
which produces a deterministic, easy-to-parse XML stream regardless of the
cppcheck version.  Severity is mapped from cppcheck's own taxonomy
(``error``, ``warning``, ``style``, ``performance``, ``portability``,
``information``) to our :class:`IssueSeverity` enum.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from ..models import GuardIssue, GuardResult, IssueSeverity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------
async def _run(
    cmd: List[str], cwd: Optional[str] = None, timeout: float = 60.0
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
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------
_CPPCHECK_SEVERITY = {
    "error":         IssueSeverity.ERROR,
    "warning":       IssueSeverity.WARNING,
    "style":         IssueSeverity.WARNING,
    "performance":   IssueSeverity.WARNING,
    "portability":   IssueSeverity.WARNING,
    "information":   IssueSeverity.INFO,
    "debug":         IssueSeverity.HINT,
    "none":          IssueSeverity.INFO,
}


def _parse_cppcheck_xml(
    xml_text: str, target_file: str
) -> List[GuardIssue]:
    """Parse cppcheck XML 2 output into a list of GuardIssue."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.debug("cppcheck XML parse failed: %s", exc)
        return []

    issues: List[GuardIssue] = []
    target_norm = os.path.normcase(os.path.abspath(target_file))

    # cppcheck XML 2 layout:
    # <results version="2">
    #   <cppcheck version="..."/>
    #   <errors>
    #     <error id="..." severity="..." msg="..." verbose="...">
    #       <location file="..." line="..." column="..."/>
    #     </error>
    #   </errors>
    # </results>
    errors_node = root.find("errors")
    if errors_node is None:
        return []

    for err in errors_node.findall("error"):
        sev_str = (err.get("severity") or "warning").lower()
        severity = _CPPCHECK_SEVERITY.get(sev_str, IssueSeverity.WARNING)
        rule_id = err.get("id") or "cppcheck"
        msg = err.get("msg") or err.get("verbose") or "(no message)"

        # cppcheck can attach multiple <location> nodes for multi-line issues;
        # we take the FIRST one as the primary location and ignore the rest.
        locations = err.findall("location")
        if not locations:
            continue
        primary = locations[0]
        loc_file = primary.get("file") or ""
        line_str = primary.get("line") or "0"
        col_str  = primary.get("column") or "0"

        try:
            line = int(line_str)
        except ValueError:
            line = 0
        try:
            column = int(col_str)
        except ValueError:
            column = 0

        # Limit to the file the user asked about — cppcheck may follow
        # #include directives and emit issues from other headers, which we
        # do not want surfaced as if they were in the target file.
        try:
            loc_norm = os.path.normcase(os.path.abspath(loc_file)) if loc_file else ""
        except Exception:
            loc_norm = ""
        if loc_norm and loc_norm != target_norm:
            continue

        issues.append(
            GuardIssue(
                tool="cppcheck",
                code=rule_id,
                severity=severity,
                message=msg,
                line=line if line > 0 else None,
                column=column if column > 0 else None,
                file_path=loc_file or target_file,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------
async def run_cppcheck(file_path: str) -> Tuple[List[GuardIssue], bool]:
    """Run cppcheck on a single file. Returns ``(issues, ran_successfully)``."""
    if not _has_tool("cppcheck"):
        return [], False

    cwd = os.path.dirname(os.path.abspath(file_path)) or "."
    cmd = [
        "cppcheck",
        "--enable=warning,style,performance,portability,information",
        "--inconclusive",
        "--quiet",
        "--xml",
        "--xml-version=2",
        # Treat the file as the language indicated by its extension.
        "--language=" + ("c" if file_path.lower().endswith(".c") else "c++"),
        os.path.basename(file_path),
    ]
    code, _stdout, stderr = await _run(cmd, cwd=cwd, timeout=90.0)
    # cppcheck writes XML to stderr by default!  This is intentional in their
    # CLI — stdout is reserved for "non-error progress info" only.  So we
    # parse the XML out of stderr.
    issues = _parse_cppcheck_xml(stderr, os.path.abspath(file_path))
    # Even returncode != 0 just means findings exist; only 124 / 127 are
    # "we couldn't run it at all".
    if code in (124, 127) and not issues:
        return [], False
    return issues, True


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------
class CPPGuard:
    """Static analysis for C and C++ source files."""

    EXTS = (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx", ".cu")

    @staticmethod
    async def check_async(file_path: str) -> GuardResult:
        issues, ran = await run_cppcheck(file_path)
        tools_run: List[str] = []
        tools_skipped: List[str] = []
        if ran:
            tools_run.append("cppcheck")
        else:
            tools_skipped.append("cppcheck")

        errors_text = "\n".join(
            i.format() for i in issues if i.severity == IssueSeverity.ERROR
        )
        warnings_text = "\n".join(
            i.format() for i in issues if i.severity == IssueSeverity.WARNING
        )
        if not tools_run:
            warnings_text = (
                "C/C++ guard requires cppcheck. Install it via your package "
                "manager (e.g. `choco install cppcheck`, `brew install cppcheck`, "
                "or `apt-get install cppcheck`).\n" + warnings_text
            )

        is_clean = (
            not any(i.severity == IssueSeverity.ERROR for i in issues)
            and bool(tools_run)
        )
        return GuardResult(
            is_clean=is_clean,
            errors=errors_text,
            warnings=warnings_text,
            issues=issues,
            tools_run=tools_run,
            tools_skipped=tools_skipped,
        )


# Backwards-compat alias so callers using the more PEP-8-flavoured name
# still work.  Both names refer to the exact same class.
CppGuard = CPPGuard


__all__ = ["CPPGuard", "CppGuard", "run_cppcheck"]
