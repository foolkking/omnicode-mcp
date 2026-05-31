<div align="center">

# OmniCode-MCP

**The codebase intelligence layer your AI editor is missing.**

Stop your AI editor from hallucinating across files. OmniCode-MCP gives Cursor,
Claude, Continue, Aider, Kiro and any MCP-compatible client a single endpoint
to *understand* your repository — search, impact analysis, safe patching,
memory recall — over MCP, REST, or WebSocket.

[![Python](https://img.shields.io/badge/python-3.11%20|%203.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7C3AED)](https://modelcontextprotocol.io/)
[![Tree-sitter](https://img.shields.io/badge/tree--sitter-7%20languages-22C55E)](https://tree-sitter.github.io/tree-sitter/)
[![LSP](https://img.shields.io/badge/LSP-10%20languages-0EA5E9)](https://microsoft.github.io/language-server-protocol/)
[![Tests](https://img.shields.io/badge/tests-433%20passed-brightgreen)](#testing)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0--rc1-orange)](pyproject.toml)

[**English**](README.md) · [**简体中文**](README_zh.md)

[Quick Start](#-quick-start) ·
[How it works](#-how-it-works) ·
[MCP tools](#-mcp-tools) ·
[Use cases](#-use-cases) ·
[Documentation](#-documentation) ·
[FAQ](#-faq)

</div>

---

## ✨ Why OmniCode-MCP

LLMs are confident liars when they don't know your repo. Most editor
extensions feed the model the current file plus a few greps, hope for
the best, and silently corrupt files when the patch goes wrong.
OmniCode-MCP plugs the gap with **eight composable capabilities** that
every AI editor needs but rarely has all of:

| # | Capability | What it does | Module |
|---|---|---|---|
| 1 | **Code understanding** | Tree-sitter AST in 7 languages, multi-mode read (outline / symbols / full / range) | [`omnicode/ast_engine/`](omnicode/ast_engine/) |
| 2 | **Context compression** | Strip comments, fold function bodies, rank chunks by priority — fits more code in the same prompt | [`omnicode/llm/token_manager.py`](omnicode/llm/token_manager.py) |
| 3 | **Hybrid search** | Semantic + symbol + text fused via RRF (auto-selected for short queries; explicit `mode=hybrid` available). Per-result `why_matched` so the model sees *why* a hit was retrieved | [`omnicode/search/`](omnicode/search/) |
| 4 | **Impact analysis** | BFS blast radius, callers, callees, risk score, suggested tests | [`omnicode_core/graph/impact.py`](omnicode_core/graph/impact.py) |
| 5 | **Safe patch ops** | `preview` → `validate` → `apply` → `rollback` with snapshots; the LLM never writes raw files | [`omnicode_core/edit/patch.py`](omnicode_core/edit/patch.py) |
| 6 | **Memory recall** | Manually-stored project memories with multi-angle automatic recall before edits (search by file / symbol / task / error / dependency in parallel) | [`omnicode_core/memory/advisory.py`](omnicode_core/memory/advisory.py) |
| 7 | **Debug console** | Web UI for index health, diff inspection, advisory drawer, edit-session viewer | [`templates/`](templates/) |
| 8 | **Optional LLM** | Multi-provider router with circuit-breaker, fallback, best-of-N — opt-in via `[llm]` extras | [`omnicode/llm/router.py`](omnicode/llm/router.py) |

A **single REST call** at `POST /intelligence/context` runs all eight in
parallel, fits the result in your token budget, and returns it as a
structured payload. Or use the **MCP tool `omni_intelligence`** to get
the same shape over stdio / SSE / streamable-http.

> [!NOTE]
> OmniCode-MCP is **not** another AI editor. It's the service Cursor /
> Continue / Claude Code / Aider / Kiro call into. They write code, we
> make their writes land more accurately and use fewer tokens.

---

## 🚀 Quick Start

### Install

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

Conda users: replace the venv steps with
`conda create -n omnicode-env python=3.11 -y && conda activate omnicode-env`.

> [!TIP]
> The core install (no `[llm]` extra) gives you search, impact,
> patch, memory, MCP, and the web console. Set
> `OMNICODE_LLM_ROUTER=false` to make the boot path skip the LLM
> stack entirely — useful when your AI editor brings its own model.

### Run

```bash
omnicode serve --console          # API + Web Console at http://127.0.0.1:6789/
omnicode serve --headless         # API only, no UI
omnicode mcp                      # MCP stdio transport (for Claude / Cursor / Kiro)
omnicode dev                      # console + auto-reload
```

The first index build takes 30 – 60 s; subsequent rebuilds are
incremental (2 – 3 s).

### Connect Claude / Cursor / Kiro (MCP stdio)

Add the snippet to your client's MCP config — `claude_desktop_config.json`
for Claude Desktop, `~/.kiro/settings/mcp.json` for Kiro, the equivalent
for any other MCP-compatible client:

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

Restart the client. By default 8 high-level tools are registered. Set
`OMNICODE_MCP_TOOLS=all` if you also want the legacy 16 fine-grained
tools.

### One-call composer

The fastest way to feel the difference: ask the composer to assemble
*everything* about a symbol in one round-trip.

```bash
curl -X POST http://127.0.0.1:6789/intelligence/context \
  -H 'content-type: application/json' \
  -d '{
        "task": "explain create_app",
        "file_path": "main.py",
        "symbol": "create_app",
        "token_budget": 4096
      }'
```

Returns a structured payload with the file outline, call graph, impact,
related tests, recent git history, and matching memory advisories — all
trimmed to your token budget.

---

## 🧩 How it works

```text
              ┌─────────────────────────────────────────────────────────┐
              │   AI Editor / Agent  (Cursor · Claude Desktop · Kiro    │
              │   · Continue · Aider · custom REST client · VS Code)    │
              └─────────────────────────────────────────────────────────┘
                            │ MCP stdio / SSE / streamable-http
                            │              · or ·
                            │ HTTP REST (with X-API-Key / Bearer)
                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │      Adapters  (omnicode_adapters/)                     │
              │  · cli/                — omnicode CLI subcommands       │
              │  · mcp_server/         — FastMCP host + auth gate       │
              │  · agent/              — local file-sync watcher        │
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
              │  Storage                                                 │
              │  <wd>/.data/shards/<id>/  vector_store.faiss + .db,     │
              │                           file_tracker.db, snapshots/,  │
              │                           edit_sessions/                 │
              │  ~/.kiro/codebase-mcp/    providers.db, users.db,       │
              │                           workspaces.json                │
              └─────────────────────────────────────────────────────────┘
```

Three layers, one direction. Adapters call core; core never imports
adapters. LLM and Web UI are **opt-in extras** — the core surface
runs in a stripped install. Full design rationale in
[`docs/architecture.md`](docs/architecture.md).

### Three deployment modes

| Mode | Code lives | The server does | Default knobs |
|---|---|---|---|
| 🏠 **local** | This machine | Index, search, LSP, edit | writes ✅, apply ✅ |
| ☁️ **cloud** | This machine (mirror) | Index, search, LSP, ~edit~ | writes ❌, apply ❌ |
| 🔄 **hybrid** | User's local box | Index + search + memory + graph; agent pushes file bodies | writes via agent ✅, apply ❌ |

Production cloud setup is documented in
[`docs/deployment.md`](docs/deployment.md) — systemd + nginx pattern,
Docker Compose + Caddy pattern, the 8-layer security model, and a
hardening checklist.

---

## 🔧 MCP tools

Nine core tools registered by default (`OMNICODE_MCP_TOOLS=core`):

| Tool | What to ask for |
|---|---|
| `omni_search` | "Find where the auth middleware is set up" — semantic / symbol / text / hybrid / LSP references |
| `omni_read` | "Show me the outline of `services/billing.py`" — outline / symbols / full / range / imports / diagnostics |
| `omni_impact` | "What breaks if I rename `User.email`?" — callers, callees, risk badge, suggested tests |
| `omni_diagnostics` | "What's wrong with `api/users.py` right now?" — ruff / mypy / eslint / tsc / LSP diagnostics fused |
| `omni_context` | "Give me everything I need to explain `create_app`" — composer in one call |
| `omni_memory` | "Has anyone solved this before in this repo?" — manually-stored memories with multi-angle recall |
| `omni_patch` | "Apply this patch safely" — preview → validate → apply → rollback, with EditSession id for undo |
| `omni_skill` | "What's the recommended workflow for a refactor?" — packaged recipes (impact-review, safe-refactor, test-coverage) |
| `discover_tools` | List the surface and pick the right tool |

Backwards-compatible aliases (still work for older MCP configs):
`omni_analyze` → `omni_impact`, `omni_edit` → `omni_patch`,
`omni_intelligence` → `omni_context`.

Need finer-grained tools? `OMNICODE_MCP_TOOLS=all` exposes 24 (the 8
core ones + 16 lower-level legacy tools); `legacy` exposes only the 16
lower-level ones.

---

## 💡 Use cases

OmniCode-MCP is intentionally a **layer**, not a product. Things people
have built or are building on top:

- **Smarter AI editor extensions** — call `/intelligence/context`
  before each prompt to cut tokens by 30 – 60% while improving
  accuracy.
- **PR review bots** — run `/graph/impact?symbol=…&depth=3` on every
  diff and flag high-blast-radius changes for human review.
- **Refactor agents** — loop over `/patch/preview` →
  `/patch/validate` → `/patch/apply` with the agent's own LLM,
  never letting the LLM write to disk directly.
- **CI quality gates** — fail the build if a PR touches a
  `risk_level=high` symbol without adding a test from the
  suggested-tests list.
- **Custom dashboards** — the bundled Web Console is just an HTML/JS
  client of the public REST API. Build your own.
- **Internal documentation generators** — call `/symbols/graph` and
  `/git/history` to auto-populate ADRs.

---

## ⚙️ Configuration

Three sources, highest wins:

1. **CLI flags** — `omnicode serve --mode cloud --port 8765 ...`
2. **Env vars** — every Pydantic field has a matching env name (e.g.
   `OMNICODE_PORT`, `OMNICODE_API_KEY`).
3. **TOML file** — `omnicode.toml` next to where you launch (or
   `OMNICODE_CONFIG=/path`). See
   [`omnicode.example.toml`](omnicode.example.toml).

The most common knobs:

```toml
[server]
mode = "local"            # local | cloud | hybrid
host = "127.0.0.1"
port = 6789

[security]
api_key = ""              # legacy single-key auth (X-API-Key header)
allow_apply_patch = true  # cloud deployments often flip this off
mcp_tools = "core"        # core (8) | all (24) | legacy (16)

[index]
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"

[search]
reranker = false          # cross-encoder rerank (opt-in)

[features]
web_console = true
lsp = true
memory = true
safe_edit = true
llm_router = true         # set false for a no-LLM core install
ai_edit = true            # LLM-driven /edit endpoint; depends on llm_router
```

Full reference — every env var, every TOML key, every precedence rule
— is in [`docs/usage.md`](docs/usage.md).

---

## 🔒 Security at a glance

Defence-in-depth, opt-in layers. Run a local laptop with zero
auth overhead, or a hardened cloud box with all eight layers — same
code.

- **Path sandbox** — `..`, absolute paths, out-of-tree symlinks
  rejected on every endpoint.
- **Safe edits, always** — every write (LLM-driven `/edit`,
  `intelligent_write`, fallback file ops) routes through PatchManager
  for a snapshot + EditSession + rollback. The LLM never overwrites
  your file without leaving a breadcrumb.
- **Three auth tiers** — single API key, multi-user RBAC
  (admin / editor / viewer), and per-deployment read-only mode that
  composes cleanly.
- **Provider keys at rest** — Fernet-encrypted in
  `~/.kiro/codebase-mcp/providers.db`. `omnicode rotate-master-key`
  re-encrypts every row under a fresh key with rollback on failure.
- **Token expiry + revoke-by-user** — `expires_in_days` on issue,
  auto-revoke after expiry, one-call employee offboarding via
  `DELETE /admin/users/{u}/tokens`.
- **MCP-over-HTTP gate** — SSE / streamable-http transports honour
  the same auth sources; `--auth required` refuses to start without
  one.
- **Per-workspace shards** — workspace A's search results can't leak
  into workspace B; dropping a workspace drops the shard atomically.

> [!IMPORTANT]
> Cloud mode defaults to read-only and apply-blocked. The preview /
> validate / explain endpoints stay open so the editor can render the
> diff and analysis to its user, just not write to disk over the
> wire.

Full security model and threat model in
[`docs/deployment.md`](docs/deployment.md).

---

## 📈 Observability

- **Audit log** — append-only CSV at `~/.kiro/codebase-mcp/audit.log`
  (override via `OMNICODE_AUDIT_LOG`). Every `/admin/*` mutation and
  every `/patch/apply` is recorded with `(ts, actor, action, target,
  ip, outcome, extra)`.
- **Prometheus metrics** — `GET /monitoring/metrics?format=prometheus`
  for the standard text format, or `format=json` for a structured
  shape. No external dependency.
- **Per-IP rate limit on `/admin/*`** — token bucket, default 30
  req/min/IP, tune via `OMNICODE_ADMIN_RATE_LIMIT`. Returns 429 with
  `Retry-After` header.
- **Idempotency-Key on `/patch/apply`** — pass any stable string;
  same key + same payload returns the cached response, different
  payload returns 409. SQLite-backed cache with 24 h TTL.

---

## 🖥️ CLI

```bash
omnicode init                      # write .data/ skeleton
omnicode index [--force]           # incremental / full rebuild
omnicode status                    # via /health
omnicode doctor                    # python / LSP / models / ports check
omnicode serve [--headless] [--console] [--mode local|cloud|hybrid]
omnicode dev                       # console + auto-reload
omnicode mcp                       # stdio MCP for AI editors
omnicode mcp --transport sse --port 6790 --auth required
omnicode mcp --workspace . --workspace-id repo-a --backend-url https://omnicode.example.com --backend-token "$OMNICODE_API_KEY" --executor hybrid
omnicode agent --remote URL --token TOK --workspace . --workspace-id repo-a
omnicode rotate-master-key [--db ...] [--key ...] [--new-key BASE64]
```

Use `omnicode mcp --backend-url ...` as a local stdio bridge when
the AI editor runs locally but OmniCode's FastAPI backend is deployed
on a cloud machine. The backend token is sent as `X-API-Key`; the
logical project id is sent as `X-Omnicode-Workspace`.

Run-helpers under [`scripts/`](scripts/) (`run.bat` / `.sh`,
`run-dev.bat` / `.sh`, `test.bat` / `.sh`, `lint.bat` / `.sh`).

---

## 📊 Performance

Validated on this repo (~125 source files, mostly Python):

| Benchmark | Target | Measured |
|---|---|---|
| Call graph cold build | < 1.5 s | **702 ms** |
| Call graph `update_file` median | < 50 ms | **10 ms** |
| Inheritance cold build | < 1 s | **503 ms** |
| Inheritance `update_file` | < 20 ms | **2 ms** |
| Token compress 5 KB | < 10 ms | **2 – 2.5 ms** |
| Incremental rebuild (no file changes) | — | **< 1 s** |

Run `python benchmarks/run_all.py` to reproduce.

---

## 🧪 Testing

```bash
# Full suite (~30 s)
python -m pytest tests -q

# Just the regressions ring (~12 s)
python -m pytest tests/integration/test_route_regressions.py -q

# Lint
ruff check omnicode omnicode_core omnicode_adapters api core tests
```

Latest CI: **433 passed, 12 skipped** — the 12 skipped are LSP-binary
probes that auto-skip when the language server isn't installed
locally.

---

## 📚 Documentation

Main docs by category:

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | What's in the box — design rationale, eight capabilities, module map, persistence layout. |
| [`docs/usage.md`](docs/usage.md) | Install, configure, run, connect AI editors. Includes optional LLM extras. |
| [`docs/api.md`](docs/api.md) | Full REST + MCP catalog with request / response shapes. |
| [`docs/deployment.md`](docs/deployment.md) | Production deployment patterns + 8-layer security model. |
| [`docs/roadmap.md`](docs/roadmap.md) | Post-1.0 research directions; permanent non-goals. |
| [`extensions/vscode/README.md`](extensions/vscode/README.md) | Thin VS Code extension (3 commands). |
| [`_keep_/README.md`](_keep_/README.md) | How to share artefacts that bypass `.gitignore`. |

---

## 🗺️ Roadmap

The original architecture-v2 plan plus Wave 1 audit and Wave 2 backlog
(43 items total) are **all shipped** as of `1.0.0-rc1`. Post-1.0
research directions live in [`docs/roadmap.md`](docs/roadmap.md):

- 🧠 Code-specific embeddings (Jina v3 / OpenAI / starcoder) with an
  A/B harness against the current generic encoder.
- 📦 Skills framework alignment (Anthropic Agent Skills) — let
  editors load only the tool subset they need per task.
- 🛡️ Code-execution sandbox (`bubblewrap` / `seccomp`) for the
  currently-disabled `execute_tool`.
- 📈 Telemetry-driven prompt feedback — mine edit sessions into the
  memory advisory store, opt-in only.

---

## ❓ FAQ

<details>
<summary><b>How is this different from Cursor / Continue / Claude Code?</b></summary>

OmniCode-MCP is a **service**, not an editor. Cursor and friends
edit code; OmniCode-MCP makes their edits land more accurately by
giving them search, impact analysis, safe patch ops, and memory
recall over MCP. You can use OmniCode-MCP *with* any of those
editors at the same time.
</details>

<details>
<summary><b>Does it require a paid LLM?</b></summary>

No. The core (search, impact, patch, memory, MCP surface) runs
without any LLM. The LLM router is an **opt-in extra**
(`pip install -e ".[llm]"`) for users who want server-side AI
edit pipelines. Most users let their AI editor (Claude / GPT /
Gemini) call OmniCode-MCP's tools directly.
</details>

<details>
<summary><b>Does my code leave the machine?</b></summary>

By default, no. Local mode never makes outbound calls. Embeddings
run on-device via `sentence-transformers/all-MiniLM-L6-v2`. The
optional remote embedding backend and the optional LLM router are
the only paths that talk to third parties, and both are off by
default.
</details>

<details>
<summary><b>Which languages are supported?</b></summary>

**AST**: Python, JavaScript, TypeScript, C++, Java, Go, Rust (7).
**LSP**: Python, TypeScript, Go, Rust, C/C++, Ruby, PHP, Java,
Kotlin, C# (10) — auto-detected; the bridge skips unavailable
servers.
</details>

<details>
<summary><b>Can I run this on Windows?</b></summary>

Yes. Tested on Windows 10/11 with Python 3.11 (conda or venv).
Use `scripts/run.bat` for the easiest setup. Path sandbox
correctly handles drive letters and falls back gracefully when
symlink creation isn't permitted.
</details>

<details>
<summary><b>How does it scale?</b></summary>

Per-workspace FAISS shards keep multi-project deployments isolated.
Cold-build cost is roughly linear in source-file count; the
incremental cache makes hot reloads near-free. The recommended cloud
shape is one VM per team — multi-tenant SaaS isn't a target.
</details>

<details>
<summary><b>What's the licence story for the embedding model?</b></summary>

`sentence-transformers/all-MiniLM-L6-v2` is Apache 2.0. Tree-sitter
grammars are MIT. LSP servers ship under their own licences and
are loaded as separate processes. OmniCode-MCP's own code is MIT.
</details>

---

## 🤝 Contributing

PRs welcome. Quick checklist:

1. Run `ruff check omnicode omnicode_core omnicode_adapters api core tests`
   before pushing — never apply `--fix` to `tests/`.
2. Add a regression test for any UI-visible fix.
3. Keep API responses on the `{"success": true, "result": {...}}`
   envelope so the web client handlers don't break.
4. Document architectural changes in
   [`CONTRIBUTING.md`](CONTRIBUTING.md).

The full developer on-ramp — architecture rules, coding conventions,
common patterns, regression matrix — is in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

---

## 🙏 Acknowledgements

The design draws on the following sources. Content has been paraphrased
and condensed for licensing compliance.

**Specifications and protocols**

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Tree-sitter](https://tree-sitter.github.io/tree-sitter/)
- [Language Server Protocol](https://microsoft.github.io/language-server-protocol/)

**Anthropic engineering blog**

- [Code execution with MCP — building more efficient AI agents](https://anthropic.com/engineering/code-execution-with-mcp), 2025-11
- [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09
- [Advanced tool use on the Claude Developer Platform](https://www.anthropic.com/engineering/advanced-tool-use), 2025-11

**Token efficiency and MCP optimisation**

- [StackOne — MCP Token Optimization](https://www.stackone.com/blog/mcp-token-optimization/)
- [MindStudio — MCP Optimization Techniques](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization)
- [Atlassian Labs — mcp-compressor](https://github.com/atlassian-labs/mcp-compressor)

**LSP integration in MCP**

- [jonrad/lsp-mcp](https://github.com/jonrad/lsp-mcp)
- [Skywork — lsp-mcp bridging MCP and LSP](https://skywork.ai/blog/lsp-mcp-mcp-lsp-bridge/)

**Upstream**

- [danyQe/codebase-mcp](https://github.com/danyQe/codebase-mcp) — original
  project this fork builds on.

**Libraries**

[LiteLLM](https://github.com/BerriAI/litellm) ·
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

---

<div align="center">

If OmniCode-MCP saves you tokens or close calls, a ⭐ helps others find it.

</div>
