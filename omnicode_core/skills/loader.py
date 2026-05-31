"""Discover and load OmniCode skills.

Skill manifest format (JSON or YAML)::

    {
      "name": "omni-impact-review",
      "description": "Pull the impact + risk + advisory bundle for a symbol.",
      "version": "1.0.0",
      "keywords": ["impact", "blast radius", "before edit"],
      "steps": [
        {
          "tool": "omni_impact",
          "args": {"symbol": "${symbol}"},
          "explain": "Get callers/callees and risk badge"
        },
        {
          "tool": "omni_memory",
          "args": {"action": "advisory", "query": "${symbol}"},
          "explain": "Surface past lessons related to the symbol"
        }
      ],
      "inputs": [
        {"name": "symbol", "required": true, "description": "Symbol to analyse"}
      ]
    }

Skills are pure documentation: the loader returns the parsed manifest
and the MCP tool ``omni_skill`` shows it to the AI. The AI editor is
responsible for interpolating ``${...}`` placeholders and invoking
the tools — the OmniCode server never executes a skill on the
user's behalf, which keeps the security model simple.

Discovery order (lowest precedence first; later entries override):

  1. ``omnicode_core/skills/builtin/``    — first-party skills
  2. ``~/.kiro/skills/``                  — user-level skills
  3. ``<workspace>/.kiro/skills/``        — project-level skills
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SkillNotFoundError(LookupError):
    """Raised when ``get_skill(name)`` can't find a skill by that name."""


@dataclass
class Skill:
    name: str
    description: str
    version: str = "1.0.0"
    keywords: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    inputs: List[Dict[str, Any]] = field(default_factory=list)
    source: str = ""  # absolute path the manifest came from
    when_to_use: str = ""
    tools_used: List[str] = field(default_factory=list)
    does_execute: bool = False
    safety_notes: List[str] = field(default_factory=list)
    # ---- audit-bundle.r8 / recipes 2.1 fields --------------------------
    # success_criteria: bullets a caller can use to verify the recipe ran
    # to completion. Optional; recipes that pre-date r8 simply report [].
    success_criteria: List[str] = field(default_factory=list)
    # next_actions: short list of "what to do once the recipe completes"
    # — surfaced on show / list responses so AI editors don't dead-end.
    next_actions: List[str] = field(default_factory=list)
    # recipe_for_handler_features: cross-reference into _HANDLER_FEATURES
    # so omni_status (or a future drift detector) can spot stale recipes.
    recipe_for_handler_features: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "keywords": self.keywords,
            "steps": self.steps,
            "inputs": self.inputs,
            "source": self.source,
            "when_to_use": self.when_to_use,
            "tools_used": self.tools_used,
            "does_execute": self.does_execute,
            "safety_notes": self.safety_notes,
            "success_criteria": self.success_criteria,
            "next_actions": self.next_actions,
            "recipe_for_handler_features": self.recipe_for_handler_features,
        }


class SkillLoader:
    """Discovers and caches skills from the three search paths.

    Refreshes on every ``list_skills()`` call (cheap — typically <30
    files), so dropping a new skill into ``~/.kiro/skills/`` is
    visible without restarting the server.
    """

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self.workspace_root = Path(workspace_root) if workspace_root else None

    # ---- Discovery ------------------------------------------------------
    def _search_paths(self) -> List[Path]:
        paths: List[Path] = []
        # 1. Built-in
        builtin = Path(__file__).parent / "builtin"
        if builtin.exists():
            paths.append(builtin)
        # 2. User-level
        user = Path.home() / ".kiro" / "skills"
        if user.exists():
            paths.append(user)
        # 3. Workspace-level
        if self.workspace_root is not None:
            ws = self.workspace_root / ".kiro" / "skills"
            if ws.exists():
                paths.append(ws)
        return paths

    def _iter_manifests(self) -> List[Path]:
        out: List[Path] = []
        for base in self._search_paths():
            for ext in ("*.json", "*.yaml", "*.yml"):
                out.extend(sorted(base.glob(ext)))
        return out

    @staticmethod
    def _parse(path: Path) -> Optional[Skill]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Skill manifest unreadable %s: %s", path, exc)
            return None
        # Try JSON first (no extra deps); if it looks YAML and PyYAML is
        # installed, fall through to it.
        data: Optional[dict] = None
        if path.suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning("Skill JSON parse failed (%s): %s", path, exc)
                return None
        else:
            try:
                import yaml  # type: ignore[import-untyped]  # optional dep, only on YAML manifests
                data = yaml.safe_load(text)
            except ImportError:
                logger.debug(
                    "Skill %s is YAML but pyyaml not installed; skipping. "
                    "Install with `pip install pyyaml`.", path,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skill YAML parse failed (%s): %s", path, exc)
                return None
        if not isinstance(data, dict):
            return None
        try:
            return Skill(
                name=str(data["name"]),
                description=str(data.get("description", "")),
                version=str(data.get("version", "1.0.0")),
                keywords=list(data.get("keywords") or []),
                steps=list(data.get("steps") or []),
                inputs=list(data.get("inputs") or []),
                source=str(path),
                when_to_use=str(data.get("when_to_use") or ""),
                tools_used=list(data.get("tools_used") or []),
                does_execute=bool(data.get("does_execute", False)),
                safety_notes=list(data.get("safety_notes") or []),
                success_criteria=list(data.get("success_criteria") or []),
                next_actions=list(data.get("next_actions") or []),
                recipe_for_handler_features=list(
                    data.get("recipe_for_handler_features") or []
                ),
            )
        except KeyError as exc:
            logger.warning(
                "Skill manifest %s missing required field: %s", path, exc,
            )
            return None

    # ---- Public API -----------------------------------------------------
    def list_skills(self) -> List[Skill]:
        """Return all discovered skills (later sources override earlier)."""
        seen: Dict[str, Skill] = {}
        for path in self._iter_manifests():
            skill = self._parse(path)
            if skill is None:
                continue
            seen[skill.name] = skill  # later wins
        return list(seen.values())

    def get_skill(self, name: str) -> Skill:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        raise SkillNotFoundError(name)

    def search(
        self, query: str, max_results: int = 10
    ) -> List[Tuple["Skill", int, List[str]]]:
        """Return ``[(skill, score, why_matched), …]`` for ``query``.

        Uses the same tokenisation and scoring shape as
        ``discover_tools._recommend_tools``: tokenise the query (Latin
        words AND CJK single characters), drop a small bilingual stop-
        word set, score each skill by how many of its keywords / name
        tokens / description tokens appear in the query, and surface
        the matched tags in ``why_matched``.

        Falls back to the full skill list (with score 0) so the caller
        never goes home empty-handed.
        """
        from omnicode_adapters.mcp_server.high_level_tools import (
            _DISCOVER_STOPWORDS,
            _DISCOVER_TOKEN_RE,
        )

        skills = self.list_skills()
        if not query or not query.strip():
            return [(s, 0, []) for s in skills[:max_results]]

        q_lower = query.lower()
        raw_tokens = _DISCOVER_TOKEN_RE.findall(q_lower)
        tokens = {
            t for t in raw_tokens
            if t not in _DISCOVER_STOPWORDS and len(t) >= 1
        }

        scored: List[Tuple[Skill, int, List[str]]] = []
        for skill in skills:
            score = 0
            why: List[str] = []

            # Exact-name match dominates everything else.
            if skill.name.lower() in q_lower or q_lower in skill.name.lower():
                score += 10
                why.append(f"name:{skill.name}")

            # Tokens of the skill name itself.
            name_tokens = {
                t for t in _DISCOVER_TOKEN_RE.findall(skill.name.lower())
                if t not in _DISCOVER_STOPWORDS and len(t) >= 2
            }
            n_hits = name_tokens & tokens
            if n_hits:
                score += 4 * len(n_hits)
                why.extend(f"name_token:{h}" for h in sorted(n_hits)[:3])

            # Keyword hits — bilingual.
            for kw in skill.keywords:
                kw_lower = kw.lower().strip()
                if not kw_lower:
                    continue
                # Latin keyword: token containment.
                if any(c.isalpha() and ord(c) < 128 for c in kw_lower):
                    if kw_lower in tokens or kw_lower in q_lower:
                        score += 3
                        why.append(f"keyword:{kw_lower}")
                else:
                    # CJK / mixed: substring match on the raw query.
                    if kw_lower in q_lower:
                        score += 3
                        why.append(f"keyword:{kw_lower}")

            # Description tokens (lower weight).
            desc_tokens = {
                t for t in _DISCOVER_TOKEN_RE.findall(skill.description.lower())
                if t not in _DISCOVER_STOPWORDS and len(t) >= 3
            }
            d_hits = desc_tokens & tokens
            if d_hits:
                score += 1 * len(d_hits)
                why.extend(f"desc:{h}" for h in sorted(d_hits)[:2])

            # ``when_to_use`` carries strong signal too.
            if skill.when_to_use:
                wtu_tokens = {
                    t for t in _DISCOVER_TOKEN_RE.findall(skill.when_to_use.lower())
                    if t not in _DISCOVER_STOPWORDS and len(t) >= 3
                }
                w_hits = wtu_tokens & tokens
                if w_hits:
                    score += 2 * len(w_hits)
                    why.extend(f"when:{h}" for h in sorted(w_hits)[:2])

            if score > 0:
                scored.append((skill, score, why))

        scored.sort(key=lambda x: x[1], reverse=True)
        if not scored:
            # Fallback: zero-match returns the full default list so the AI
            # never sees a dead end.
            return [(s, 0, ["fallback:default"]) for s in skills[:max_results]]
        return scored[:max_results]


_DEFAULT: Optional[SkillLoader] = None


def get_skill_loader(workspace_root: Optional[str] = None) -> SkillLoader:
    """Process-wide skill loader. First call sets the workspace root."""
    global _DEFAULT
    if _DEFAULT is None or workspace_root is not None:
        # Resolve workspace_root from settings when not provided so the
        # singleton finds <wd>/.kiro/skills automatically.
        if workspace_root is None:
            workspace_root = os.environ.get("WORKING_DIR") or os.getcwd()
        _DEFAULT = SkillLoader(workspace_root=workspace_root)
    return _DEFAULT


__all__ = ["Skill", "SkillLoader", "SkillNotFoundError", "get_skill_loader"]
