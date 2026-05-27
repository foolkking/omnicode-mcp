"""
Feature flags for OmniCode-MCP.

Controls which capabilities are active at runtime. Features can be toggled
via environment variables, .env file, or a [features] section in the config.

Core features (always available):
  - index, search, ast, graph, read, diagnostics, edit (patch ops)

Optional features (require extra dependencies or configuration):
  - lsp: requires language servers installed (pyright, tsserver, ...)
  - memory: requires sentence-transformers
  - llm_router: requires litellm + provider API keys
  - ai_edit: requires llm_router
  - web_console: requires templates/ directory
  - safe_edit: patch preview/validate/apply/rollback (always on by default)
"""

import os
from dataclasses import dataclass, field


@dataclass
class FeatureFlags:
    """Runtime feature toggles."""

    # Core (always on)
    index: bool = True
    search: bool = True
    ast: bool = True
    graph: bool = True
    read: bool = True
    diagnostics: bool = True
    safe_edit: bool = True

    # Optional
    lsp: bool = field(default_factory=lambda: os.environ.get("OMNICODE_LSP", "true").lower() == "true")
    memory: bool = field(default_factory=lambda: os.environ.get("OMNICODE_MEMORY", "true").lower() == "true")
    web_console: bool = field(default_factory=lambda: os.environ.get("OMNICODE_WEB_CONSOLE", "true").lower() == "true")

    # LLM (off by default in headless mode)
    llm_router: bool = field(default_factory=lambda: os.environ.get("OMNICODE_LLM_ROUTER", "true").lower() == "true")
    ai_edit: bool = field(default_factory=lambda: os.environ.get("OMNICODE_AI_EDIT", "true").lower() == "true")

    def disable_llm(self) -> None:
        """Disable all LLM-dependent features."""
        self.llm_router = False
        self.ai_edit = False

    def headless(self) -> None:
        """Configure for headless/API-only mode."""
        self.web_console = False

    def minimal(self) -> None:
        """Minimal mode — only core capabilities, no LLM, no web."""
        self.disable_llm()
        self.headless()
        self.memory = False
        self.lsp = False


# Singleton
_features: FeatureFlags | None = None


def get_features() -> FeatureFlags:
    """Get the global feature flags instance."""
    global _features
    if _features is None:
        _features = FeatureFlags()
    return _features


def reset_features() -> None:
    """Reset feature flags (for testing)."""
    global _features
    _features = None
