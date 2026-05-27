"""
Git Commit History Analyzer (STAGE 5.4)
=======================================
Analyses a file's git history to surface signals that help the LLM avoid
breaking carefully-crafted patches:

* **Change frequency** — how often the file is touched.
* **Defensive patches** — commits whose messages indicate bug fixes /
  workarounds / edge-case handling (these introduce code that LOOKS
  unnecessary but is critical).
* **Co-changed files** — files that historically change in the same commits
  (suggests tight coupling).
* **Risk score** — a single 0–1 number combining the above factors so the
  caller can decide how cautiously to edit.

This analyzer prefers GitPython for parsing because it gives us proper objects
without shelling out per commit, but falls back to the ``git`` CLI when the
:class:`git.Repo` cannot be opened.
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive-pattern keyword library
# ---------------------------------------------------------------------------
DEFENSIVE_KEYWORDS: Tuple[str, ...] = (
    "fix",
    "bugfix",
    "bug fix",
    "hotfix",
    "patch",
    "workaround",
    "edge case",
    "edge-case",
    "regression",
    "rollback",
    "revert",
    "guard against",
    "defensive",
    "race condition",
    "deadlock",
    "memory leak",
    "null check",
    "off-by-one",
    "off by one",
    "crash",
    "panic",
    "segfault",
    "cve",
    "security",
    "vulnerability",
    "issue",
    "closes #",
    "fixes #",
)

# Markers that look intentional even without a fix verb
HARDENING_KEYWORDS: Tuple[str, ...] = (
    "harden",
    "protect",
    "sanitize",
    "validate",
    "escape",
    "throttle",
    "rate limit",
    "fallback",
    "backoff",
    "retry",
    "timeout",
)

ISSUE_RE = re.compile(r"(?:#|GH-|[A-Z]{2,}-)(\d+)")


# ---------------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------------
class CommitInfo(BaseModel):
    """Lightweight commit summary used in API responses."""

    hash: str
    short_hash: str
    author: str
    date: str
    message: str
    is_defensive: bool = False
    is_hardening: bool = False
    related_issues: List[str] = Field(default_factory=list)


class HistoryReport(BaseModel):
    """Aggregated risk + history report for a single file."""

    file_path: str
    total_commits: int
    unique_authors: int
    first_commit_at: Optional[str] = None
    last_commit_at: Optional[str] = None
    days_active: int = 0

    defensive_commit_count: int = 0
    hardening_commit_count: int = 0
    defensive_patches: List[CommitInfo] = Field(default_factory=list)

    co_changed_files: List[Dict[str, Any]] = Field(default_factory=list)
    risk_score: float = 0.0
    risk_level: str = "low"  # 'low' | 'medium' | 'high'
    advisory: str = ""
    related_issues: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
@dataclass
class _RawCommit:
    hash: str
    author: str
    date: datetime
    message: str
    files: List[str] = field(default_factory=list)


class GitHistoryAnalyzer:
    """Inspects a repository's git log to extract risk-aware metadata."""

    def __init__(self, working_dir: str, max_commits_scanned: int = 200) -> None:
        self.working_dir = os.path.abspath(working_dir)
        self.max_commits_scanned = max_commits_scanned
        self._git_available = self._detect_git()

    # ------------------------------------------------------------------ availability
    def _detect_git(self) -> bool:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    # ------------------------------------------------------------------ raw fetch
    def _fetch_commits(self, file_path: str, max_commits: int) -> List[_RawCommit]:
        """Run a single ``git log`` invocation and parse a delimited stream.

        We ask git for: hash, ISO date, author name, then subject + body
        terminated by a unique sentinel so multi-line bodies are captured
        cleanly.  ``--name-only`` lists every file changed in that commit.

        Layout (per commit):
            START<hash>SEP<date>SEP<author>SEP<body>END
            <file 1>
            <file 2>
            ...
        Splitting on START isolates each commit; splitting on END inside the
        chunk separates metadata+body from the file list.
        """
        if not self._git_available:
            return []
        START = "<<<KIRO_COMMIT_START>>>"
        SEP = "<<<KIRO_COMMIT_SEP>>>"
        END = "<<<KIRO_COMMIT_END>>>"
        fmt = f"{START}%H{SEP}%aI{SEP}%an{SEP}%B{END}"
        try:
            cmd = [
                "git",
                "log",
                f"-n{max_commits}",
                f"--format={fmt}",
                "--name-only",
                "--",
                file_path,
            ]
            res = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except Exception as exc:
            logger.debug("git log failed for %s: %s", file_path, exc)
            return []
        if res.returncode != 0:
            return []

        commits: List[_RawCommit] = []
        # Drop the prefix before the first START (empty in normal output)
        chunks = res.stdout.split(START)
        for raw in chunks[1:]:
            meta_part, _, files_part = raw.partition(END)
            parts = meta_part.split(SEP)
            if len(parts) < 4:
                continue
            commit_hash, date_str, author = parts[0].strip(), parts[1].strip(), parts[2].strip()
            message = parts[3].strip()
            try:
                dt = datetime.fromisoformat(date_str)
            except ValueError:
                # Older git versions emit %ai (space, not T)
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
                except Exception:
                    dt = datetime.now(timezone.utc)
            files = [ln.strip() for ln in files_part.splitlines() if ln.strip()]
            commits.append(
                _RawCommit(
                    hash=commit_hash,
                    author=author,
                    date=dt,
                    message=message,
                    files=files,
                )
            )
        return commits

    # ------------------------------------------------------------------ classification
    @staticmethod
    def _classify(message: str) -> Tuple[bool, bool]:
        msg_l = message.lower()
        is_defensive = any(kw in msg_l for kw in DEFENSIVE_KEYWORDS)
        is_hardening = any(kw in msg_l for kw in HARDENING_KEYWORDS)
        return is_defensive, is_hardening

    @staticmethod
    def _extract_issues(message: str) -> List[str]:
        return list({m.group(0) for m in ISSUE_RE.finditer(message)})

    # ------------------------------------------------------------------ public API
    def analyze_file(
        self,
        file_path: str,
        co_change_top_n: int = 10,
        defensive_top_n: int = 8,
    ) -> HistoryReport:
        """Compute a full :class:`HistoryReport` for ``file_path``."""
        rel = file_path
        # Normalise to repo-relative path
        try:
            abs_path = os.path.abspath(os.path.join(self.working_dir, file_path))
            if abs_path.startswith(self.working_dir):
                rel = os.path.relpath(abs_path, self.working_dir).replace("\\", "/")
        except Exception:
            pass

        commits = self._fetch_commits(rel, self.max_commits_scanned)

        if not commits:
            return HistoryReport(
                file_path=rel,
                total_commits=0,
                unique_authors=0,
                advisory="No git history found for this file (new file or repo unavailable).",
            )

        # Basic stats
        authors = {c.author for c in commits}
        first_commit = commits[-1]
        last_commit = commits[0]
        days_active = max(0, (last_commit.date - first_commit.date).days)

        # Defensive/hardening detection
        defensive_commits: List[CommitInfo] = []
        hardening_count = 0
        all_issues: List[str] = []
        co_changed = Counter()

        for c in commits:
            is_def, is_hard = self._classify(c.message)
            if is_hard:
                hardening_count += 1
            issues = self._extract_issues(c.message)
            all_issues.extend(issues)
            if is_def:
                defensive_commits.append(
                    CommitInfo(
                        hash=c.hash,
                        short_hash=c.hash[:8],
                        author=c.author,
                        date=c.date.isoformat(),
                        message=c.message.splitlines()[0][:200],
                        is_defensive=True,
                        is_hardening=is_hard,
                        related_issues=issues,
                    )
                )
            for f in c.files:
                if f and f != rel:
                    co_changed[f] += 1

        defensive_count = len(defensive_commits)

        # Sort co-changed files
        top_co_changed = [
            {"file": f, "co_changes": n}
            for f, n in co_changed.most_common(co_change_top_n)
        ]

        # Risk scoring (0–1)
        risk_score, risk_level, advisory = self._risk(
            commits=commits,
            defensive_count=defensive_count,
            hardening_count=hardening_count,
            unique_authors=len(authors),
            last_commit_dt=last_commit.date,
        )

        return HistoryReport(
            file_path=rel,
            total_commits=len(commits),
            unique_authors=len(authors),
            first_commit_at=first_commit.date.isoformat(),
            last_commit_at=last_commit.date.isoformat(),
            days_active=days_active,
            defensive_commit_count=defensive_count,
            hardening_commit_count=hardening_count,
            defensive_patches=defensive_commits[:defensive_top_n],
            co_changed_files=top_co_changed,
            risk_score=round(risk_score, 4),
            risk_level=risk_level,
            advisory=advisory,
            related_issues=sorted(set(all_issues))[:25],
        )

    def get_frequently_changed_together(
        self, file_path: str, limit: int = 5
    ) -> List[str]:
        """Backward-compat helper used elsewhere in the codebase."""
        report = self.analyze_file(file_path, co_change_top_n=limit)
        return [item["file"] for item in report.co_changed_files[:limit]]

    # ------------------------------------------------------------------ scoring
    @staticmethod
    def _risk(
        commits: List[_RawCommit],
        defensive_count: int,
        hardening_count: int,
        unique_authors: int,
        last_commit_dt: datetime,
    ) -> Tuple[float, str, str]:
        n = len(commits)
        if n == 0:
            return 0.0, "low", "No history."

        # 1. Frequency: more commits = higher fragility.
        #    Scale: 50 commits ≈ saturation.
        freq_score = min(1.0, n / 50.0)

        # 2. Defensive ratio: how many of the recent commits are bug fixes?
        defensive_ratio = defensive_count / max(1, n)
        # 3. Hardening ratio
        hardening_ratio = hardening_count / max(1, n)

        # 4. Author diversity: many authors = higher coordination cost.
        author_score = min(1.0, unique_authors / 10.0)

        # 5. Recency: a file edited yesterday is hotter than one edited 2 years ago.
        try:
            now = datetime.now(timezone.utc)
            if last_commit_dt.tzinfo is None:
                last_dt = last_commit_dt.replace(tzinfo=timezone.utc)
            else:
                last_dt = last_commit_dt
            age_days = max(0, (now - last_dt).days)
        except Exception:
            age_days = 365
        # Decay with half-life of ~90 days
        recency_score = math.exp(-age_days / 90.0)

        # Weighted blend (defensive ratio dominates because that's the core signal)
        score = (
            0.30 * defensive_ratio
            + 0.20 * hardening_ratio
            + 0.20 * freq_score
            + 0.15 * author_score
            + 0.15 * recency_score
        )
        score = max(0.0, min(1.0, score))

        # Bucket
        if score >= 0.6:
            level = "high"
        elif score >= 0.3:
            level = "medium"
        else:
            level = "low"

        # Advisory
        msg_parts: List[str] = []
        if defensive_count >= 3:
            msg_parts.append(
                f"{defensive_count} defensive commits detected — handle with care."
            )
        if hardening_count >= 2:
            msg_parts.append(f"{hardening_count} hardening commits in history.")
        if n >= 25:
            msg_parts.append(f"High change frequency ({n} commits scanned).")
        if unique_authors >= 5:
            msg_parts.append(f"{unique_authors} distinct authors.")
        if not msg_parts:
            msg_parts.append("Low historical risk; should be safe to refactor.")
        advisory = " ".join(msg_parts)

        return score, level, advisory


__all__ = ["GitHistoryAnalyzer", "HistoryReport", "CommitInfo"]
