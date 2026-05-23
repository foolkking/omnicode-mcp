"""
Issue / PR Linker (STAGE 5.5)
=============================
Pulls structured Issue / PR references out of commit messages, branch names,
and (optionally) the GitHub API.

The local-only path is always available and never makes network calls. When
the user supplies a ``GITHUB_TOKEN`` and the working directory is a checkout
of a GitHub repository, the linker upgrades each issue with:

* state (open / closed)
* title
* author
* labels
* URL

If the network is unavailable the local references are returned untouched,
so this module is safe to call from anywhere — including offline boxes.

Recognised patterns
-------------------
* GitHub style:   ``#1234`` · ``GH-1234`` · ``fixes #42`` · ``closes GH-7``
* JIRA style:     ``ABC-123`` · ``XY-9999``
* Azure DevOps:   ``AB#1234``
* GitLab style:   ``!1234`` (merge request) · ``%5`` (milestone)

The output is always a list of :class:`IssueReference` regardless of whether
the GitHub API was contacted; callers can decide how to render them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("github_pr",  re.compile(r"\b(?:PR|pull request)\s*[#-]?(\d{1,7})\b", re.IGNORECASE)),
    ("github",     re.compile(r"(?<!\w)#(\d{1,7})\b")),
    ("github_alt", re.compile(r"\bGH-(\d{1,7})\b")),
    ("gitlab_mr",  re.compile(r"(?<!\w)!(\d{1,7})\b")),
    ("ado",        re.compile(r"\bAB#(\d{1,7})\b")),
    # JIRA-style identifiers — exclude common non-issue prefixes (PR, GH, AB)
    # and require the project to be at least 2 alphabetic characters.
    ("jira",       re.compile(
        r"\b(?!(?:PR|GH|AB|MR|CI|CD)\b)([A-Z][A-Z0-9]{1,9})-(\d{1,7})\b"
    )),
)

# Verbs that imply an issue will close once this commit lands.
_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)

_HTTP_TIMEOUT_S = 4.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class IssueReference:
    """A single reference extracted from a commit message / branch name."""

    raw: str               # exact text matched, e.g. "fixes #42"
    kind: str              # github / github_pr / jira / ado / gitlab_mr
    identifier: str        # canonical id, e.g. "#42" or "ABC-123"
    project: Optional[str] = None  # for JIRA-style: ABC
    number: Optional[int] = None   # numeric portion if applicable
    closing: bool = False  # the verb implies the issue closes
    source_commit: Optional[str] = None  # short commit hash if known
    state: Optional[str] = None      # populated by GitHub enrich (open/closed)
    title: Optional[str] = None
    author: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "identifier": self.identifier,
            "project": self.project,
            "number": self.number,
            "closing": self.closing,
            "source_commit": self.source_commit,
            "state": self.state,
            "title": self.title,
            "author": self.author,
            "labels": list(self.labels),
            "url": self.url,
        }


# ---------------------------------------------------------------------------
# Linker
# ---------------------------------------------------------------------------
class IssueLinker:
    """Extracts Issue/PR references from commits and (optionally) enriches them."""

    def __init__(
        self,
        working_dir: str,
        github_token: Optional[str] = None,
        enable_network: bool = True,
        max_commits: int = 50,
    ) -> None:
        self.working_dir = os.path.abspath(working_dir)
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN") or None
        self.enable_network = enable_network
        self.max_commits = max(1, int(max_commits))
        self._owner_repo: Optional[Tuple[str, str]] = None  # cached parse of `git remote`

    # ------------------------------------------------------------------ extract
    @staticmethod
    def extract(text: str, source_commit: Optional[str] = None) -> List[IssueReference]:
        """Pull all references out of a single message / string."""
        if not text:
            return []
        seen: set = set()
        refs: List[IssueReference] = []

        for kind, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                raw = match.group(0)
                if kind == "jira":
                    project = match.group(1)
                    number = int(match.group(2))
                    identifier = f"{project}-{number}"
                else:
                    project = None
                    number = int(match.group(1))
                    if kind == "github":
                        identifier = f"#{number}"
                    elif kind == "github_alt":
                        identifier = f"GH-{number}"
                    elif kind == "github_pr":
                        identifier = f"PR-{number}"
                    elif kind == "gitlab_mr":
                        identifier = f"!{number}"
                    elif kind == "ado":
                        identifier = f"AB#{number}"
                    else:
                        identifier = raw

                key = (kind, identifier)
                if key in seen:
                    continue
                seen.add(key)

                # Determine if this is a closing reference: look at the 24
                # characters of context immediately preceding the match.
                ctx_start = max(0, match.start() - 24)
                ctx = text[ctx_start: match.start()].lower()
                closing = any(verb in ctx for verb in _CLOSING_KEYWORDS)

                refs.append(
                    IssueReference(
                        raw=raw,
                        kind=kind,
                        identifier=identifier,
                        project=project,
                        number=number,
                        closing=closing,
                        source_commit=source_commit,
                    )
                )
        return refs

    # ------------------------------------------------------------------ git helpers
    def _detect_owner_repo(self) -> Optional[Tuple[str, str]]:
        """Parse ``git remote get-url origin`` into (owner, repo) for GitHub."""
        if self._owner_repo is not None:
            return self._owner_repo
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception:
            return None
        if res.returncode != 0:
            return None
        url = res.stdout.strip()
        # Supported: git@github.com:owner/repo.git, https://github.com/owner/repo[.git]
        m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
        if not m:
            m = re.match(
                r"https?://(?:[^/]*@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/)?$",
                url,
            )
        if m:
            self._owner_repo = (m.group(1), m.group(2))
            return self._owner_repo
        return None

    def _list_commits(self, file_path: Optional[str] = None) -> List[Dict[str, str]]:
        """Return the most recent commits as ``{hash, message}`` dicts."""
        SEP = "<<<KIRO_LINKER_SEP>>>"
        END = "<<<KIRO_LINKER_END>>>"
        fmt = f"%H{SEP}%B{END}"
        cmd = [
            "git",
            "log",
            f"-n{self.max_commits}",
            f"--format={fmt}",
        ]
        if file_path:
            cmd.extend(["--", file_path])
        try:
            res = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except Exception:
            return []
        if res.returncode != 0:
            return []
        commits: List[Dict[str, str]] = []
        for raw in res.stdout.split(END):
            raw = raw.strip("\n")
            if not raw:
                continue
            parts = raw.split(SEP, 1)
            if len(parts) != 2:
                continue
            commits.append({"hash": parts[0].strip(), "message": parts[1].strip()})
        return commits

    # ------------------------------------------------------------------ public
    def extract_from_repo(
        self,
        file_path: Optional[str] = None,
        enrich: bool = True,
    ) -> List[IssueReference]:
        """Scan the latest N commits and return all references found."""
        commits = self._list_commits(file_path=file_path)
        all_refs: List[IssueReference] = []
        seen: set = set()
        for c in commits:
            short = c["hash"][:8] if c["hash"] else None
            for ref in self.extract(c["message"], source_commit=short):
                key = (ref.kind, ref.identifier)
                if key in seen:
                    continue
                seen.add(key)
                all_refs.append(ref)
        if enrich and self.enable_network:
            self.enrich_with_github(all_refs)
        return all_refs

    def enrich_with_github(self, refs: Iterable[IssueReference]) -> List[IssueReference]:
        """If we have a GitHub token + GitHub remote, fetch issue/PR metadata."""
        if not self.enable_network:
            return list(refs)
        token = self.github_token
        if not token:
            return list(refs)
        owner_repo = self._detect_owner_repo()
        if owner_repo is None:
            return list(refs)
        owner, repo = owner_repo

        try:
            import json
            import urllib.error
            import urllib.request
        except Exception:  # pragma: no cover
            return list(refs)

        out: List[IssueReference] = []
        stop_calling = False
        for ref in refs:
            out.append(ref)
            if (
                stop_calling
                or ref.kind not in ("github", "github_alt", "github_pr")
                or not ref.number
            ):
                continue
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{ref.number}"
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "OmniCode-MCP",
            }
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                    body = resp.read()
                    data = json.loads(body.decode("utf-8", errors="replace"))
                ref.state = data.get("state")
                ref.title = data.get("title")
                user = data.get("user") or {}
                ref.author = user.get("login")
                ref.labels = [
                    (lbl.get("name") or "")
                    for lbl in (data.get("labels") or [])
                    if isinstance(lbl, dict)
                ]
                ref.url = data.get("html_url")
            except urllib.error.HTTPError as exc:
                logger.debug("GitHub enrich failed for %s: %s", ref.identifier, exc)
                # Auth failures will repeat on every issue — stop trying but
                # still pass through remaining refs un-enriched.
                if exc.code in (401, 403):
                    self.enable_network = False
                    stop_calling = True
            except Exception as exc:  # network blocked, etc.
                logger.debug("GitHub enrich error for %s: %s", ref.identifier, exc)
                # Stop trying once one call fails — likely offline.
                self.enable_network = False
                stop_calling = True
        return out


__all__ = ["IssueLinker", "IssueReference"]
