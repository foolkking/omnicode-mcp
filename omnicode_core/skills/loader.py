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
from typing import Any, Dict, List, Optional

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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "keywords": self.keywords,
            "steps": self.steps,
            "inputs": self.inputs,
            "source": self.source,
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
                import yaml  # noqa: PLC0415 — optional dep
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

    def search(self, query: str, max_results: int = 10) -> List[Skill]:
        """Return skills matching ``query`` against name/description/keywords.

        Simple substring match — fast, predictable, no dependencies. The
        AI editor can do its own ranking on the returned set.
        """
        q = query.lower().strip()
        if not q:
            return self.list_skills()[:max_results]
        out: List[Skill] = []
        for skill in self.list_skills():
            haystacks = (
                skill.name.lower(),
                skill.description.lower(),
                " ".join(k.lower() for k in skill.keywords),
            )
            if any(q in h for h in haystacks):
                out.append(skill)
            if len(out) >= max_results:
                break
        return out


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
