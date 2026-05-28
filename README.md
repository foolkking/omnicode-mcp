<div align="center">

# OmniCode-MCP

**A local-first, optionally cloud-deployable Codebase Intelligence Layer
that any AI editor can call.**

[![Python](https://img.shields.io/badge/python-3.11%20|%203.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7C3AED)](https://modelcontextprotocol.io/)
[![Tree-sitter](https://img.shields.io/badge/tree--sitter-7%20languages-22C55E)](https://tree-sitter.github.io/tree-sitter/)
[![LSP](https://img.shields.io/badge/LSP-10%20languages-0EA5E9)](https://microsoft.github.io/language-server-protocol/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-433%20passed-brightgreen)](#testing)

[Why OmniCode?](#why-omnicode-mcp) ·
[Quick Start](#quick-start) ·
[Architecture](#architecture) ·
[Documentation](#documentation) ·
[Roadmap](#roadmap) ·
[References](#references)

</div>

---

## What is OmniCode-MCP?

OmniCode-MCP is a **service AI editors call**, not yet another AI editor.
It wraps a working directory (or many) in a single FastAPI process and
exposes:

- **Eight composable capabilities** for code understanding, search,
  impact analysis, safe patch operations, and proactive memory recall.
- **A Model Context Protocol surface** so Claude Desktop, Cursor,
  Continue, Aider, Kiro, and any future MCP client can plug in.
- **A REST + WebSocket HTTP API** plus an optional Web Console for
  human review.

The point is to make every AI editor that calls in **smarter, more
token-efficient, and safer** when it touches a real codebase. We do
**not** compete with Cursor / Continue / Copilot — they edit code, we
make their edits land more accurately.

## Why OmniCode-MCP

The eight things every AI editor needs but rarely has all of:

| # | Capability | What it does | Module |
|---|---|---|---|
| 1 | Code understanding | Tree-sitter AST in 7 languages, multi-mode read | [`omnicode/ast_engine/`](omnicode/ast_engine/) |
| 2 | Context compression | Comment-strip, function-fold, priority pruner | [`omnicode/llm/token_manager.py`](omnicode/llm/token_manager.py) |
| 3 | Hybrid search | Semantic + symbol + text + RRF + `why_matched` | [`omnicode/search/`](omnicode/search/) |
| 4 | Impact analysis | BFS blast radius, callers, callees, risk score | [`omnicode_core/graph/impact.py`](omnicode_core/graph/impact.py) |
| 5 | Safe patch ops | Preview → validate → apply → rollback with snapshots | [`omnicode_core/edit/patch.py`](omnicode_core/edit/patch.py) |
| 6 | Memory recall | Multi-angle advisory from past edit sessions | [`omnicode_core/memory/advisory.py`](omnicode_core/memory/advisory.py) |
| 7 | Debug console | Web UI for index status, diff inspector, advisory drawer | [`templates/`](templates/) |
| 8 | Optional LLM | Multi-provider router, opt-in via `[llm]` extras | [`omnicode/llm/router.py`](omnicode/llm/router.py) |

A **single REST call** at `POST /intelligence/context` runs all eight
in parallel, fits the result in your token budget, and returns it as
a structured payload. Or use the **MCP tool `omni_intelligence`** to
get the same shape over stdio / SSE / streamable-http.

## Quick Start

### 1 · Install

```bash
git clone https://github.com/foolkking/omnicode-mcp.git
cd omnicode-mcp

python -m venv .venv
. .venv/Scripts/activate          # Windows PowerShell
# . .venv/bin/activate            # macOS / Linux

pip install -e .                  # core only (no LLM)
# pip install -e ".[llm]"         # add multi-provider LLM router
# pip install -e ".[agent]"       # add filesystem watcher for hybrid mode
# pip install -e ".[dev]"         # tests + linters
```

### 2 · Run

```bash
# Local mode — default
omnicode serve --console          # API + Web Console at http://127.0.0.1:6789/
omnicode serve --headless         # API only — no UI
omnicode mcp                      # MCP stdio (for Claude / Cursor / Kiro)
omnicode dev                      # console + auto-reload

# Cloud / hybrid presets
omnicode serve --mode cloud       # read-only + apply blocked
omnicode serve --mode hybrid      # accepts pushes from a local agent
```

The first index build takes 30 – 60 s; subsequent rebuilds are
incremental (2 – 3 s).

### 3 · Connect an AI editor (MCP stdio)

Add to `~/.kiro/settings/mcp.json` (Kiro), `claude_desktop_config.json`
(Claude Desktop), or your MCP-compatible client of choice:

```json
{
  "mcpServers": {
    "omnicode": {
      "command": "omnicode",
      "args": ["mcp"],
      "env": {
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1"
      }
    }
  }
}
```

Restart the client. By default 8 high-level tools are registered
(`omni_search`, `omni_read`, `omni_edit`, `omni_analyze`,
`omni_memory`, `omni_context`, `omni_intelligence`, `discover_tools`).
Set `OMNICODE_MCP_TOOLS=all` if you also need the legacy 16
fine-grained tools.

### 4 · Use it from anything else

```bash
# Discover what this deployment can do
curl http://127.0.0.1:6789/capabilities

# One-call multi-capability composer
curl -X POST http://127.0.0.1:6789/intelligence/context \
  -H 'content-type: application/json' \
  -d '{"task": "explain create_app", "file_path": "main.py", "symbol": "create_app"}'

# Preview + apply a patch
curl -X POST http://127.0.0.1:6789/patch/preview \
  -H 'content-type: application/json' \
  -d '{"file_path": "main.py", "content": "..."}'
```

## Three deployment modes

| Mode | Code lives | This server does | Default knobs |
|---|---|---|---|
| **local** | This machine | Index, search, LSP, edit | writes ON, apply ON |
| **cloud** | This machine (mirror) | Index, search, LSP, ~edit~ | writes OFF, apply OFF |
| **hybrid** | User's local box | Index + search + memory + graph; agent pushes file bodies | writes ON via agent, apply OFF |

For real cloud deployment see [`docs/cloud-deployment.md`](docs/cloud-deployment.md).

## Architecture

```
              ┌─────────────────────────────────────────────────────────┐
              │   AI Editor / Agent  (Cursor · Claude Desktop · Kiro    │
              │   · Continue · Aider · custom REST client · VS Code     │
              │   ext.)                                                 │
              └─────────────────────────────────────────────────────────┘
                            │ MCP stdio / SSE / streamable-http
                            │              · or ·
                            │ HTTP REST (with X-API-Key / Bearer)
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │      Adapters  (omnicode_adapters/)                     │
              │  · cli/                — omnicode CLI subcommands        │
              │  · mcp_server/         — FastMCP host + auth gate        │
              │  · agent/              — local file-sync watcher (W2-2)  │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │     FastAPI app  (api/v1/routers/* — 22 routers)        │
              │     Middlewares: API-key → RBAC → read-only             │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │  omnicode_core/   ← language-agnostic, no UI / LLM dep  │
              │  ├── intelligence/composer.py  (the 8-capability one)   │
              │  ├── ast/  search/  graph/  memory/  edit/  lsp/        │
              │  ├── auth/ (RBAC, migrations, master-key rotation)      │
              │  ├── workspace/ (per-workspace bookmarks)               │
              │  ├── index/ sharding.py (per-workspace FAISS shards)    │
              │  ├── embeddings/ (local · remote · hybrid backends)     │
              │  └── security/sandbox.py                                │
              └─────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │   Storage                                                │
              │   <wd>/.data/shards/<id>/  vector_store.faiss + .db,    │
              │                            file_tracker.db, snapshots/, │
              │                            edit_sessions/                │
              │   ~/.kiro/codebase-mcp/    providers.db, users.db,      │
              │                            workspaces.json               │
              └─────────────────────────────────────────────────────────┘
```

The split between `omnicode/` (legacy, has LLM deps) and
`omnicode_core/` (clean v2 layer) lets us keep LLM features as opt-in
extras without breaking older callers. See
[`docs/architecture-v2.md`](docs/architecture-v2.md) for the full
rationale.

## Documentation

Start here, in order:

| Doc | What it covers |
|---|---|
| [`docs/features.md`](docs/features.md) | **Feature inventory** — every endpoint, every CLI command, every config key, persistence layout, module map. |
| [`docs/architecture-v2.md`](docs/architecture-v2.md) | Architecture rationale; long-form §1-§17 design decisions. |
| [`docs/api-reference.md`](docs/api-reference.md) | Full REST + MCP catalog with request / response shapes. |
| [`docs/configuration.md`](docs/configuration.md) | Every env var + TOML key + precedence rules. |
| [`docs/security.md`](docs/security.md) | Sandbox, RBAC, key rotation, read-only mode, MCP-over-HTTP auth. |
| [`docs/cloud-deployment.md`](docs/cloud-deployment.md) | systemd + nginx pattern; docker-compose + Caddy pattern; hardening checklist. |
| [`docs/llm-extras.md`](docs/llm-extras.md) | Optional LLM router, provider registry, AI edit pipeline. |
| [`docs/running.md`](docs/running.md) | Local-run cookbook with conda + venv + Windows / macOS / Linux. |
| [`docs/test_plan.md`](docs/test_plan.md) | Manual + automated regression matrix. |
| [`docs/wave2-plan.md`](docs/wave2-plan.md) | Wave 2 implementation log (10 / 10 done). |
| [`docs/final-audit.md`](docs/final-audit.md) | Point-by-point audit of architecture-v2 §1-§17. |
| [`docs/roadmap.md`](docs/roadmap.md) | Long-term research direction; everything pre-1.0 is shipped. |
| [`extensions/vscode/README.md`](extensions/vscode/README.md) | Thin VS Code extension (3 commands). |
| [`_keep_/README.md`](_keep_/README.md) | How to share artefacts that bypass `.gitignore`. |

## Configuration at a glance

Three sources, highest wins:

1. **CLI flags** — `omnicode serve --mode cloud --port 8765 ...`
2. **Process env vars** — every Pydantic Settings field has a matching env name.
3. **TOML file** — `omnicode.toml` next to where you launch (or `OMNICODE_CONFIG=/path`). See [`omnicode.example.toml`](omnicode.example.toml).

Common knobs:

```toml
[server]
mode = "local"            # local | cloud | hybrid

[security]
api_key = ""              # legacy single-key auth (X-API-Key)
allow_apply_patch = true  # cloud deployments often flip this off
mcp_tools = "core"        # core (8) | all (24) | legacy (16)

[index]
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"

[search]
reranker = false          # cross-encoder rerank, opt-in (W2-9)

[features]
web_console = true
lsp = true
memory = true
safe_edit = true
```

Full reference: [`docs/configuration.md`](docs/configuration.md).

## Security

- **Path sandbox** — every file path is resolved, then checked
  against the workspace root. `..` traversal, absolute paths, and
  out-of-tree symlinks are rejected.
- **Three auth tiers** — legacy single-key (`OMNICODE_API_KEY`),
  multi-user RBAC (admin / editor / viewer), and per-deployment
  read-only mode. Stack composes cleanly.
- **Provider keys at rest** — Fernet-encrypted in
  `~/.kiro/codebase-mcp/providers.db`. `omnicode rotate-master-key`
  re-encrypts every row under a fresh key with rollback on failure.
- **Token expiry + revoke-by-user** — `expires_in_days` on issue,
  auto-revoke on first use after expiry, `DELETE
  /admin/users/{u}/tokens` for one-call employee offboarding.
- **MCP-over-HTTP gate** — SSE / streamable-http transports honour
  the same auth sources; `--auth required` refuses to start when
  none are configured.

Full security model: [`docs/security.md`](docs/security.md).

## CLI

```bash
omnicode init                     # write .data/ skeleton
omnicode index [--force]          # incremental / full rebuild
omnicode status                   # via /health
omnicode doctor                   # python / LSP / models / ports check
omnicode serve [--headless] [--console] [--mode local|cloud|hybrid]
omnicode dev                      # console + auto-reload
omnicode mcp                      # stdio MCP for AI editors
omnicode agent --remote URL --token TOK --workspace .
omnicode rotate-master-key [--db ...] [--key ...] [--new-key BASE64]
```

Run-helpers under [`scripts/`](scripts/) (`run.bat`/`.sh`,
`run-dev.bat`/`.sh`, `test.bat`/`.sh`, `lint.bat`/`.sh`).

## Testing

```bash
# All tests (~30 s)
python -m pytest tests -q

# Just the regressions ring (~12 s)
python -m pytest tests/integration/test_route_regressions.py -q

# Lint
ruff check omnicode omnicode_core omnicode_adapters api core tests
```

Latest CI status: **433 passed, 12 skipped** (skipped are LSP-binary
probes that auto-skip when the language server isn't installed
locally).

Performance benchmarks (`benchmarks/run_all.py`, ~125 source files):

| Benchmark | Target | Measured |
|---|---|---|
| Call graph cold build | < 1.5 s | 702 ms |
| Call graph `update_file` median | < 50 ms | 10 ms |
| Inheritance cold build | < 1 s | 503 ms |
| Token compress 5 KB | < 10 ms | 2 – 2.5 ms |
| Incremental rebuild (no file changes) | — | < 1 s |

## What you can build on top

OmniCode-MCP is intentionally a **layer**, not a product. Things
people have built or plan to build on top:

- **AI editor plugins** that ask `/intelligence/context` before each
  prompt to save tokens.
- **Code-review bots** that watch PRs and run
  `/graph/impact?symbol=…&depth=3` to flag high-blast-radius changes.
- **Custom Web Consoles** — the existing one is just an HTML/JS
  client of the public REST API.
- **Refactor agents** that loop over `/patch/preview` →
  `/patch/validate` → `/patch/apply` with their own LLM, never
  letting the LLM write to disk directly.
- **CI guards** that fail the build if a PR touches a symbol with a
  `risk_level=high` rating without also adding a test in the
  suggested-tests list.

## Roadmap

The big design plan ([`docs/architecture-v2.md`](docs/architecture-v2.md))
is fully implemented as of 1.0.0-rc1:

| Phase | Items | Status |
|---|---|---|
| P0 | Core / adapter split, headless mode, LSP bridge, incremental index, patch ops, MCP slim, structured read modes | ✅ |
| P1 | Search rerank scaffolding, memory advisory, impact analysis, edit-session, search debug, API key auth, Docker compose, GH Actions | ✅ |
| P2 | Cloud / hybrid / local modes, MCP-over-HTTP, multi workspace, RBAC, WebGL graph, multi embedding models | ✅ |
| §17 final | Composer assembly + capability fingerprint | ✅ |
| Wave 1 audit | Sandbox, read-only, why_matched, REST exposure for impact + advisory, LSP rename, modes flag, MCP slim | ✅ |
| Wave 2 (10 items) | TOML config, HTTPS+systemd, MCP-over-HTTP auth, local agent, master-key rotation, Web Console new pages, LSP fleet, reranker, FAISS shards, VS Code extension | ✅ |

Next: [`docs/roadmap.md`](docs/roadmap.md) tracks long-term research
directions (per-language code-specific embeddings, agent skills
alignment, code-execution sandbox).

## Contributing

PRs welcome. Please:

- Run `ruff check omnicode omnicode_core omnicode_adapters api core tests`
  before pushing — never apply `--fix` to `tests/` (history
  reason: a previous `ruff --fix tests` accidentally deleted the
  directory).
- Add a regression test for any UI-visible fix.
- Keep API responses on the `{"success": true, "result": {...}}`
  envelope so the web client handlers don't break.
- Document architectural changes in `docs/test_plan.md`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full developer
on-ramp.

## License

MIT — see [`LICENSE`](LICENSE).

---

## References

The design draws on the following sources. Content has been
paraphrased and condensed for licensing compliance; reproduction stays
under thirty consecutive words per source.

### Specifications and protocols

- [Model Context Protocol — official site](https://modelcontextprotocol.io/)
- [Tree-sitter — incremental parsing library](https://tree-sitter.github.io/tree-sitter/)
- [Language Server Protocol specification](https://microsoft.github.io/language-server-protocol/)

### Anthropic engineering blog

- [Code execution with MCP — building more efficient AI agents](https://anthropic.com/engineering/code-execution-with-mcp), 2025-11
- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09
- [Advanced tool use on the Claude Developer Platform](https://www.anthropic.com/engineering/advanced-tool-use), 2025-11

### Token efficiency and MCP optimisation

- [StackOne — MCP Token Optimization](https://www.stackone.com/blog/mcp-token-optimization/), 2025-12
- [MindStudio — MCP Optimization Techniques](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization), 2025-12
- [Atlassian Labs — mcp-compressor](https://github.com/atlassian-labs/mcp-compressor)

### LSP integration in MCP

- [jonrad/lsp-mcp](https://github.com/jonrad/lsp-mcp)
- [Skywork — lsp-mcp bridging MCP and LSP](https://skywork.ai/blog/lsp-mcp-mcp-lsp-bridge/)

### Upstream

- [danyQe/codebase-mcp](https://github.com/danyQe/codebase-mcp) —
  original project this fork builds on.

### Libraries

- [LiteLLM](https://github.com/BerriAI/litellm) ·
  [FAISS](https://github.com/facebookresearch/faiss) ·
  [sentence-transformers](https://github.com/UKPLab/sentence-transformers) ·
  [FastAPI](https://fastapi.tiangolo.com/) ·
  [Pydantic v2](https://docs.pydantic.dev/) ·
  [D3.js v7](https://d3js.org/) ·
  [highlight.js](https://highlightjs.org/) ·
  [Tailwind CSS](https://tailwindcss.com/) ·
  [tree-sitter](https://tree-sitter.github.io/) ·
  [cryptography (Fernet)](https://cryptography.io/) ·
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) ·
  [watchfiles](https://github.com/samuelcolvin/watchfiles)

> All third-party trademarks belong to their respective owners.
> References are listed for attribution; the implementation in this
> repository is original work paraphrasing the ideas described in
> those sources.
