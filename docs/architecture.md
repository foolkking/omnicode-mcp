# Architecture

> Single source of truth for *what's in the box*. Combines the design
> rationale (formerly `architecture-v2.md`), the feature inventory
> (formerly `features.md`), and the §1-§17 audit (formerly
> `final-audit.md`).

---

## Table of contents

- [Project identity](#project-identity)
- [Three deployment modes](#three-deployment-modes)
- [Capability-aware r60 contract](#capability-aware-r60-contract)
- [Deterministic index and search](#deterministic-index-and-search)
- [The eight capabilities](#the-eight-capabilities)
- [Module map](#module-map)
- [Persistence layout](#persistence-layout)
- [Engineering & quality](#engineering--quality)
- [Where to look first](#where-to-look-first)

---

## Project identity

OmniCode-MCP is a **local-first, optionally cloud-hosted Codebase
Intelligence Layer** that any AI editor can call.

### Core value proposition

1. Make AI **understand** the codebase (AST + LSP + call graph + git
   history).
2. Stop AI from re-reading whole files (`outline / symbols /
   relevant_chunks / diagnostics / imports / tests` modes).
3. Give AI **context** before it edits (symbols, references,
   diagnostics, callers, callees, recent commits, past memories,
   blast radius).
4. Make every edit **reviewable** — preview → validate → apply →
   rollback, with snapshot-backed sessions.
5. Stay **local-first, controllable, auditable** — fully offline by
   default, API keys encrypted at rest, no cloud dependency unless
   the operator opts in.

### What we're NOT

- ❌ Not an AI editor — Cursor / Continue / Claude Code / Aider do
  that. We're the layer they call.
- ❌ Not a VS Code replacement.
- ❌ Not a self-built Agent framework — LangGraph / autogen do that
  better.
- ❌ Not a multi-tenant SaaS (near term). Single-tenant self-host is
  the deployment model.

### Architectural rules

These are non-negotiable:

1. `omnicode_core/` does not import from Web UI or specific LLM
   providers. Adapters call core, never the reverse.
2. MCP / HTTP / Web Console are **adapters** sharing the same
   service singletons in `core/dependencies.py`.
3. LLM features are **optional** (`pip install omnicode-mcp[llm]`).
4. Caller-supplied paths go through the sandbox before any
   filesystem call.
5. State writes go through the read-only middleware; new mutating
   endpoints inherit the gate automatically.
6. Per-workspace data lives in shards (`<wd>/.data/shards/<id>/`),
   never the legacy single `.data/` layout.
7. Imports stay one-way: `adapters → core → stdlib / third-party`.

---

## Three deployment modes

| Mode | Code lives | This server does | Default flag preset |
|---|---|---|---|
| **local** | This machine | Index, search, LSP, edit | writes ON, apply ON |
| **cloud** | This machine (mirror) | Index, search, LSP, ~edit~ | writes OFF, apply OFF |
| **hybrid** | User's local box | Index + search + memory + graph | writes ON via agent, apply OFF |

CLI: `omnicode serve --mode local|cloud|hybrid` plus `--headless`,
`--host`, `--port`. Hybrid means a separate
`omnicode agent` process on the user's machine pushes file bodies to
this server's `/index/*` endpoints; the user's editor still applies
patches locally.

### Workspace-aware MCP bridge

Workspace identity is explicit across MCP, agent, and HTTP:

- `X-Omnicode-Workspace: <workspace_id>` identifies the logical
  project. All paths remain workspace-relative; local absolute paths
  such as `C:\repo\src\a.py` must never be sent to cloud APIs.
- `X-Omnicode-Executor: local|remote|hybrid|auto` advertises where the
  caller expects file-bound operations to execute. The current bridge
  preserves remote proxy behaviour while making the execution policy
  auditable in logs and `omni_status`.
- `/workspaces` accepts an optional stable `workspace_id` so the local
  agent and cloud backend can agree on `repo-a` instead of relying on
  generated ids.
- `/index/*` fails closed when `X-Omnicode-Workspace` names a workspace
  that is not the active backend `WORKING_DIR`; this prevents local file
  bodies from being indexed into the wrong cloud project.

The intended hybrid path is:

1. Cloud registers `workspace_id=repo-a` for
   `/srv/omnicode/workspaces/repo-a-cache` and runs in `hybrid` mode.
2. Local `omnicode agent --workspace C:\repo --workspace-id repo-a`
   pushes file bodies and deletions to `/index/*`.
3. Local `omnicode mcp --workspace C:\repo --workspace-id repo-a
   --backend-url https://... --executor hybrid` gives the AI editor a
   stable local project root and a cloud analysis backend.

For full deployment recipes see [`deployment.md`](deployment.md).

### Runtime config and sync protocol

Hybrid MCP sessions now share one structured runtime model:

- `RuntimeConfig` is built from CLI > env > `omnicode.toml` > defaults.
  It exports the same env vars the legacy MCP server already understands,
  so the bridge can move incrementally without a flag-day rewrite.
- `LocalWorkspace` is the path authority for local files. It accepts only
  workspace-relative paths, rejects `..`, absolute paths outside the root,
  and symlink escapes.
- `LocalManifest` records local file hashes and revisions under
  `~/.omnicode/workspaces/<workspace_id>/manifest.json`.
- `SyncQueue` turns pending manifest changes into `/sync/batch` payloads.
  It reads file bytes first, then decodes as UTF-8, so Windows CRLF bytes
  hash the same way in the manifest and upload payload.
- `SyncClient` talks to `/sync/batch`, `/sync/status`, and `/sync/barrier`
  with `X-Omnicode-Workspace`, `X-Omnicode-Executor`, and optional
  `X-API-Key` headers.

The cloud sync API accepts only workspace ids, workspace-relative paths,
hashes, revisions, and UTF-8 file content. Local absolute paths are never
valid in this protocol. Accepted file content is stored in
`CloudSnapshotStore`, a content-addressed store under
`~/.omnicode/cloud-sync/workspaces/<workspace_id>/`. The index records
`accepted_revision` and `indexed_revision`; `/sync/barrier` blocks cloud
analysis when the cloud index is stale.

`HybridToolRouter` defines the MCP execution contract:

- `omni_read` and `omni_patch` are always local-authority tools.
- `omni_diagnostics` is local-first.
- `omni_search`, `omni_context`, and `omni_impact` may run in cloud only
  after the barrier revision is current.
- `omni_status` is aggregate and reports runtime, sync, route, capability,
  and agent-auto state.

LLM, embedding, and diagnostics capability selection is reported through a
separate capability contract. It derives from runtime mode flags such as
`OMNICODE_LLM_MODE`, `OMNICODE_EMBEDDING_MODE`, and
`OMNICODE_DIAGNOSTICS_MODE`; it does not silently assume cloud is available
when no backend URL is configured.

---

## Capability-aware r60 contract

r60 changes the contract from "all tools are equally ready" to "tools report
the capability state they actually have." The goal is reliable AI editor
behavior when semantic, graph, cloud, or language validators are unavailable.

The shared capability registry lives in
`omnicode_core/capabilities/registry.py`. `omni_status`, `discover_tools`,
and tool preflight checks use the same state model:

| State | Meaning |
|---|---|
| `ready` | Safe for default AI editor use |
| `partial` | Useful, but incomplete or warning-bearing |
| `degraded` | Fallback path is active; confidence is lower |
| `unavailable` | Capability is not currently usable |
| `unsupported` | Capability is not implemented for this language/environment |

Core rules:

- `omni_read(full/range/outline)` and `omni_patch` are local-authority
  operations in hybrid mode and must not fail just because cloud is down.
- `omni_search`, `omni_context`, and `omni_impact` expose freshness,
  provider, fallback, and missing-capability metadata.
- Semantic search is optional. If the embedding model or vector metadata is
  not ready, semantic providers are disabled or degraded; exact providers
  still run.
- Graph impact is optional. If a symbol is found but graph is unavailable,
  `omni_impact` returns unknown risk, low confidence, and fallback
  references/test candidates instead of reporting `symbol not found`.
- Scala and unknown-language diagnostics/validation return `unsupported` or
  `not_performed`, not a fake "passed" result.

`discover_tools()` is therefore a strategy router, not just a static list.
It recommends workflows based on current capabilities and keeps deprecated
aliases in a compatibility section.

---

## Deterministic index and search

The deterministic index is the production baseline. It uses SQLite plus
local text fallback, not a separate database service:

| Layer | Storage / provider | Purpose |
|---|---|---|
| L0 files | SQLite `files` | workspace-relative paths, hashes, language, revision |
| L1 lines | SQLite `lines` and optional FTS5 `line_fts` | exact line text search |
| L2 symbols | SQLite `symbols` | class/function/object definitions |
| Text fallback | `rg`, then Python grep | reliable exact text search when FTS is unavailable or empty |
| Semantic | FAISS vector store | optional semantic search after embedding metadata validation |

`SnapshotExactIndex` is used for cloud snapshot indexes and local workspace
exact indexes. The schema records `schema_version`, `index_kind`,
`line_fts_available`, `line_fts_reason`, and `exact_indexed_revision`.

The query planner in `omnicode_core/search/planner.py` classifies queries
into intents such as `exact_symbol`, `exact_text`, `regex_text`,
`file_path`, `semantic`, `references`, and `hybrid`. Search responses include
the query plan, provider chain, capabilities used/missing, fallback state,
warnings, and `empty_reason`.

This distinction matters:

- `ok=true, results=[]` means the selected provider really searched and
  found no matches.
- `INDEX_NOT_READY`, `TEXT_SEARCH_UNAVAILABLE`, or semantic stale/invalid
  means the provider was not usable and the caller should bootstrap or
  choose another workflow.

---

## The eight capabilities

The "Codebase Intelligence Layer" promise is exactly eight things,
implemented behind the capability-aware contract, all reachable through
three converging entry points
(`GET /capabilities`, `POST /intelligence/context`, MCP tool
`omni_context`):

| # | Capability | Module | Highlights |
|---|---|---|---|
| 1 | Code understanding | `omnicode/ast_engine/` | tree-sitter for py / js / ts / cpp / java / go / rust; outline + symbols + imports + tests + relevant_chunks + diagnostics modes |
| 2 | Structured context compression | `omnicode/llm/token_manager.py` | comment strip, function fold, priority-based pruner |
| 3 | Hybrid search | `omnicode/search/` + `omnicode_core/search/` | semantic + symbol + text + RRF; `why_matched` tags on every result; cross-encoder reranker (opt-in) |
| 4 | Call-graph impact | `omnicode_core/graph/impact.py` | BFS blast radius, callers, callees, entry points, dead symbols, related tests, low/medium/high risk rating |
| 5 | Safe patch operations | `omnicode_core/edit/patch.py` | preview → validate → apply → rollback with snapshots; sessions persisted as JSON |
| 6 | Memory advisory | `omnicode_core/memory/advisory.py` | multi-angle recall (file / symbol / task / error / git diff) with confidence + signals |
| 7 | Debug console | `templates/` + WebSocket logs | search debug, edit sessions, impact viewer, advisory drawer, code graph viewer (SVG → 2D canvas → WebGL2 tiers) |
| 8 | Optional LLM enhancement | `omnicode/llm/router.py` | LiteLLM-backed multi-provider routing; opt-in via `pip install omnicode-mcp[llm]` |

The composer (`omnicode_core/intelligence/composer.py`) runs available
capabilities inside a token budget and reports per-capability errors or
missing capabilities without pretending degraded sections are complete.

---

## Module map

```
omnicode_core/                    ← clean v2 layer (no UI / LLM deps)
├── auth/                         ← multi-user RBAC + master-key rotation
│   ├── users.py                  ← SQLite store, SHA-256 token hashes
│   ├── migrations.py             ← PRAGMA user_version runner
│   └── rotation.py               ← Fernet master-key rotation
├── config/
│   ├── features.py               ← runtime feature flags
│   └── toml_loader.py            ← omnicode.toml → env-var injector
├── edit/
│   └── patch.py                  ← PatchManager (preview / validate / apply / rollback / explain / sessions)
├── embeddings/
│   └── backend.py                ← local / remote / hybrid backends
├── graph/
│   └── impact.py                 ← ImpactAnalyzer (7 public methods)
├── index/
│   ├── file_tracker.py           ← incremental index bookkeeping
│   └── sharding.py               ← per-workspace FAISS shards (auto-migrate from legacy)
├── intelligence/
│   └── composer.py               ← eight-capability orchestrator
├── lsp/
│   └── bridge.py                 ← LSP JSON-RPC client (10 servers)
├── memory/
│   └── advisory.py               ← MemoryAdvisor — proactive recall
├── search/
│   └── reranker.py               ← cross-encoder rerank (opt-in)
├── security/
│   └── sandbox.py                ← path traversal guard
└── workspace/
    └── registry.py               ← workspace bookmark store

omnicode/                         ← legacy modules (still used; depend on LLM extras)
├── ast_engine/                   ← tree-sitter parser + chunker + call/inheritance graph
├── search/                       ← FAISS + SQLite vector store + hybrid RRF engine
├── llm/                          ← multi-provider router, provider registry, secret box
├── pipelines/                    ← AI edit pipeline (three-layer defence)
├── git_context/                  ← history risk analyzer, blame, issue linker
├── memory/                       ← memory store + dedupe + hybrid scorer
├── guard/                        ← static-analysis gate (ruff / eslint / cppcheck)
└── server/                       ← legacy MCP tool definitions

omnicode_adapters/                ← thin layer that calls core
├── agent/                        ← local file-sync watcher (hybrid mode)
│   ├── client.py                 ← stateless httpx client + retry
│   └── watcher.py                ← debounced watchfiles loop + polling fallback
├── cli/                          ← omnicode CLI subcommands
│   ├── main.py                   ← argparse entry point
│   └── commands/                 ← init / index / status / serve / mcp / agent / dev / doctor / rotate-master-key
└── mcp_server/
    ├── high_level_tools.py       ← 6 + 1 omni_* tools
    └── http_auth.py              ← MCP-over-HTTP bearer-token gate

api/v1/routers/                   ← FastAPI routers (22 total)
├── admin.py                      ← /admin/users + /admin/tokens (RBAC management)
├── agent.py                      ← /index/upsert-file etc. (hybrid mode)
├── files.py                      ← /read with 7 modes
├── git.py                        ← /git, /session, /git/history, /git/issues
├── graph.py                      ← /graph/impact + /risk + /related-tests + /entrypoints + /dead
├── intelligence.py               ← /capabilities + /intelligence/context
├── lsp.py                        ← /lsp/* bridge endpoints
├── memory.py                     ← /memory/* including /memory/advisory
├── patch.py                      ← /patch/preview + /validate + /apply + /rollback + /explain
├── search.py                     ← /search hybrid + /search/index
├── workspaces.py                 ← /workspaces CRUD
└── (...others: project, directory, fs_browser, working_directory, model providers, logs, health, static_files, guard)

core/                             ← FastAPI middlewares + dependency injection
├── dependencies.py               ← service singletons (search engine, git mgr, llm router, ast parser)
├── lifespan.py                   ← startup / shutdown lifecycle
├── auth_middleware.py            ← legacy single-key gate
├── rbac_middleware.py            ← multi-user RBAC gate
└── read_only_middleware.py       ← read-only mode gate

extensions/
└── vscode/                       ← thin VS Code extension (3 commands, ~340 LoC TS)

templates/                        ← Web Console (vanilla JS + Tailwind, EN/中文 i18n)
├── components/
│   ├── layout/sidebar.html
│   └── sections/                 ← dashboard / search / graph-viewer / impact-viewer / edit-sessions / providers / memory / logs / etc.
└── static/js/                    ← api/routes.js + components/loader.js + utils/{theme,i18n,features}.js
```

`omnicode_core` is the destination of the v2 refactor; new features
go there. `omnicode/` (singular) is the legacy tree, kept because
it owns the LLM router, search engine, AST chunker, and git
analyser — none of which warrant a rewrite right now.

---

### r60 module additions

| Path | Purpose |
|---|---|
| `omnicode_core/capabilities/registry.py` | Shared capability state for `omni_status`, `discover_tools`, and tool preflight |
| `omnicode_core/workspace/exact_index.py` | SQLite files/lines/symbols/FTS exact index and revision metadata |
| `omnicode_core/search/planner.py` | Query intent and provider-chain planning |
| `omnicode_core/search/text_grep.py` | ripgrep and Python grep exact-text fallback |
| `omnicode_core/embeddings/models.py` | Supported embedding models, cache directory, local-files-only status, pull helpers |
| `omnicode_adapters/cli/commands/models_cmd.py` | `omnicode models list/pull/status` CLI |

---

## Persistence layout

```
<wd>/                                          ← active working directory
└── .data/
    └── shards/
        ├── default/                           ← legacy single-tenant data (auto-migrated from <wd>/.data/<file>)
        │   ├── vector_store.faiss             ← FAISS index of code chunks
        │   ├── vector_store.db                ← chunk metadata (SQLite)
        │   ├── file_tracker.db                ← mtime / hash for incremental rebuild
        │   ├── snapshots/                     ← pre-apply file backups
        │   └── edit_sessions/                 ← JSON session records
        └── wk_<id>/                           ← per-workspace shard (multi-tenant)
            └── (same layout)

~/.kiro/codebase-mcp/                          ← user-level shared state
├── providers.db                               ← Fernet-encrypted provider keys
├── providers.key                              ← master Fernet key (file mode 0600)
├── users.db                                   ← RBAC users + token hashes (versioned by PRAGMA user_version)
└── workspaces.json                            ← workspace bookmarks
```

Sharding is auto-migrated on first run by
`omnicode_core/index/sharding.py::auto_migrate_legacy()` —
idempotent, refuses to overwrite an already-populated default
shard.

---

## Engineering & quality

### Tests

```bash
python -m pytest tests -q               # r60 sweep: 1117 passed, 16 skipped
python -m pytest tests/integration/test_route_regressions.py -q
python -m pytest tests/benchmarks/test_django_large_repo_hybrid.py -q -m large_repo
python -m pytest tests/benchmarks/test_kafka_large_repo_hybrid.py -q -m large_repo
```

Some tests are optional probes or large-repo benchmarks and may skip when
local prerequisites are missing. Unit tests live under `tests/unit/`,
integration tests under `tests/integration/`, and production-scale gates
under `tests/benchmarks/`.

### Lint

```bash
ruff check omnicode omnicode_core omnicode_adapters api core tests
```

Config in `pyproject.toml`. Always clean on `main`.

### CI

`.github/workflows/ci.yml` runs:

1. ruff lint (read-only check on the whole tree)
2. pytest matrix on Python 3.11 + 3.12
3. Docker image build smoke (push to `main` only)

Branch protection blocks merges until lint + tests pass.

### Container

| File | Purpose |
|---|---|
| `Dockerfile` | App image; pre-downloads embedding model |
| `docker-compose.yml` | Local dev compose |
| `deploy/docker-compose.cloud.yml` | Cloud overlay with Caddy reverse proxy |
| `deploy/Caddyfile` | TLS termination config |

### systemd

`deploy/omnicode.service` and `deploy/omnicode-mcp.service` ship
hardened defaults: `NoNewPrivileges`, `ProtectSystem=strict`,
`ReadWritePaths` scoped to the workspace, `MemoryMax=4G` on the
main service.

---

## Where to look first

* **New to the project?** Read [README.md](../README.md) →
  [usage.md](usage.md) → this doc.
* **Calling OmniCode from another tool?** Read
  [api.md](api.md). Three endpoints are enough for most use cases:
  1. `GET /capabilities` — discover what's online.
  2. `POST /intelligence/context` — pull a structured,
     token-budgeted dossier in one round-trip.
  3. `POST /patch/preview` + `POST /patch/apply` — make the
     change.
* **Deploying to a server?** Read [deployment.md](deployment.md)
  end-to-end.
* **Curious what's next?** Read [roadmap.md](roadmap.md) — the
  original P0/P1/P2 + Wave 1 + Wave 2 backlogs are all shipped;
  the file lists post-1.0 research directions.
