<div align="center">

# OmniCode-MCP

**A production-grade Codebase MCP server with AST-aware search, multi-LLM routing, AI-driven edit pipelines, and a full web console.**

[![Python](https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7C3AED)](https://modelcontextprotocol.io/)
[![Tree-sitter](https://img.shields.io/badge/tree--sitter-7%20languages-22C55E)](https://tree-sitter.github.io/tree-sitter/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-267%20passed-brightgreen)](#testing)

[Features](#features) ·
[Quick Start](#quick-start) ·
[Architecture](#architecture) ·
[API](#http-api) ·
[Roadmap](#roadmap) ·
[References](#references)

</div>

---

## Overview

OmniCode-MCP wraps a working directory in a single FastAPI service that exposes:

- A **semantic + symbol + text search engine** built on Tree-sitter, FAISS, and `sentence-transformers`.
- An **AI editing pipeline** with three-layer defence (whole-file / surgical / patch) so Gemini-style "thinking" output never overwrites your code.
- A **multi-provider LLM gateway** (LiteLLM) with hot-pluggable API keys, role-based selection, and best-of-N parallel routing.
- A **memory system** with hybrid keyword + cosine scoring, deduplication, and per-field match localisation.
- A **call-graph + inheritance visualiser** with D3 force-directed layout, automatic SVG-to-Canvas switch above 1500 nodes, cluster colouring, and hop-bounded scope filters.
- A **dark-mode bilingual web console** with a built-in MCP tool registry visible to Claude Desktop / VS Code / Cursor.

The project is a refactor of the original [`codebase-mcp`](https://github.com/danyQe/codebase-mcp) into a modular,
fully tested, multi-language, multi-provider implementation.

## Features

### Code intelligence

| Feature | Highlights |
|---|---|
| **AST chunking** | Tree-sitter for Python, JavaScript, TypeScript, C++, Java, Go, Rust |
| **Symbol search** | SQL `LIKE` over indexed symbol names — fuzzy + exact, scoring by name proximity |
| **Semantic search** | FAISS index persisted to disk; survives restarts; rebuilt incrementally |
| **Text search** | SQLite-backed substring scan with file-pattern filter |
| **Symbol outline** | List every named symbol in a file with `line_start`/`line_end`, signature, parent class |
| **Call graph** | Cross-file caller/callee edges; degree-based hub ranking; bounded edge cap by `max_nodes × 30` |
| **Inheritance graph** | `subclass → base`, `impl Trait for Struct`, multi-language |

### AI editing

- **Three-layer defence**: prose detector + reasoning-strip + final-shrink check, so the file is never overwritten by chain-of-thought leakage.
- **Symbol surgical mode**: when instructions reference an unambiguous symbol, only that function/class is rewritten and spliced back, preserving the rest of the file byte-for-byte.
- **`reasoning_effort=none`**: forwarded to LiteLLM so reasoning models (`gemini-2.5-flash-thinking`, `o1-*`, `claude-3.5-sonnet-thinking-*`) don't leak `<thinking>` blocks into the output.
- **Structured failure analysis**: when an edit fails, the API returns 200 with `failure_analysis.{stage, root_cause, suggested_fixes, raw_llm_excerpt}`.

### Multi-provider LLM gateway

- **Hot-pluggable providers**: register OpenAI-compatible / Anthropic / Gemini / Ollama / Azure / Bedrock from the web UI; encryption-at-rest via Fernet.
- **User-level shared registry**: API keys live in `~/.kiro/codebase-mcp/providers.db` so they're available across every project.
- **Role-based routing**: assign `default` / `quality` / `cost` / `fastest` / `edit` / `scan` / `review` / `summary` / `chat` to specific providers.
- **Best-of-N**: race the top N providers, pick the longest non-empty response.
- **Provider Test**: built-in `/providers/{name}/test` with 20 s timeout, structured `hint` + `hint_field` for UI red-border feedback.

### Memory system

- **Fingerprint deduplication**: SHA1 of `(category, normalized_content)`; repeated stores increment `access_count`.
- **Hybrid scoring**: `min_score` threshold filters out clearly irrelevant rows; combined keyword + cosine similarity rescues tag-only and filename-only matches.
- **Per-field match localisation**: every result carries a `match_fields` array indicating exactly where the query landed (`content`, `tags`, `category`, `subcategory`, `related_files`, `embedding`) with a `snippet` and `weight`.

### Web console

- **Dashboard**: 4 stat cards (files / symbols / memories / branch), live system health, recent tool-call timeline.
- **Search & Index**: three search tabs with the same code-preview modal (85vh, scrollable, jump-to-line, highlight.js syntax colouring, "Open in editor" via `vscode://file/...`).
- **File Operations**: read / write / AI-edit with three pipelines (whole-file / surgical / patch).
- **Code Graph Viewer**: D3 force-directed call graph or inheritance graph; cluster-coloured by top-level directory; `Hops` filter for "show N hops around symbol X"; cascading directory-scope picker; auto-switch to Canvas2D for graphs >1500 nodes.
- **Memory System**: store / search / edit (modal) / dedupe; results show `×N` access count and per-field match snippets.
- **Model Providers**: register, edit, test, enable/disable, reorder; role-assignment grid for 9 routing roles.
- **Working Directory**: validate / switch with native folder picker; per-project `.data/` is auto-loaded so memories and indices survive directory switches.
- **Tri-state theme**: light / dark / system; tri-state language toggle EN / 中文 with DOM-walking translator and 250+ phrase dictionary.
- **Live logs**: WebSocket-streamed, auto-backfill on connect.

### MCP tool registry

25+ tools registered via `FastMCP`. They speak stdio to Claude Desktop, VS Code, or Cursor and proxy to the FastAPI backend, so you get the full feature set inside the AI client.

## Architecture

```
              ┌───────────────────────────────────────────────┐
              │     MCP Client (Claude Desktop / Cursor /     │
              │              VS Code / Windsurf)              │
              └───────────────────────────────────────────────┘
                                   │ stdio
                                   ▼
              ┌───────────────────────────────────────────────┐
              │         mcp_server.py — FastMCP host          │
              │  25+ tools: search / read / edit / git /      │
              │  memory / providers / project / directory     │
              └───────────────────────────────────────────────┘
                                   │ HTTP / JSON
                                   ▼
              ┌───────────────────────────────────────────────┐
              │     main.py  +  api/v1/routers/*  (FastAPI)   │
              └───────────────────────────────────────────────┘
                  │             │           │             │
       ┌──────────┘             │           │             └──────────┐
       ▼                        ▼           ▼                        ▼
┌────────────────┐    ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
│ omnicode/      │    │ omnicode/llm/  │ │ omnicode/      │ │ memory_system/ │
│ ast_engine/    │    │ + provider     │ │ pipelines/     │ │ + dedupe       │
│  + chunker     │    │   registry     │ │  + edit (3-    │ │ + hybrid       │
│  + call graph  │    │ + LLM router   │ │    layer       │ │   scoring      │
│  + inheritance │    │ + LiteLLM      │ │    defence)    │ │ + per-field    │
│ omnicode/      │    │ + best-of-N    │ │  + write       │ │   localisation │
│  search/       │    │ + secret_box   │ │ + EditPipeline │ │ + auto context │
│ FAISS+SQLite   │    │   (Fernet)     │ │ + Guard        │ │   advisory     │
└────────────────┘    └────────────────┘ └────────────────┘ └────────────────┘
       │                       │                  │                 │
       └───────────────────────┴──────────────────┴─────────────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ <project>/.data/   │
                         │  vector_store.db   │  per-project semantic index
                         │  vector_store.faiss│  (persisted, restart-safe)
                         │  metadata.db       │  per-project memory store
                         └────────────────────┘
                         ┌────────────────────────────────┐
                         │ ~/.kiro/codebase-mcp/          │
                         │  providers.db / providers.key  │  USER-LEVEL keys
                         │  selections.db                 │  shared across projects
                         └────────────────────────────────┘
```

## Tech stack

- **Backend**: Python 3.11, FastAPI, Uvicorn, Pydantic 2
- **AST**: tree-sitter 0.22+ with per-language grammars
- **Search**: FAISS, sentence-transformers (`all-MiniLM-L6-v2`), SQLite
- **LLM gateway**: LiteLLM, Anthropic / OpenAI / Gemini / DeepSeek / Ollama / Azure / Bedrock
- **MCP**: `mcp` Python SDK (FastMCP)
- **Static analysis**: ruff, mypy, eslint, tsc, cppcheck (auto-detected)
- **Frontend**: Tailwind CSS, vanilla JS modules, D3 v7, highlight.js (lazy-loaded)
- **Crypto**: cryptography (Fernet) for API-key encryption at rest
- **Tests**: pytest, pytest-asyncio, anyio, FastAPI TestClient

## Quick Start

### Prerequisites

- Python 3.11+
- Conda (recommended) or `venv`
- Git
- Optional: Node.js for JS/TS static analysis

### 1. Clone and create the environment

```bash
git clone https://github.com/foolkking/omnicode-mcp.git
cd omnicode-mcp

# Conda (recommended)
conda create -n omnicode-env python=3.11 -y
conda activate omnicode-env
pip install -e .

# OR venv
python -m venv .venv
. .venv/Scripts/activate    # Windows PowerShell
# . .venv/bin/activate      # macOS / Linux
pip install -e .
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Optional — keys can also be added via the web UI's Model Providers panel
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Embeddings — kept offline by default
TRANSFORMERS_OFFLINE=1
HF_HUB_OFFLINE=1

# Default provider
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash
```

### 3. Run the API server

```bash
uvicorn main:app --port 6789
```

Then open <http://127.0.0.1:6789/> in your browser.

The first index build takes 30 – 60 s; click **Search → Rebuild Index** in the dashboard.

### 4. Connect to Claude Desktop / Cursor / VS Code

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "omnicode": {
      "command": "python",
      "args": ["C:/path/to/omnicode-mcp/mcp_server.py"],
      "env": {
        "ENV_FILE": "C:/path/to/omnicode-mcp/.env"
      }
    }
  }
}
```

Restart the client. You should see 25+ omnicode tools available.

## HTTP API

The full route list lives at `/docs` (Swagger) once the server is running. Highlights:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/search` | Semantic search via FAISS |
| `POST` | `/search/text` | Substring scan with file-pattern filter |
| `POST` | `/search/symbols` | Fuzzy / exact symbol search |
| `GET`  | `/search/symbols/{file_path:path}` | List symbols in a file |
| `GET`  | `/search/symbols/graph` | Build cross-file call graph |
| `GET`  | `/search/inheritance` | Build subclass → base graph |
| `POST` | `/search/symbols/relations` | Per-symbol callers + callees |
| `POST` | `/read` | Read file with line range or symbol resolution |
| `POST` | `/write` | Write file with quality gate |
| `POST` | `/edit` | AI edit with three-layer defence |
| `POST` | `/memory/store` | Store memory (auto-dedupe by fingerprint) |
| `POST` | `/memory/search` | Hybrid keyword + semantic search with `min_score` |
| `POST` | `/memory/dedupe` | Collapse duplicate active memories |
| `GET`  | `/providers` | List registered LLM providers |
| `POST` | `/providers/{name}/test` | Test provider with 20s timeout |
| `PUT`  | `/selections` | Set role → provider assignments |
| `GET`  | `/working-directory` | Active project info + service status |
| `PUT`  | `/working-directory` | Switch project (auto-loads new `.data/`) |
| `WS`   | `/logs/stream` | Live log tail |

## Per-project data layout

Every project keeps an isolated data folder so switching working directories preserves
each project's memories and search index without conflicts.

```
<project>/
├── .data/
│   ├── vector_store.db          # SQLite chunks (per-project)
│   ├── vector_store.faiss       # persisted FAISS index (per-project)
│   ├── metadata.db              # memory store (per-project)
│   └── providers.db             # legacy per-project provider DB (still honoured)
└── .codebase/                   # auto-commit git repo for AI edits

~/.kiro/codebase-mcp/            # USER-LEVEL — shared across projects
├── providers.db                 # provider registry + encrypted API keys
├── providers.key                # Fernet key (file mode 0600)
└── selections.db                # role → provider assignments
```

Resolution order for the provider DB:

1. `PROVIDER_DB_PATH` env var, if set (relative paths resolve to the working dir).
2. `<project>/.data/providers.db` if it already exists (legacy).
3. `~/.kiro/codebase-mcp/providers.db` (user-level shared default).

A one-time migration copies a legacy project DB up to the user level on first run, so
existing API keys stay decryptable.

## Web console screenshots

> Screenshots are kept in `docs/screenshots/`. Open the dashboard at <http://127.0.0.1:6789/>
> after starting the server.

## Testing

The full test suite covers AST parsing, call/inheritance graphs, token compression, the LLM router with stubbed providers, the encryption-at-rest provider registry, edit-pipeline safety against thinking-model leakage, and route-level regressions for every UI bug shipped to date.

```bash
# All tests
conda run --no-capture-output -n omnicode-env python -m pytest tests -q

# Targeted regression suite (~75 s)
python -m pytest tests/integration/test_route_regressions.py -q

# Lint
ruff check omnicode api core tests
```

Latest CI status: **267 passed, 1 skipped, 13 warnings** in ~90 s.

Performance benchmarks (Windows laptop, ~125 source files, in `benchmarks/run_all.py`):

| Benchmark | Target | Measured |
|---|---|---|
| Call graph cold build | < 1.5 s | 702 ms |
| Call graph `update_file` median | < 50 ms | 10 ms |
| Inheritance cold build | < 1 s | 503 ms |
| Token compress 5 KB | < 10 ms | 2 – 2.5 ms |

## Configuration

All settings live in `omnicode/config/settings.py` and are overridable via `.env` or environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `WORKING_DIR` | `cwd` | Project root the server operates on |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `6789` | Where FastAPI listens |
| `PROVIDER_DB_PATH` | unset → user-level | Force a specific provider DB path |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `MAX_SEARCH_RESULTS` | `10` | Default page size |
| `QUALITY_THRESHOLD` | `0.8` | Edit pipeline quality gate |
| `CODEBASE_GIT_DIR` | `.codebase` | Auto-commit git for AI edits |
| `MEMORY_MIN_IMPORTANCE` | `3` | Default importance filter |
| `FS_BROWSER_DENY_PATTERNS` | `[/etc/shadow, ...]` | Paths the file picker refuses |

## Roadmap

Detailed evaluation of upcoming work is tracked in [`docs/roadmap.md`](docs/roadmap.md), prioritised by token-savings impact:

1. LSP-MCP bridge — `goToDefinition` / `findReferences` / `hover` / `getDiagnostics`
2. Symbol-outline mode for `/read` (≈ 50 % token reduction on large-file reads)
3. Diagnostics-first search (attach Guard output to every result)
4. Tool-description compression (mcp-compressor pattern)
5. TOON output encoding for large graph payloads
6. Skills framework alignment with Anthropic Agent Skills
7. Code-execution tool with sandboxed exec
8. Tool search instead of list-all on MCP startup
9. Incremental embedding cache
10. Auto memory advisory injection into edit pipeline

## Contributing

PRs welcome. Please:

- Run `ruff check omnicode api core tests` before pushing — never apply `--fix` to `tests/` (history reason: a previous `ruff --fix tests` accidentally deleted the directory).
- Add a regression test in `tests/integration/test_route_regressions.py` for any UI-visible fix.
- Keep API responses on the `{"success": true, "result": {...}}` envelope so the web client handlers don't break.
- Document architectural changes in `docs/test_plan.md`.

See [`docs/test_plan.md`](docs/test_plan.md) for the manual + automated test matrix.

## License

MIT — see [`LICENSE`](LICENSE).

---

## References

The design draws on the following sources. Content has been paraphrased and condensed for licensing compliance; reproduction stays under thirty consecutive words per source.

### Specifications and protocols

- [Model Context Protocol — Wikipedia overview](https://en.wikipedia.org/wiki/Model_Context_Protocol)
- [Model Context Protocol — official site](https://modelcontextprotocol.io/)
- [Tree-sitter — incremental parsing library](https://tree-sitter.github.io/tree-sitter/)
- [Language Server Protocol specification](https://microsoft.github.io/language-server-protocol/)

### Anthropic engineering blog

- [Code execution with MCP — building more efficient AI agents](https://anthropic.com/engineering/code-execution-with-mcp), 2025-11
- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09
- [Advanced tool use on the Claude Developer Platform](https://www.anthropic.com/engineering/advanced-tool-use), 2025-11
- [New capabilities for building agents on the Anthropic API](https://www.anthropic.com/news/agent-capabilities-api), 2025-10
- [Anthropic — code execution tool docs](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/code-execution-tool)

### Token efficiency and MCP optimisation

- [StackOne — MCP Token Optimization: 4 Approaches Compared](https://www.stackone.com/blog/mcp-token-optimization/), 2025-12
- [MindStudio — How to Reduce Token Usage in AI Agents: 10 MCP Optimization Techniques](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization), 2025-12
- [Mukund Kidambi — Beyond GraphQL: what actually reduces token spend in MCP servers](https://medium.com/@mukundkidambi/beyond-graphql-what-actually-reduces-token-spend-in-mcp-servers-9aa3350e8d4d), 2025-12
- [Atlassian Labs — mcp-compressor](https://github.com/atlassian-labs/mcp-compressor), 2025
- [PlainEnglish — Building the Blueprint for Production-Grade MCP Servers](https://plainenglish.io/artificial-intelligence/building-the-blueprint-for-production-grade-mcp-servers), 2025-12

### LSP integration in MCP

- [Claude Code v2.0.74 LSP support](https://how2shout.com/news/claude-code-v2-0-74-lsp-language-server-protocol-update.html), 2025-12
- [Skywork — What is lsp-mcp? Bridging MCP and LSP](https://skywork.ai/blog/lsp-mcp-mcp-lsp-bridge/), 2025
- [jonrad/lsp-mcp](https://github.com/jonrad/lsp-mcp), 2025
- [LobeHub — vimo-ai LSP MCP Server](https://lobehub.com/mcp/vimo-ai-lsp-mcp-server), 2025
- [Visual Studio Marketplace — sehejjain.lsp-mcp-bridge](https://marketplace.visualstudio.com/items?itemName=sehejjain.lsp-mcp-bridge)
- [Visual Studio Marketplace — CJL.lsp-mcp](https://marketplace.visualstudio.com/items?itemName=CJL.lsp-mcp)
- [Playbooks — MultilspyLSP MCP server](https://playbooks.com/mcp/asimihsan-multilspy-lsp), 2025
- [Kiro — Code Intelligence](https://kiro.dev/docs/cli/code-intelligence/)

### Skills and agent frameworks

- [IntuitionLabs — Claude Skills vs MCP](https://intuitionlabs.ai/articles/claude-skills-vs-mcp), 2025-10
- [Skywork — Claude Skills allowed tools](https://skywork.ai/blog/ai-bot/claude-skills-allowed-tools-ultimate-guide/)
- [Microsoft DevBlog — AI Skills Executor in .NET with Azure OpenAI MCP](https://devblogs.microsoft.com/foundry/dotnet-ai-skills-executor-azure-openai-mcp/), 2025-12
- [yoloshii/mcp-code-execution-enhanced](https://github.com/yoloshii/mcp-code-execution-enhanced)

### Semantic code search

- [Weaviate — Build a Coding Assistant with Weaviate MCP: RAG over Code & Docs](https://weaviate.io/blog/coding-assistant-weaviate-mcp), 2025-12
- [ceaksan — Local semantic code search MCP](https://ceaksan.com/en/local-semantic-code-search-ai-mcp), 2025
- [Shamsul Arefin — Building an AI Agent with MCP Code Execution](https://medium.com/@shamsul.arefin/building-an-ai-agent-with-mcp-code-execution-from-confusion-to-clarity-6b13fccc8c4b), 2025-11
- [Scott Lepper — MCP Servers that don't suck (tokens)](https://medium.com/@scott.lepper/mcp-servers-that-dont-suck-tokens-0d6ea31e7522), 2025-12

### Upstream project

- [danyQe/codebase-mcp](https://github.com/danyQe/codebase-mcp) — original project this fork builds on.

### Libraries used

- [LiteLLM](https://github.com/BerriAI/litellm) — multi-provider LLM gateway
- [FAISS](https://github.com/facebookresearch/faiss) — vector similarity search
- [sentence-transformers](https://github.com/UKPLab/sentence-transformers) — embeddings (`all-MiniLM-L6-v2`)
- [FastAPI](https://fastapi.tiangolo.com/) — async HTTP framework
- [Pydantic v2](https://docs.pydantic.dev/) — data validation
- [D3.js v7](https://d3js.org/) — call/inheritance graph rendering
- [highlight.js](https://highlightjs.org/) — syntax colouring in the code-preview modal
- [Tailwind CSS](https://tailwindcss.com/) — styling
- [tree-sitter](https://tree-sitter.github.io/) — multi-language AST parsing
- [cryptography (Fernet)](https://cryptography.io/) — at-rest encryption of API keys
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) — `mcp.server.fastmcp.FastMCP`

> All third-party trademarks belong to their respective owners. References are listed for attribution; the implementation in this repository is original work paraphrasing the ideas and best practices described in those sources.
