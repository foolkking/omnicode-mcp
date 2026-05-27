"""
OmniCode Core — Codebase Intelligence Layer
=============================================

The core package contains all code-understanding and code-operation logic.
It has **no dependency** on:
  - Web UI (templates, static files, Tailwind)
  - Specific LLM providers (LiteLLM, OpenAI, Anthropic)
  - MCP protocol details (FastMCP, stdio)

Adapters (omnicode_adapters/) and LLM enhancements (omnicode_llm/) import
from this package, never the other way around.

Subpackages
-----------
- index      — Incremental file tracking + AST chunking + embedding
- search     — Hybrid recall (vector + symbol + text + git + LSP) + reranker
- ast        — Tree-sitter multi-language parsing + outline generation
- lsp        — Language Server Protocol bridge (pyright, tsserver, gopls, ...)
- graph      — Call graph, inheritance graph, impact analysis
- memory     — Persistent memory store + hybrid search + auto advisory
- edit       — Safe patch operations (preview / validate / apply / rollback)
- diagnostics — Static analysis gate (ruff, eslint, mypy, cppcheck)
- read       — Multi-mode file reading (full / outline / symbols / chunks / ...)
- config     — Settings, feature flags, path resolution
"""

__version__ = "2.0.0-dev"
