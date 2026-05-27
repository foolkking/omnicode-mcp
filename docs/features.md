# OmniCode-MCP — Feature Inventory

> Snapshot of the shipped feature set on the `foolkking/main` branch
> through commit `68b911e`. Use this as the single source of truth
> when explaining the project to a new collaborator or to an LLM.
>
> Pairs with:
> * [`docs/architecture-v2.md`](architecture-v2.md) — the long-form
>   architecture plan.
> * [`docs/wave2-plan.md`](wave2-plan.md) — what's still parked.
> * [`docs/cloud-deployment.md`](cloud-deployment.md) — production
>   deployment guide.

---

## 1 · One-line summary

OmniCode-MCP is a **local-first, optionally cloud-hosted Codebase
Intelligence Layer** that any AI editor can call. It does **not**
compete with Cursor / Claude Code / Aider; it makes them more
accurate, more token-efficient, and safer when they touch a
real codebase.

---

## 2 · Deployment posture

| Mode | Where code lives | What this server does | Default flag preset |
|---|---|---|---|
| **local** | This machine | Index, search, LSP, edit | writes ON, apply ON |
| **cloud** | This machine (mirror) | Index, search, LSP, edit | writes OFF, apply OFF |
| **hybrid** | User's local box | Index + search + memory + graph | writes ON via agent, apply OFF |

CLI: `omnicode serve --mode local|cloud|hybrid` plus `--headless`,
`--host`, `--port`.

---

## 3 · The eight capabilities (from architecture-v2 §17)

The "Codebase Intelligence Layer" promise is exactly eight things, all
shipped and all reachable through three converging entry points
(`/capabilities`, `POST /intelligence/context`, MCP `omni_intelligence`).

| # | Capability | Module | Highlights |
|---|---|---|---|
| 1 | Code understanding | `omnicode/ast_engine/` | tree-sitter for py / js / ts / cpp / java / go / rust; outline + symbols + imports modes |
| 2 | Structured context compression | `omnicode/llm/token_manager.py` | comment strip, function fold, priority-based pruner |
| 3 | Search & references | `omnicode/search/` + LSP bridge | semantic + symbol + text + hybrid RRF; `why_matched` tags on every result |
| 4 | Call-graph impact | `omnicode_core/graph/impact.py` | BFS blast radius, callers/callees, entry points, dead symbols, related tests, risk rating |
| 5 | Safe patch operations | `omnicode_core/edit/patch.py` | preview → validate → apply → rollback with snapshots; sessions persisted |
| 6 | Memory advisory | `omnicode_core/memory/advisory.py` | multi-angle recall (file / symbol / task / error / git diff) with confidence + signals |
| 7 | Debug console | `templates/` + WebSocket logs | search debug, edit sessions, provider mgmt, code graph viewer (SVG / canvas / WebGL) |
| 8 | Optional LLM enhancement | `omnicode/llm/router.py` | LiteLLM-backed multi-provider routing; opt-in via `pip install omnicode-mcp[llm]` |

---

## 4 · REST API surface

> All endpoints below are served by the FastAPI app at port `6789`
> (default). See `api/v1/routers/__init__.py` for the registration
> list — there are 19 routers in total.

### Core read / search / index
* `GET  /health` — liveness probe (always public).
* `POST /search` — hybrid / semantic / symbol / text search.
* `POST /search/index?force=` — full / incremental rebuild.
* `POST /search/update_file?file_path=` — re-index a single file from disk.
* `POST /search/text` — text-only matcher with regex / case-sensitive options.
* `POST /read` — multi-mode file read:
  `full | outline | symbols | imports | diagnostics |
  relevant_chunks (needs ?query=) | tests`.
* `GET  /read/{path:path}` — convenience GET form.

### Capabilities & composer
* `GET  /capabilities` — eight-capability fingerprint of this deployment.
* `POST /intelligence/context` — single-call multi-capability composer.

### Call-graph & impact
* `GET  /graph/impact?symbol=&depth=` — blast radius BFS.
* `GET  /graph/entrypoints?symbol=` — top-level callers reaching a symbol.
* `GET  /graph/dead?max_files=` — symbols with 0 callers.
* `GET  /graph/related-tests?symbol=` — heuristic + reachability.
* `GET  /graph/risk?symbol=` — low / medium / high rating with reasons.
* `POST /project/graph/...` — full graph builder + visualisation feed.

### Safe-patch pipeline
* `POST /patch/preview` — diff-only render.
* `POST /patch/validate` — static analysis on the would-be result.
* `POST /patch/apply` — snapshot + write; gated by
  `OMNICODE_READ_ONLY` and `OMNICODE_ALLOW_APPLY_PATCH`.
* `POST /patch/rollback?session_id=` — restore a snapshot.
* `POST /patch/explain` — human-readable change summary.
* `GET  /patch/sessions?limit=` — recent sessions.
* `GET  /patch/sessions/{id}` — full session with diff.

### LSP bridge
* `GET  /lsp/status` — which language servers are alive.
* `POST /lsp/definition`, `/lsp/references`, `/lsp/hover`,
  `/lsp/rename` (Wave 1).
* `GET  /lsp/symbols/{path}`, `/lsp/workspace-symbols?query=`,
  `/lsp/diagnostics/{path}`.

### Memory
* `POST /memory/store`, `/memory/search`, `/memory/dedupe`.
* `GET  /memory/context`, `/memory/list`, `/memory/stats`.
* `POST /memory/advisory` — proactive recall (Wave 1).

### Files & edits
* `POST /file_operations` — read / write / edit / create / delete.
* `POST /edit` — orchestrated AI edit pipeline (uses LLM if available).
* `POST /write` — guarded write with diagnostics.
* `POST /guard` — run ruff / eslint / cppcheck on a path.

### Git context
* `POST /git` — log / status / diff / commit / branches.
* `POST /session` — Git-backed session checkout.
* `GET  /git/history?file_path=` — risk-aware history per file.
* `GET  /git/issues?max_commits=` — extracted issue/PR refs.

### Project
* `POST /project` — file-tree, dependencies, metadata.
* `POST /directory` — directory listing with metadata.
* `POST /fs` — filesystem-style listing for the Web Console.

### Working directory & workspaces
* `GET  /working-directory`, `PUT /working-directory`,
  `POST /working-directory/validate`.
* `GET  /workspaces`, `GET /workspaces/active`.
* `POST /workspaces`, `DELETE /workspaces/{id}`.
* `PUT  /workspaces/{id}/activate`, `/workspaces/{id}/rename`.

### Provider catalog (LLM, optional)
* `GET  /providers`, `POST /providers`, `PUT /providers/{name}`,
  `DELETE /providers/{name}`.
* `POST /providers/{name}/test`.

### Admin (multi-user RBAC)
* `GET / POST    /admin/users`, `PUT /admin/users/{u}/role`,
  `DELETE /admin/users/{u}`.
* `GET / POST    /admin/tokens`, `DELETE /admin/tokens/{hash}`.

### Local-agent (Wave 2 W2-2)
* `POST   /index/upsert-file` — agent uploads a file body.
* `POST   /index/upsert-batch` — debounced burst upload.
* `DELETE /index/file` — agent reports a deletion.
* `GET    /index/sync-status` — what the server has indexed.
* `GET    /index/stats` — alias of `/sync-status`.

### Logs
* `WS  /ws/logs` — live log stream for the Web Console.
* `POST /logs` — submit / clear / fetch with filters.

---

## 5 · MCP tools (stdio + SSE + streamable-http)

`mcp_server.py` registers **23 tools** when `OMNICODE_MCP_TOOLS=all`
(default). The set splits into:

| Tier | Tools | When to register |
|---|---|---|
| **High-level** (6 + 1 + 1) | `omni_search`, `omni_read`, `omni_edit`, `omni_analyze`, `omni_memory`, `omni_context`, `omni_intelligence`, `discover_tools` | always for `all`, only set for `core` |
| **Legacy** (16) | `search_tool`, `read_code_tool`, `edit_file`, `write_tool`, `file_tool`, `git_tool`, `session_tool`, `memory_tool`, `project_context_tool`, `list_file_symbols_tool`, `read_symbol_from_database`, `project_structure_tool`, `list_directory_tool`, `show_directory_tree`, `code_analysis_tool`, `execute_tool` | always for `all`, only set for `legacy` |

Toggle at startup with `OMNICODE_MCP_TOOLS=core|legacy|all` (Wave 1).

Transports:
* `python mcp_server.py` (stdio, default — what AI editors spawn).
* `python mcp_server.py --transport sse --port 6790 --auth required`.
* `python mcp_server.py --transport streamable-http --auth auto`.

Bearer-token gate (Wave 2 W2-5) honours both `OMNICODE_API_KEY` and
RBAC tokens; refuses to start when `--auth required` and no source
is configured.

---

## 6 · Security & multi-user

| Layer | What it does | Toggle |
|---|---|---|
| **Path sandbox** | Rejects `..`, absolute paths, symlinks-out | always on |
| **Read-only mode** | Blocks all writes except an allow-list (search, composer, patch preview/validate/explain, /admin/users bootstrap) | `OMNICODE_READ_ONLY=true` |
| **Apply-patch gate** | Returns 403 on `/patch/apply` & `/patch/rollback` | `OMNICODE_ALLOW_APPLY_PATCH=false` |
| **Legacy single-key auth** | `X-API-Key` or `Bearer <token>` middleware | `OMNICODE_API_KEY=…` |
| **Multi-user RBAC** | SQLite-backed users + tokens; admin / editor / viewer | bootstrap via `POST /admin/users` |
| **MCP-over-HTTP auth** | Same gate, applied to FastMCP's SSE / streamable-http apps | env or `--auth required` |
| **Provider key encryption** | Fernet-encrypted in `providers.db` | always on, master key from env |

User store: `~/.kiro/codebase-mcp/users.db`.
Provider store: `~/.kiro/codebase-mcp/providers.db`.
Workspace bookmarks: `~/.kiro/codebase-mcp/workspaces.json`.

---

## 7 · Configuration

Three sources, highest wins:
1. **CLI flags** — e.g. `--mode cloud --port 8765 --headless`.
2. **Process env vars** — every Settings field has a matching env name.
3. **`omnicode.toml`** — see `omnicode.example.toml` for the schema.
4. (Pydantic defaults.)

The TOML loader (`omnicode_core/config/toml_loader.py`) injects
sections into env vars via `setdefault` so existing env wins. Sections
mapped today:

```
[server]    mode, host, port, auth
[workspace] root, read_only
[features]  web_console, mcp_http, llm_router, lsp, memory, safe_edit
[index]     incremental, embedding_device, embedding_model
[security]  api_key, allow_apply_patch, allow_shell, mcp_tools, require_api_key
[agent]     remote, token, debounce_ms
[env]       passthrough — verbatim env-var overrides
```

---

## 8 · Embedding backends

`OMNICODE_EMBEDDING_BACKEND` selects the runtime backend:

| Value | Behaviour | Required env |
|---|---|---|
| `local` | Offline `sentence-transformers` (default) | `EMBEDDING_MODEL` (model name) |
| `remote` | OpenAI-compatible `/embeddings` | `OMNICODE_EMBEDDING_REMOTE_URL`, `OMNICODE_EMBEDDING_REMOTE_KEY` |
| `hybrid` | Local for indexing, remote for query-time | both of the above |

Falls back to local on remote failure.

---

## 9 · Workspaces & multi-project

`omnicode_core/workspace/registry.py` keeps a JSON-backed bookmark
list at `~/.kiro/codebase-mcp/workspaces.json`. Switching the active
workspace flips `WORKING_DIR` in-process and the response carries
`requires_restart=true` because heavy services (FAISS, etc.) need a
fresh process to fully re-init.

REST surface listed under §4.

---

## 10 · Local agent (W2-2)

The hybrid story. A small watcher on the user's machine pushes file
bodies to a remote OmniCode server so embedding / search / memory /
graph all happen on the beefy box without it ever touching the real
tree.

Components:
* `omnicode_adapters/agent/client.py` — `AgentClient` (stateless httpx,
  retry, exclude/binary/oversize filter).
* `omnicode_adapters/agent/watcher.py` — `Watcher` (debounced
  `watchfiles` loop, polling fallback when watchfiles isn't installed).
* `omnicode_adapters/cli/commands/agent_cmd.py` + `omnicode agent`
  subcommand.

Flow:
```
local                      remote OmniCode
─────                      ───────────────
omnicode agent  ──────►  POST /index/upsert-batch
                          POST /index/upsert-file
                          DELETE /index/file
                          GET  /index/sync-status
```

Patches still apply locally — the agent does NOT receive patches from
the remote in this iteration. Pull-mode is parked.

---

## 11 · Web Console

Mounted at `/` when not in headless mode. Front-end lives in
`templates/` (vanilla JS + Tailwind, dark-mode aware, EN / 中文 i18n).

Key pages:

* **Dashboard** — service status, index stats, recent edit sessions.
* **Search Debug** — query box with `why_matched` tags and snippet
  preview.
* **Code Graph Viewer** — interactive call / inheritance graph with
  three renderer tiers (SVG ≤1500 nodes → 2D canvas ≤5000 →
  **WebGL2** >5000), cluster colouring, hit-test, pan / zoom.
* **Provider management** — CRUD + Test for the LLM router providers.
* **Memory viewer** — search + filter recorded memories.
* **Patch sessions** — list + diff inspector (Wave 2 W2-6 will add
  inline impact + advisory drawers).
* **Working directory & workspaces** — switch and rename bookmarks.
* **Live logs** — WebSocket-backed tail.

---

## 12 · CLI

```bash
omnicode init                     # write .data/ skeleton
omnicode index [--force]          # incremental / full rebuild
omnicode status                   # via /health
omnicode serve [--headless] [--mode local|cloud|hybrid] [--host] [--port] [--reload]
omnicode dev                      # console + reload
omnicode mcp                      # stdio MCP for AI editors
omnicode agent [--remote URL] [--token TOK] [--workspace .] [--debounce-ms N]
omnicode doctor                   # python / LSP / models / ports check
```

Run-helpers under `scripts/` (`run.bat`/`.sh`, `run-dev.bat`/`.sh`,
`test.bat`/`.sh`, `lint.bat`/`.sh`).

---

## 13 · Persistence

| Path | Holds |
|---|---|
| `<wd>/.data/vector_store.faiss` | FAISS index of code chunks |
| `<wd>/.data/vector_store.db`    | chunk metadata (SQLite) |
| `<wd>/.data/file_tracker.db`    | mtime / hash for incremental indexing |
| `<wd>/.data/snapshots/`         | pre-apply file backups |
| `<wd>/.data/edit_sessions/`     | JSON session records |
| `<wd>/.data/selections.db`      | selected-text history |
| `~/.kiro/codebase-mcp/providers.db`  | encrypted provider keys |
| `~/.kiro/codebase-mcp/users.db`      | RBAC users + token hashes |
| `~/.kiro/codebase-mcp/workspaces.json` | workspace bookmarks |

---

## 14 · Engineering & quality

* **Tests**: `pytest tests/` — currently **378 passed, 2 skipped**.
  Unit tests under `tests/unit/` (≈30 files), integration under
  `tests/integration/` (composer + REST + edit pipeline + issue
  linker).
* **Lint**: `ruff check omnicode omnicode_core omnicode_adapters api core tests`
  — clean.
* **CI**: `.github/workflows/ci.yml` — Python 3.11 / 3.12 matrix,
  ruff lint, pytest, Docker build smoke (only on push to `main`).
* **Container**: `Dockerfile` + `docker-compose.yml` for local;
  `deploy/docker-compose.cloud.yml` + `deploy/Caddyfile` for cloud.
* **systemd**: `deploy/omnicode.service` and `deploy/omnicode-mcp.service`
  with hardening flags (`ProtectSystem=strict`, `ReadWritePaths`
  scoped, `MemoryMax`).

---

## 15 · Module map (`omnicode_core/` v2 layer)

```
omnicode_core/
├── auth/             # multi-user + RBAC SQLite store
├── config/
│   ├── features.py   # feature flags
│   └── toml_loader.py
├── edit/
│   └── patch.py      # PatchManager (preview/validate/apply/rollback)
├── embeddings/
│   └── backend.py    # local / remote / hybrid embeddings
├── graph/
│   └── impact.py     # ImpactAnalyzer (7 public methods)
├── index/
│   └── file_tracker.py  # incremental index bookkeeping
├── intelligence/
│   └── composer.py   # eight-capability orchestrator
├── lsp/
│   └── bridge.py     # LSP JSON-RPC client (5 servers)
├── memory/
│   └── advisory.py   # MemoryAdvisor
├── security/
│   └── sandbox.py    # path traversal guard
└── workspace/
    └── registry.py   # workspace bookmark store
```

```
omnicode_adapters/
├── agent/            # local-side file-sync (W2-2)
├── cli/              # omnicode CLI subcommands
└── mcp_server/
    ├── high_level_tools.py   # 6 + 1 omni_* tools
    └── http_auth.py          # MCP-over-HTTP bearer gate (W2-5)
```

`omnicode_core` MUST NOT depend on Web UI or specific LLM providers —
adapters call core, never the reverse.

---

## 16 · Roadmap status

| Wave | Status | What |
|---|---|---|
| P0 | ✅ done | core/adapter split, headless mode, LSP bridge, incremental index, patch ops, MCP tool slim, structured read modes |
| P1 | ✅ done | search rerank scaffolding, memory advisory, impact analysis, edit-session, search debug page, API key auth, Docker compose, GH Actions |
| P2 | ✅ done | cloud / hybrid / local modes, MCP-over-HTTP, multi workspace, RBAC, WebGL graph, multi embedding models |
| §17 final | ✅ done | composer assembly + capability fingerprint |
| Wave 1 audit | ✅ done | sandbox, read-only, why_matched, REST exposure for impact + advisory, LSP rename, modes flag, MCP slim |
| Wave 2 W2-1 / W2-3 / W2-5 | ✅ done | TOML config, HTTPS+systemd, MCP auth |
| Wave 2 W2-2 | ✅ done | local agent file-sync |
| Wave 2 W2-4 | ⏳ planned | master-key & token rotation |
| Wave 2 W2-6 | ⏳ planned | Web Console edit-session + impact pages |
| Wave 2 W2-7 | ⏳ planned | LSP fleet expansion (ruby / php / java / kotlin / c#) |
| Wave 2 W2-8 | ⏳ planned | thin VS Code extension |
| Wave 2 W2-9 | ⏳ planned | cross-encoder reranker |
| Wave 2 W2-10 | ⏳ planned | per-workspace FAISS sharding |
| Out of scope | ❌ never | full AI editor, self-built Agent framework, SaaS billing |

---

## 17 · Where to look first

* New to the project? Start with the README (§ what / why / quick
  start), then `docs/architecture-v2.md` for the long-form story.
* Deploying for real? Read `docs/cloud-deployment.md` end-to-end.
* Writing an AI editor that calls into OmniCode? You only need three
  endpoints:
  1. `GET /capabilities` — discover what's online.
  2. `POST /intelligence/context` — pull a structured, token-budgeted
     dossier in one round-trip.
  3. `POST /patch/preview` + `POST /patch/apply` (or local apply via
     the same payload) — make the change.
  Everything else is an optimisation.

---

*Generated 2026-05-28 from the live `foolkking/main` branch.*
