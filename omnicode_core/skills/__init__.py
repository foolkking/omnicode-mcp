"""OmniCode Skills — packaged workflow recipes.

A "skill" is a small declarative YAML/JSON manifest that bundles:

  * a name + description (how the AI editor finds it)
  * a list of trigger keywords (when to auto-suggest the skill)
  * a workflow: an ordered list of MCP tool calls with templated args

The skill loader is purely *advisory* — it never runs the workflow on
its own. The MCP tool ``omni_skill`` (registered in
``omnicode_adapters/mcp_server/skills_tools.py``) lets the AI editor
ask "list available skills" or "show me the steps for skill X". The
editor then decides whether to follow the recipe.

This is the simplest implementation that respects the architecture:
no automatic execution, no privileged code paths, just structured
documentation that an AI can read.

First-party skills live in ``omnicode_core/skills/builtin/``; users
can drop additional skills under ``<workspace>/.kiro/skills/`` or
``~/.kiro/skills/``.
"""

from omnicode_core.skills.loader import (
    Skill,
    SkillLoader,
    SkillNotFoundError,
    get_skill_loader,
)

__all__ = ["Skill", "SkillLoader", "SkillNotFoundError", "get_skill_loader"]
