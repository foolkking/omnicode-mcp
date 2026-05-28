"""Line-level text search across the workspace.

This is the backend behind ``mode=text`` in the MCP ``omni_search``
tool. The previous implementation cheated by ``LIKE %query%`` over
SQLite chunks and then claimed every match was on line 1 — useless for
AI editors that actually want to *go look*.

This module does the obvious thing: walk the workspace, read each file
that matches the pattern, scan it line by line, and return real
``(file, line_no, content, context_before, context_after)`` records.

Usage:
    from omnicode_core.search.text_grep import grep_workspace

    hits = grep_workspace(
        workspace_root="/path/to/repo",
        query="OMNICODE_READ_ONLY",
        file_patterns=["*.py", "*.md"],
        max_results=50,
        context_lines=2,
    )

Design notes:

* ``file_patterns`` is a list of glob fragments matched against the
  basename. ``"*.py"`` means "any Python file". Pass ``["*"]`` to scan
  every file. Common binary / vendor directories are pruned regardless.
* Compiled regex search when ``use_regex=True``, otherwise plain
  substring.
* Output is small dataclasses, not Pydantic models, so this module can
  be reused by anything in core without dragging the API layer in.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# Directories we never want to scan, even if they're on disk and
# match a glob. Anything inside these paths is silently skipped at the
# directory-walk level so we don't even open the file.
_PRUNE_DIRNAMES = frozenset(
    [
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".data",
        "dist",
        "build",
        "_keep_",
        ".idea",
        ".vscode",
    ]
)

# Files we never want to open even when the extension globs match.
# Mostly the artefacts that look like text but are actually huge/noisy.
_SKIP_FILE_SUFFIXES = frozenset(
    [
        ".min.js",
        ".min.css",
        ".map",
        ".lock",
        ".log",
    ]
)

# Hard cap on a single file's size in bytes when grepping. Bigger
# files are skipped to keep latency bounded — if you need to search a
# 50 MB log you should be using `rg` directly anyway.
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB

_DEFAULT_PATTERNS: Tuple[str, ...] = (
    "*.py",
    "*.js",
    "*.jsx",
    "*.ts",
    "*.tsx",
    "*.go",
    "*.rs",
    "*.java",
    "*.cpp",
    "*.cc",
    "*.c",
    "*.h",
    "*.hpp",
    "*.rb",
    "*.php",
    "*.kt",
    "*.cs",
    "*.md",
    "*.toml",
    "*.yaml",
    "*.yml",
    "*.json",
    "*.html",
    "*.css",
    "*.sh",
    "*.bat",
)


@dataclass
class GrepHit:
    """A single line-level text-search hit."""

    file_path: str  # workspace-relative
    line_number: int  # 1-indexed
    line_content: str
    context_before: List[str] = field(default_factory=list)
    context_after: List[str] = field(default_factory=list)
    match_span: Tuple[int, int] = (0, 0)  # (start_col, end_col), 0-indexed

    def as_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "line_number": self.line_number,
            "line_content": self.line_content,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "match_span": list(self.match_span),
            "merged_lines": list(getattr(self, "_merged_lines", []) or []),
        }


def _compile_query(query: str, use_regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    """Compile the user query into a single regex.

    Plain-text mode escapes the query so that meta characters don't
    blow up the search. Regex mode passes the pattern through.
    """
    if not use_regex:
        query = re.escape(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(query, flags)


def _normalise_patterns(patterns: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """Pick a sensible default if the caller didn't pass globs.

    Special-cases ``"*"`` and ``["*"]`` to mean "scan everything"
    (still subject to the prune list).
    """
    if not patterns:
        return _DEFAULT_PATTERNS
    if isinstance(patterns, str):
        patterns = (patterns,)
    cleaned = tuple(p for p in patterns if p)
    if not cleaned:
        return _DEFAULT_PATTERNS
    if cleaned == ("*",) or cleaned == ("**",):
        return ("*",)
    return cleaned


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _iter_candidate_files(
    workspace_root: Path, patterns: Sequence[str]
) -> Iterable[Path]:
    """Walk ``workspace_root`` and yield every file matching ``patterns``.

    Prunes the directories in ``_PRUNE_DIRNAMES`` aggressively so we
    don't even descend into them.
    """
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        # Prune in-place — os.walk respects the mutation.
        dirnames[:] = [
            d for d in dirnames
            if d not in _PRUNE_DIRNAMES and not d.startswith(".")
            or d in {".kiro", ".github"}  # explicitly allow these
        ]

        for fname in filenames:
            # Skip hidden files except a couple of common useful ones.
            if fname.startswith(".") and fname not in {".env.example", ".gitignore"}:
                continue

            # Skip known noisy suffixes.
            lname = fname.lower()
            if any(lname.endswith(suf) for suf in _SKIP_FILE_SUFFIXES):
                continue

            if not _matches_any(fname, patterns):
                continue

            full = Path(dirpath) / fname
            try:
                if full.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield full


def _scan_file(
    path: Path,
    pattern: re.Pattern[str],
    workspace_root: Path,
    context_lines: int,
    remaining: int,
) -> List[GrepHit]:
    """Scan one file for matches; return up to ``remaining`` hits."""
    try:
        # Read with utf-8 first; fall back to a byte-tolerant read so we
        # don't crash on the occasional latin-1 / mixed file.
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return []

    if not text:
        return []

    lines = text.splitlines()
    hits: List[GrepHit] = []
    rel = path.relative_to(workspace_root).as_posix()

    for idx, line in enumerate(lines):
        if remaining <= 0:
            break
        m = pattern.search(line)
        if not m:
            continue
        before = lines[max(0, idx - context_lines): idx]
        after = lines[idx + 1: idx + 1 + context_lines]
        hits.append(
            GrepHit(
                file_path=rel,
                line_number=idx + 1,  # 1-indexed for humans
                line_content=line,
                context_before=before,
                context_after=after,
                match_span=(m.start(), m.end()),
            )
        )
        remaining -= 1

    return hits


def grep_workspace(
    workspace_root: str | os.PathLike,
    query: str,
    file_patterns: Optional[Sequence[str]] = None,
    max_results: int = 50,
    context_lines: int = 2,
    use_regex: bool = False,
    case_sensitive: bool = False,
    merge_adjacent: bool = True,
) -> List[GrepHit]:
    """Run a line-level text search over the workspace.

    Parameters
    ----------
    workspace_root:
        Absolute path to the workspace root. Caller is expected to have
        already validated this against the sandbox.
    query:
        The string (or regex if ``use_regex=True``) to search for.
    file_patterns:
        Glob fragments matched against file basename. Defaults to a
        sensible source-code-only list. Pass ``("*",)`` to scan
        everything.
    max_results:
        Hard cap on hits across the whole workspace. Once we hit this we
        stop scanning new files.
    context_lines:
        Number of lines of context to include before and after each hit.
        Set to 0 to disable.
    use_regex:
        Treat ``query`` as a regex instead of a plain substring.
    case_sensitive:
        Default behaviour is case-insensitive (matches grep -i).
    merge_adjacent:
        When two hits are within ``2 * context_lines + 1`` lines of each
        other in the same file, merge them into a single hit covering
        both. Mirrors ripgrep's default grouping behaviour. Set False to
        keep every match as a separate row.
    """
    if not query.strip():
        return []

    root = Path(workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        return []

    patterns = _normalise_patterns(file_patterns)
    pattern = _compile_query(query, use_regex=use_regex, case_sensitive=case_sensitive)

    out: List[GrepHit] = []
    remaining = max(1, int(max_results))
    for path in _iter_candidate_files(root, patterns):
        if remaining <= 0:
            break
        hits = _scan_file(
            path,
            pattern,
            root,
            context_lines=max(0, int(context_lines)),
            remaining=remaining,
        )
        if hits:
            if merge_adjacent and context_lines > 0:
                hits = _merge_adjacent_hits(hits, context_lines)
            out.extend(hits)
            remaining -= len(hits)

    return out


def _merge_adjacent_hits(hits: List[GrepHit], context_lines: int) -> List[GrepHit]:
    """Merge hits whose context windows overlap.

    Two hits in the same file at lines ``a < b`` are merged when
    ``b - a <= 2 * context_lines + 1``. The merged record:

    * keeps the earliest line as ``line_number`` (the "anchor"),
    * appends every additional matched line after the anchor as part of
      ``context_after`` (so the rendered snippet still shows them),
    * keeps the original ``match_span`` of the first hit (downstream
      renderers are expected to only show the anchor's column markers
      anyway).

    The implementation is O(n) and assumes hits arrive in line order
    (which ``_scan_file`` produces).
    """
    if not hits:
        return hits

    merged: List[GrepHit] = []
    for h in hits:
        if not merged or merged[-1].file_path != h.file_path:
            merged.append(h)
            continue

        prev = merged[-1]
        # Range of lines the previous record's context window covers.
        prev_window_end = prev.line_number + len(prev.context_after)
        gap = h.line_number - prev_window_end

        if gap <= context_lines:
            # Overlap or touching — extend prev.context_after so it now
            # spans through h.line_number + its context_after, while
            # de-duplicating overlap.
            prev_anchor = prev.line_number
            covered_through = max(
                prev_anchor + len(prev.context_after),
                h.line_number + len(h.context_after),
            )
            new_after: List[str] = []
            # Walk every line index after the anchor up to ``covered_through``.
            # We rebuild from h's content so the new lines are included.
            # ``h.context_before`` covers lines (h.line_number - len(h.context_before)) .. (h.line_number - 1)
            # ``h.line_content`` is line h.line_number
            # ``h.context_after`` covers lines (h.line_number + 1) .. (h.line_number + len(h.context_after))
            # ``prev.context_after`` covers (prev_anchor + 1) .. (prev_anchor + len(prev.context_after))
            for offset in range(1, covered_through - prev_anchor + 1):
                line_no = prev_anchor + offset
                if line_no <= prev_anchor + len(prev.context_after):
                    new_after.append(prev.context_after[line_no - prev_anchor - 1])
                elif line_no == h.line_number:
                    new_after.append(h.line_content)
                elif line_no - h.line_number - 1 < len(h.context_after):
                    new_after.append(h.context_after[line_no - h.line_number - 1])
                else:
                    # Gap we don't have content for — pad with empty line.
                    new_after.append("")
            prev.context_after = new_after
            # Track the merged extra match line numbers in why_matched-style
            # piggyback: use the dataclass's ``__dict__`` since we don't
            # want to expand the public schema for this micro-feature.
            extra = prev.__dict__.setdefault("_merged_lines", [])
            extra.append(h.line_number)
        else:
            merged.append(h)

    return merged


__all__ = ["GrepHit", "grep_workspace"]
