# Configuration

> Every knob OmniCode-MCP responds to, where it comes from, and how
> the precedence rules work.

---

## Where settings come from

Three layered sources, **highest precedence wins**:

1. **CLI flags** — `omnicode serve --mode cloud --port 8765`. Win
   over everything below.
2. **Process env vars** — every Pydantic Settings field has a
   matching env name. Set in your shell or `.env` file.
3. **TOML configuration file** — `omnicode.toml` next to where you
   launch (or `OMNICODE_CONFIG=/path/to.toml`). Loaded BEFORE the
   Settings class instantiates and folded into env via `setdefault`,
   so anything already set in env wins.
4. **Pydantic defaults** — built into `omnicode/config/settings.py`.

> **Rule of thumb**: put project-wide defaults in `omnicode.toml`,
> per-deployment secrets in env vars, one-off overrides on the CLI.

---

## TOML reference

Sample at [`omnicode.example.toml`](../omnicode.example.toml). Every
key is optional.

### `[server]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `mode` | `OMNICODE_MODE` | `local` | `local` / `cloud` / `hybrid`. Affects defaults for read-only + apply-patch flags. |
| `host` | `API_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for cloud + reverse proxy. |
| `port` | `API_PORT` | `6789` | FastAPI port. |
| `auth` | `OMNICODE_MCP_REQUIRE_AUTH` | `false` | Refuse MCP-over-HTTP startup when no auth source is configured. |

### `[workspace]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `root` | `WORKING_DIR` | `cwd` | Absolute path of the codebase. |
| `read_only` | `OMNICODE_READ_ONLY` | `false` | Block every mutating endpoint except a query-only allow-list. |

### `[features]`

Soft toggles consumed by the composer + capability fingerprint.

| Key | Env | Default |
|---|---|---|
| `web_console` | `OMNICODE_WEB_CONSOLE` | `true` |
| `mcp_http` | `OMNICODE_MCP_HTTP` | `false` |
| `llm_router` | `OMNICODE_LLM_ROUTER` | `false` |
| `lsp` | `OMNICODE_LSP` | `true` |
| `memory` | `OMNICODE_MEMORY` | `true` |
| `safe_edit` | `OMNICODE_SAFE_EDIT` | `true` |

### `[index]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `incremental` | `OMNICODE_INDEX_INCREMENTAL` | `true` | When `false` every rebuild is full. |
| `embedding_device` | `OMNICODE_EMBEDDING_DEVICE` | `cpu` | `cpu` / `cuda`. |
| `embedding_model` | `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model name. |

### `[search]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `reranker` | `OMNICODE_RERANKER` | `false` | Cross-encoder reranker (W2-9). Adds 50 – 200 ms per query. |
| `reranker_model` | `OMNICODE_RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | HuggingFace cross-encoder. |

### `[security]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `api_key` | `OMNICODE_API_KEY` | `""` | Legacy single-key auth (`X-API-Key` header). |
| `require_api_key` | `OMNICODE_REQUIRE_API_KEY` | `false` | Refuse to start without a key. |
| `allow_apply_patch` | `OMNICODE_ALLOW_APPLY_PATCH` | `true` | When `false`, `/patch/apply` and `/patch/rollback` return 403. |
| `allow_shell` | `OMNICODE_ALLOW_SHELL` | `false` | Reserved for the future `execute_tool` gate. |
| `mcp_tools` | `OMNICODE_MCP_TOOLS` | `core` | `core` (8 tools) / `all` (24) / `legacy` (16). |

### `[agent]`

The `omnicode agent` watcher (Wave 2 W2-2).

| Key | Env | Default | Notes |
|---|---|---|---|
| `remote` | `OMNICODE_REMOTE` | `""` | Remote OmniCode URL. Required. |
| `token` | `OMNICODE_AGENT_TOKEN` | `""` | Bearer / X-API-Key sent on every push. Falls back to `OMNICODE_API_KEY`. |
| `debounce_ms` | `OMNICODE_AGENT_DEBOUNCE_MS` | `800` | Coalesce bursts of file events. |

### `[env]` passthrough

For ad-hoc env-var overrides we don't model:

```toml
[env]
TRANSFORMERS_OFFLINE = "1"
HF_HUB_OFFLINE = "1"
GITHUB_TOKEN = "ghp_..."
```

---

## Environment variable reference (alphabetical)

> Setting any of these in env wins over an `omnicode.toml` value.

| Env var | Type | Notes |
|---|---|---|
| `API_HOST` | string | FastAPI host. |
| `API_PORT` | int | FastAPI port. |
| `API_TITLE` / `API_DESCRIPTION` / `API_VERSION` | string | OpenAPI metadata. |
| `CODEBASE_GIT_DIR` | string | Auto-commit git for AI edits. Default `.codebase`. |
| `CONDA_ENV_NAME` | string | Used by `scripts/run.sh` etc. |
| `CORS_ORIGINS` / `CORS_METHODS` / `CORS_HEADERS` | list | CORS config. |
| `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL` | string | Fallback when no role mapping exists. |
| `EMBEDDING_MODEL` | string | sentence-transformers model name. |
| `FS_BROWSER_DENY_PATTERNS` | list | Paths the file picker refuses. |
| `HF_DATASETS_OFFLINE` / `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | bool | HuggingFace offline mode (set to `1` for cloud). |
| `MAX_SEARCH_RESULTS` | int | Default page size. |
| `MEMORY_MIN_IMPORTANCE` | int | Default importance filter. |
| `OMNICODE_AGENT_DEBOUNCE_MS` | int | Watcher debounce window. |
| `OMNICODE_AGENT_TOKEN` | string | Agent's bearer token. |
| `OMNICODE_ALLOW_APPLY_PATCH` | bool | See `[security]`. |
| `OMNICODE_ALLOW_SHELL` | bool | See `[security]`. |
| `OMNICODE_API_KEY` | string | Legacy single-key auth. |
| `OMNICODE_CONFIG` | path | Override path of the TOML file. |
| `OMNICODE_EMBEDDING_BACKEND` | string | `local` / `remote` / `hybrid`. |
| `OMNICODE_EMBEDDING_REMOTE_URL` | url | OpenAI-compatible /embeddings endpoint. |
| `OMNICODE_EMBEDDING_REMOTE_KEY` | string | Bearer for the remote embedding API. |
| `OMNICODE_EMBEDDING_REMOTE_MODEL` | string | Model name to send to the remote. |
| `OMNICODE_INDEX_INCREMENTAL` | bool | See `[index]`. |
| `OMNICODE_LSP` / `OMNICODE_MEMORY` / `OMNICODE_SAFE_EDIT` | bool | Feature toggles. |
| `OMNICODE_LLM_ROUTER` | bool | Enable / disable the LLM router. |
| `OMNICODE_MCP_HTTP` | bool | Enable MCP-over-HTTP transport. |
| `OMNICODE_MCP_REQUIRE_AUTH` | bool | Refuse MCP-over-HTTP startup without auth. |
| `OMNICODE_MCP_TOOLS` | string | `core` / `all` / `legacy`. |
| `OMNICODE_MODE` | string | `local` / `cloud` / `hybrid`. |
| `OMNICODE_READ_ONLY` | bool | Block every mutating endpoint. |
| `OMNICODE_REMOTE` | url | Remote URL for the local agent. |
| `OMNICODE_RERANKER` | bool | Enable cross-encoder reranker. |
| `OMNICODE_RERANKER_MODEL` | string | HuggingFace cross-encoder. |
| `OMNICODE_REQUIRE_API_KEY` | bool | Refuse to start without a key. |
| `OMNICODE_WEB_CONSOLE` | bool | Set to `false` for headless. |
| `PROVIDER_DB_PATH` | path | Override the provider DB location. |
| `QUALITY_THRESHOLD` | float | Edit pipeline quality gate. |
| `WORKING_DIR` | path | Project root. |

### LLM provider env vars

Used by the optional router (only when `omnicode-mcp[llm]` is
installed):

| Env var | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Built-in `claude` / `claude_fast` providers. |
| `OPENAI_API_KEY` | Built-in `openai` / `openai_fast` providers. |
| `GEMINI_API_KEY` | Built-in `gemini` / `gemini_fast` providers. |
| `DEEPSEEK_API_KEY` | Built-in `deepseek` provider. |

---

## Mode presets

`omnicode serve --mode <name>` applies a preset via `setdefault` —
existing env vars still win.

| Mode | `OMNICODE_MODE` | `OMNICODE_READ_ONLY` | `OMNICODE_ALLOW_APPLY_PATCH` |
|---|---|---|---|
| `local` (default) | `local` | `false` | `true` |
| `cloud` | `cloud` | `true` | `false` |
| `hybrid` | `hybrid` | `false` | `false` |

Hybrid mode keeps writes ON because the `omnicode agent` endpoint
under `/index/*` needs to push file bodies. `apply_patch` is still
blocked because the local editor is the canonical writer.

---

## Persistence layout

| Path | Purpose | Per-machine state? |
|---|---|---|
| `<wd>/.data/shards/<id>/vector_store.faiss` | FAISS index | yes |
| `<wd>/.data/shards/<id>/vector_store.db` | chunk metadata | yes |
| `<wd>/.data/shards/<id>/file_tracker.db` | mtime / hash for incremental | yes |
| `<wd>/.data/shards/<id>/snapshots/` | pre-apply file backups | yes |
| `<wd>/.data/shards/<id>/edit_sessions/` | JSON session records | yes |
| `~/.kiro/codebase-mcp/providers.db` | Fernet-encrypted provider keys | yes (shared across projects) |
| `~/.kiro/codebase-mcp/providers.key` | Fernet master key (file mode `0600`) | yes |
| `~/.kiro/codebase-mcp/users.db` | RBAC users + token hashes | yes (versioned via `PRAGMA user_version`) |
| `~/.kiro/codebase-mcp/workspaces.json` | workspace bookmarks | yes |

> The shard layout (W2-10) is auto-migrated from the legacy
> `<wd>/.data/<file>` layout on first run. Idempotent — won't move
> anything if the default shard already has files.

---

## Worked examples

### Local single-user dev box

```toml
# omnicode.toml
[server]
mode = "local"

[features]
llm_router = true     # opt-in, requires pip install omnicode-mcp[llm]
```

```bash
omnicode serve --console
```

### Cloud read-only deployment

```toml
# omnicode.toml on the server
[server]
mode = "cloud"
host = "127.0.0.1"
port = 6789

[security]
api_key = ""                  # leave blank, use RBAC instead
allow_apply_patch = false

[search]
reranker = true               # better quality on shared infra
```

```bash
# Service unit boots with cloud preset already applied
omnicode serve --headless --mode cloud
# Then bootstrap an admin user via /admin/users
```

### Hybrid (cloud index + local apply)

```bash
# On the cloud machine
omnicode serve --headless --mode hybrid

# On the user's local machine
omnicode agent \
    --remote https://omnicode.example.com \
    --token sk-... \
    --workspace .
```

### Multi-tenant cloud

Identical to the read-only deployment + workspaces:

```bash
# Bootstrap admin
curl -X POST https://omnicode.example.com/admin/users \
  -H 'content-type: application/json' \
  -d '{"username": "alice"}'

# Register workspace
curl -X POST https://omnicode.example.com/workspaces \
  -H "X-API-Key: $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "team-a", "path": "/srv/omnicode/workspaces/team-a", "set_active": true}'
```

Each registered workspace gets its own FAISS shard at
`<wd>/.data/shards/wk_<id>/`. Search queries are scoped to the
active workspace.

---

## Validating the config

```bash
omnicode doctor
```

Checks:

- Python ≥ 3.11
- Required deps (`fastapi`, `uvicorn`, `tree-sitter`, `faiss-cpu`,
  `sentence-transformers`, `pydantic`, `httpx`).
- LSP servers (10 supported, only what's on PATH).
- Embedding model cache.
- Port 6789 reachable.

Failures print install hints rather than crash.
