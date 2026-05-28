# API Reference

> Generated against `foolkking/main` head `096edb4`. The live OpenAPI
> schema is always at `http://<host>:<port>/docs` and `/openapi.json`
> when the server is running.
>
> Every successful response uses the envelope:
>
> ```json
> { "success": true, "result": { ... }, "timestamp": "..." }
> ```
>
> Every error response is:
>
> ```json
> { "success": false, "error": "<string>", "timestamp": "..." }
> ```

---

## Table of contents

- [Authentication headers](#authentication-headers)
- [Capabilities and intelligence layer](#capabilities-and-intelligence-layer)
- [Search and indexing](#search-and-indexing)
- [File reading](#file-reading)
- [Patch / edit pipeline](#patch--edit-pipeline)
- [LSP bridge](#lsp-bridge)
- [Call-graph and impact](#call-graph-and-impact)
- [Memory](#memory)
- [Workspaces](#workspaces)
- [Local agent (hybrid mode)](#local-agent-hybrid-mode)
- [Admin (multi-user RBAC)](#admin-multi-user-rbac)
- [Providers (LLM, optional)](#providers-llm-optional)
- [Working directory](#working-directory)
- [Git context](#git-context)
- [Project / directory / fs browser](#project--directory--fs-browser)
- [Logs and monitoring](#logs-and-monitoring)
- [MCP tools](#mcp-tools)

---

## Authentication headers

Two equivalent ways to present a token:

```http
X-API-Key: <token>
Authorization: Bearer <token>
```

Public paths bypass auth: `/health`, `/docs`, `/redoc`,
`/openapi.json`, `/favicon.ico`.

When **no** auth is configured (no `OMNICODE_API_KEY`, no RBAC users)
every request passes through. The first `POST /admin/users` is always
allowed so a fresh install can bootstrap an admin.

---

## Capabilities and intelligence layer

### `GET /capabilities`

Returns the eight-capability fingerprint of this deployment. AI
editors call this once at startup to learn what's online.

```json
{
  "result": {
    "version": "1.0.0-rc1",
    "total": 8,
    "available": 8,
    "capabilities": [
      { "capability": "code_understanding", "available": true,  "detail": "...", "backend": "tree-sitter" },
      { "capability": "context_compression", "available": true, "detail": "...", "backend": "..." },
      { "capability": "search",              "available": true, "detail": "...", "backend": "local-sentence-transformers" },
      { "capability": "impact_analysis",     "available": true },
      { "capability": "safe_patch",          "available": true },
      { "capability": "memory_recall",       "available": true },
      { "capability": "debug_console",       "available": true },
      { "capability": "llm_enhancement",     "available": false }
    ]
  }
}
```

### `POST /intelligence/context`

Single-call multi-capability composer. Inputs:

| Field | Type | Default | Description |
|---|---|---|---|
| `task` | string | null | Free-form task description |
| `file_path` | string | null | Repo-relative path under inspection |
| `symbol` | string | null | Symbol the editor is focused on |
| `query` | string | null | Free-text search query |
| `max_search_results` | int | 5 | Top-K for the search step |
| `impact_depth` | int | 2 | BFS depth for blast radius |
| `memory_max` | int | 5 | Max memories to recall |
| `token_budget` | int | 4096 | Soft cap; over-budget snippets get truncated |
| `include_git_history` | bool | true | Run the git-history risk pass |
| `include_impact` | bool | true | Run impact BFS |
| `include_memory` | bool | true | Run memory advisory |

Returns a structured `IntelligenceContext`:

```jsonc
{
  "result": {
    "request": { /* echo of inputs */ },
    "capability_status": [ /* same shape as /capabilities */ ],
    "code_understanding": { "file": "...", "language": "python", "symbols": [...] },
    "search": { "query": "...", "result_count": 3, "results": [...] },
    "impact": { "total_blast_radius": 12, "affected_symbols": [...], "files_involved": [...] },
    "memory": { "advisory": "...", "memories_used": [...], "confidence": 0.8 },
    "git_history": { "risk_level": "low", "risk_score": 0.18, "co_changed_files": [...] },
    "advisories": [ "⚠️ Impact: changing this symbol may affect 12 symbols across 4 files." ],
    "token_estimate": 1147,
    "token_budget": 4096,
    "elapsed_ms": 891,
    "errors": {}
  }
}
```

Per-capability failures land in `errors[<capability>]` so a partial
result is always returned.

---

## Search and indexing

### `POST /search`

```jsonc
// Request
{
  "query": "create_app",
  "search_type": "semantic",       // semantic | text | symbol_exact | fuzzy_symbol
  "max_results": 10,
  "min_score": 0.5,
  "file_pattern": "*.py",          // optional
  "symbol_type": "function"        // optional
}
```

Each result row carries a `why_matched` array
(e.g. `["semantic", "symbol:contains"]` or `["symbol:exact",
"reranked"]`).

### `POST /search/text`

Substring scan with optional regex / case-sensitive flag.

### `POST /search/symbols`

Fuzzy / exact symbol-name search via the indexed metadata column.

### `GET /search/symbols/{file_path:path}`

List symbols in a single file (line ranges, signatures, parent class).

### `GET /search/symbols/graph`

Cross-file call graph, with degree-based hub ranking.

### `GET /search/inheritance`

Subclass → base graph across the workspace.

### `POST /search/symbols/relations`

Per-symbol callers + callees lookup.

### `POST /search/index?force=`

Run the indexer. Default is incremental; `?force=true` rebuilds from
scratch.

### `POST /search/update_file?file_path=`

Re-index a single file from disk.

---

## File reading

### `POST /read`

Multi-mode read with token efficiency:

| Mode | Description | Typical tokens vs full |
|---|---|---|
| `full` | Whole file (default) | 100% |
| `outline` | Function/class signatures + first docstring line | 5 – 15% |
| `symbols` | Symbol list only (name, kind, lines) | 1 – 5% |
| `imports` | Import / require / include lines only | < 1% |
| `diagnostics` | Lint output only (ruff / eslint / etc.) | varies |
| `relevant_chunks` | Top-K semantic chunks of this file vs `query` | 10 – 30% |
| `tests` | Candidate test files for this file | tiny |

Sandbox check happens **before** mode dispatch — `../../../etc/passwd`
returns 403 regardless of mode.

```jsonc
// Request (relevant_chunks needs a query)
{
  "file_path": "main.py",
  "mode": "outline",
  "with_line_numbers": true,
  "query": null
}
```

### `GET /read/{file_path:path}`

Convenience GET form for the `full` mode.

---

## Patch / edit pipeline

### `POST /patch/preview`

Renders a unified diff without touching disk.

```jsonc
{ "file_path": "main.py", "content": "<full new file body>" }
```

### `POST /patch/validate`

Run static analysis (ruff / eslint / cppcheck) on the would-be result.

### `POST /patch/apply`

Snapshot + write. Returns a `session_id` for rollback.

**Gated** by:
- `OMNICODE_READ_ONLY=true` → 403
- `OMNICODE_ALLOW_APPLY_PATCH=false` → 403

### `POST /patch/rollback?session_id=`

Restore the file from its pre-edit snapshot. Same gates as `/apply`.

### `POST /patch/explain`

Human-readable summary of what a patch does.

### `GET /patch/sessions?limit=`

Recent edit sessions.

### `GET /patch/sessions/{session_id}`

Full session with diff + checks_before / checks_after.

---

## LSP bridge

10 supported languages: pyright (Python), tsserver (TS/JS), gopls
(Go), rust-analyzer (Rust), clangd (C/C++), solargraph (Ruby),
intelephense (PHP), jdtls (Java), kotlin-language-server, omnisharp
(C#).

### `GET /lsp/status`

Which language servers are alive / available.

### `POST /lsp/definition` · `/lsp/references` · `/lsp/hover` · `/lsp/rename`

```jsonc
// Query parameters
?file=src/main.py&line=42&col=10                // 0-indexed
?include_declaration=true                       // /references only
?new_name=newSymbol                             // /rename only
```

### `GET /lsp/symbols/{file_path:path}`

Document symbols via LSP (more accurate than tree-sitter for
cross-file references).

### `GET /lsp/workspace-symbols?query=`

Workspace-wide symbol search.

### `GET /lsp/diagnostics/{file_path:path}`

Triggers `didOpen` and waits ~2 s for the language server to push
diagnostics back.

---

## Call-graph and impact

All endpoints are GET with `?symbol=...&depth=2&max_files=200`.

### `GET /graph/impact`

BFS the call graph from `symbol`. Returns affected (callees) +
dependents (callers) symbols, files involved, total blast radius.

### `GET /graph/entrypoints`

Top-level 0-caller roots that reach `symbol`.

### `GET /graph/dead`

Symbols with 0 callers (potential dead code). Excludes entry-point
patterns (`main`, `app`, `__init__`, `setup`, `teardown`,
`conftest`) and `test_*` functions.

### `GET /graph/related-tests`

Combines filename heuristics with call-graph reachability. Returns
test files plus ready-to-run `pytest` commands.

### `GET /graph/risk`

low / medium / high rating with reasons. Use to gate confirmation
modals.

### `POST /project/graph/...`

Larger graph builder + visualisation feed (drives the Web Console
graph viewer).

---

## Memory

### `POST /memory/store`

Store a memory with category + content. Auto-deduplicates via SHA1
fingerprint.

### `POST /memory/search`

Hybrid keyword + semantic search with `min_score`.

### `POST /memory/dedupe`

Collapse duplicate active memories.

### `POST /memory/advisory`

Proactive recall (Wave 1). Multi-angle search across file path,
symbol, task, error message, git diff.

```jsonc
{
  "file_path": "main.py",
  "symbol": "create_app",
  "task": "explain create_app",
  "error_message": null,
  "git_diff": null,
  "max_memories": 5,
  "max_tokens": 800
}
```

Returns:

```jsonc
{
  "advisory": "Relevant past lessons:\n1. ...",
  "memories_used": [12, 47],
  "confidence": 0.78,
  "signals_matched": ["file_path", "symbol", "task"],
  "memory_count": 5
}
```

### `GET /memory/list` · `/memory/stats` · `/memory/{id}` · `DELETE /memory/{id}`

Standard CRUD plus stats.

---

## Workspaces

User-level bookmark store at
`~/.kiro/codebase-mcp/workspaces.json`. Each entry is `(id, name,
path)` with one `active` flag.

### `GET /workspaces` · `GET /workspaces/active`

List or fetch the active.

### `POST /workspaces`

```jsonc
{ "name": "my-app", "path": "C:/Users/me/projects/my-app", "set_active": false }
```

### `DELETE /workspaces/{id}`

Removes the bookmark **and** drops the per-workspace FAISS shard from
`<wd>/.data/shards/<id>/`.

### `PUT /workspaces/{id}/activate`

Flip the active flag and update `WORKING_DIR`.

### `PUT /workspaces/{id}/rename`

```jsonc
{ "name": "new-name" }
```

---

## Local agent (hybrid mode)

Five endpoints under `/index/*` for the `omnicode agent` watcher to
push file bodies to a remote OmniCode server.

### `POST /index/upsert-file`

```jsonc
{
  "file_path": "src/main.py",
  "content": "<full UTF-8 body>",
  "content_hash": "<optional sha256>"
}
```

### `POST /index/upsert-batch`

Same shape but `{ "files": [<UpsertFile>, ...] }`. Returns per-file
indexed counts plus per-file errors.

### `DELETE /index/file`

```jsonc
{ "file_path": "src/old.py" }
```

### `GET /index/sync-status` · `GET /index/stats`

What the server has indexed (file count, chunks, embedding model
name, working_dir).

All five endpoints respect the path sandbox.

---

## Admin (multi-user RBAC)

`SQLite`-backed at `~/.kiro/codebase-mcp/users.db`. Three roles:
`admin` (everything), `editor` (read + write), `viewer` (read-only).

### `GET /admin/users` · `POST /admin/users`

```jsonc
// POST body
{ "username": "alice", "role": "admin" }
```

The **first** `POST /admin/users` call is always allowed (bootstrap)
and returns a `bootstrap_token` field — save it; you'll need it for
every subsequent admin call.

### `PUT /admin/users/{username}/role` · `DELETE /admin/users/{username}`

### `GET /admin/tokens?username=` · `POST /admin/tokens`

```jsonc
// Issue body
{ "username": "alice", "label": "laptop", "expires_in_days": 90 }
```

Returns `token` (plaintext, **shown once only**) plus `token_hash`.

### `DELETE /admin/tokens/{token_hash}`

### `DELETE /admin/users/{username}/tokens`

Revoke every token of a user in one call. For the
"departing-employee" path.

---

## Providers (LLM, optional)

Only loaded when `omnicode-mcp[llm]` extras are installed.

### `GET /providers` · `POST /providers` · `PUT /providers/{name}` · `DELETE /providers/{name}`

Provider records: `name`, `model`, `api_key` (encrypted at rest),
`api_base`, `provider_type`, `group`, `extra_headers`, `enabled`,
`built_in`.

### `POST /providers/{name}/test`

Sends a 1-token health-check prompt with a 20 s timeout. Returns
`{"ok": bool, "hint": "...", "hint_field": "<which field is wrong>"}`.

### `GET /selections` · `PUT /selections`

Role → provider assignments for `default / quality / cost / fastest /
edit / scan / review / summary / chat`.

---

## Working directory

### `GET /working-directory`

Active project info + service status.

### `PUT /working-directory`

Switch project. Auto-loads the new `.data/` so memories and indices
persist across switches.

### `POST /working-directory/validate`

Validate a path without switching to it.

---

## Git context

### `POST /git`

Operations: `log` / `status` / `diff` / `commit` / `branches`. Body
shape:

```jsonc
{ "operation": "log", "max_results": 20, "file_path": "..." }
```

### `POST /session`

Git-backed session checkout. Operations: `start` / `end` / `switch` /
`merge` / `delete` / `current` / `list`.

### `GET /git/history?file_path=&max_commits=`

Risk-aware per-file history. Returns
`{ risk_score, risk_level, defensive_patches, co_changed_files,
hardening_count, related_issues }`.

### `GET /git/issues?max_commits=&enrich=`

Extracted issue/PR references. With `enrich=true` and
`GITHUB_TOKEN`, fetches issue titles / status from GitHub.

---

## Project / directory / fs browser

### `POST /project`

File-tree, dependencies, metadata.

### `POST /directory`

Directory listing with metadata.

### `POST /fs`

Filesystem-style listing for the Web Console (with deny-list
enforcement).

### `GET /code-graph` · `GET /inheritance`

Pre-built graph payloads for the visualiser.

---

## Logs and monitoring

### `WS /ws/logs`

Live log stream (auto-backfill on connect).

### `POST /logs`

```jsonc
// Submit
{ "operation": "submit", "level": "info", "message": "...", "component": "..." }
// Or fetch with filters
{ "operation": "fetch", "level": "warn", "limit": 100, "since": "2026-05-28T00:00:00Z" }
// Or clear
{ "operation": "clear" }
```

### `GET /monitoring/performance`

Process-level metrics.

### `GET /health`

Liveness probe — always public.

---

## MCP tools

Registered by `mcp_server.py` via `FastMCP`. Tunable surface:

| `OMNICODE_MCP_TOOLS` | Tools registered | Total |
|---|---|---|
| `core` (default) | omni_search, omni_read, omni_edit, omni_analyze, omni_memory, omni_context, omni_intelligence, discover_tools | **8** |
| `all` | core + 16 legacy | 24 |
| `legacy` | 16 legacy only | 16 |

### High-level tools

| Tool | Notes |
|---|---|
| `omni_search(query, mode="auto")` | semantic / symbol / text / git / memory in one |
| `omni_read(file, mode="outline")` | wraps the multi-mode `/read` endpoint |
| `omni_edit(action="preview", ...)` | preview / validate / apply / rollback |
| `omni_analyze(symbol, analysis="impact")` | callers / callees / impact / risk |
| `omni_memory(action="search", ...)` | store / search / context / advisory |
| `omni_context(file, symbol)` | full context dossier for one file+symbol |
| `omni_intelligence(task, file, symbol, query, ...)` | full eight-capability composer |
| `discover_tools(query)` | runtime catalog with filters |

### Transports

```bash
# stdio (default — what AI editors spawn)
omnicode mcp

# SSE for remote MCP clients
python mcp_server.py --transport sse --port 6790 --auth required

# Streamable HTTP
python mcp_server.py --transport streamable-http --auth auto
```

The `--auth required` flag refuses to start when no auth source is
configured (neither `OMNICODE_API_KEY` nor any RBAC user). Runtime
gate honours both sources.
