# Usage Guide

> Everything you need to install, configure, and run OmniCode-MCP.
> Combines the local-run cookbook (formerly `running.md`), the
> configuration reference (formerly `configuration.md`), and the
> opt-in LLM features (formerly `llm-extras.md`).

---

## Table of contents

- [Install](#install)
- [Run the server](#run-the-server)
- [Connect an AI editor](#connect-an-ai-editor)
- [Configuration](#configuration)
  - [Where settings come from](#where-settings-come-from)
  - [TOML reference](#toml-reference)
  - [Environment variables](#environment-variables)
  - [Mode presets](#mode-presets)
  - [Worked examples](#worked-examples)
- [Optional LLM features](#optional-llm-features)
- [Troubleshooting](#troubleshooting)

---

## Install

### Required

- Python ≥ 3.11 (3.12 also tested in CI)
- git
- Optional: 10 LSP servers (only what's on `PATH` activates; check
  with `omnicode doctor`)
- Optional: Docker if you want to run via compose

### One-liner

```bash
git clone https://github.com/foolkking/omnicode-mcp.git
cd omnicode-mcp

python -m venv .venv
. .venv/Scripts/activate          # Windows PowerShell
# . .venv/bin/activate            # macOS / Linux

pip install -e .                  # core only
# pip install -e ".[llm]"         # add multi-provider LLM router
# pip install -e ".[agent]"       # add filesystem watcher (hybrid mode)
# pip install -e ".[dev]"         # tests + linters
```

### Conda alternative

```bash
conda create -n omnicode-env python=3.11 -y
conda activate omnicode-env
pip install -e ".[dev]"
```

### Environment variables

```bash
cp .env.example .env
# Edit, or skip and rely on omnicode.toml + the Web Console.
```

By default HuggingFace runs offline (`TRANSFORMERS_OFFLINE=1`,
`HF_HUB_OFFLINE=1`) so a network-restricted machine doesn't fail at
startup. To prime the embedding model cache once:

```bash
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
  python -c "from sentence_transformers import SentenceTransformer; \
             SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

The Docker image already does this in the build step.

---

## Run the server

### Local single-user (default)

```bash
omnicode serve --console          # API + Web UI at http://127.0.0.1:6789/
omnicode serve --headless         # API only, no UI
omnicode dev                      # console + auto-reload on file changes
omnicode mcp                      # MCP stdio for Claude Desktop / Cursor / Kiro
```

The first index build takes 30 – 60 s; subsequent rebuilds are
incremental (2 – 3 s).

### Cloud / hybrid

```bash
omnicode serve --headless --mode cloud         # writes OFF, apply OFF
omnicode serve --headless --mode hybrid        # accepts /index/* pushes from agents
```

Mode presets (see [`Mode presets`](#mode-presets) below).

### MCP-over-HTTP (remote AI editors)

```bash
python mcp_server.py --transport sse --port 6790 --auth required
python mcp_server.py --transport streamable-http --auth auto
```

Use only when remote MCP clients need to connect over the wire.
Stdio is the right call locally.

### CLI cheat sheet

| Command | Purpose |
|---|---|
| `omnicode init` | Write `.data/` skeleton |
| `omnicode index [--force]` | Incremental / full rebuild |
| `omnicode status` | Hits `/health` on the running server |
| `omnicode doctor` | Python / deps / LSP / model / port check |
| `omnicode rotate-master-key` | Rotate Fernet key for `providers.db` |
| `omnicode agent --remote URL --token TOK --workspace .` | Hybrid-mode local watcher |
| `omnicode serve [--headless\|--console] [--mode local\|cloud\|hybrid] [--host] [--port] [--reload]` | All-in-one server |
| `omnicode dev` | `serve --console --reload` |
| `omnicode mcp` | stdio MCP |

Helper scripts in `scripts/` (`run.bat`/`.sh`, `run-dev.bat`/`.sh`,
`test.bat`/`.sh`, `lint.bat`/`.sh`) wrap the above for convenience.

---

## Connect an AI editor

### Kiro

`~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "omnicode": {
      "command": "omnicode",
      "args": ["mcp"],
      "env": {
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1"
      },
      "autoApprove": [
        "omni_search", "omni_read", "omni_analyze",
        "omni_memory", "omni_context", "omni_intelligence",
        "discover_tools"
      ]
    }
  }
}
```

`autoApprove` is a Kiro convention — read-only tools don't need
per-call confirmation. Keep `omni_edit` out of the list so writes
still prompt.

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS):

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

Restart the app after saving.

### Cursor / Continue / VS Code

All accept the same shape via their MCP config UIs. Use the same
`command` + `args`. Continue's MCP integration also accepts SSE if
you'd rather run `mcp_server.py --transport sse --port 6790`.

By default 8 high-level tools are registered (`omni_search`,
`omni_read`, `omni_edit`, `omni_analyze`, `omni_memory`,
`omni_context`, `omni_intelligence`, `discover_tools`). Set
`OMNICODE_MCP_TOOLS=all` if you also need the legacy 16
fine-grained tools.

---

## Configuration

### Where settings come from

Three layered sources, **highest precedence wins**:

1. **CLI flags** — `omnicode serve --mode cloud --port 8765`.
2. **Process env vars** — every Pydantic Settings field has a
   matching env name. Set in your shell or `.env`.
3. **TOML configuration file** — `omnicode.toml` next to where you
   launch (or `OMNICODE_CONFIG=/path/to.toml`). Loaded BEFORE
   Settings instantiates and folded into env via `setdefault`, so
   anything already set wins.
4. **Pydantic defaults** — built into `omnicode/config/settings.py`.

> Rule of thumb: project-wide defaults in `omnicode.toml`, secrets
> in env vars, one-off overrides on the CLI.

### TOML reference

Sample at [`omnicode.example.toml`](../omnicode.example.toml). Every
key is optional.

#### `[server]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `mode` | `OMNICODE_MODE` | `local` | `local` / `cloud` / `hybrid` |
| `host` | `API_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for cloud + reverse proxy. |
| `port` | `API_PORT` | `6789` | FastAPI port |
| `auth` | `OMNICODE_MCP_REQUIRE_AUTH` | `false` | Refuse MCP-over-HTTP startup when no auth source is configured |

#### `[workspace]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `root` | `WORKING_DIR` | `cwd` | Absolute path of the codebase |
| `read_only` | `OMNICODE_READ_ONLY` | `false` | Block every mutating endpoint except a query-only allow-list |

#### `[features]`

| Key | Env | Default |
|---|---|---|
| `web_console` | `OMNICODE_WEB_CONSOLE` | `true` |
| `mcp_http` | `OMNICODE_MCP_HTTP` | `false` |
| `llm_router` | `OMNICODE_LLM_ROUTER` | `false` |
| `lsp` | `OMNICODE_LSP` | `true` |
| `memory` | `OMNICODE_MEMORY` | `true` |
| `safe_edit` | `OMNICODE_SAFE_EDIT` | `true` |

#### `[index]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `incremental` | `OMNICODE_INDEX_INCREMENTAL` | `true` | When `false` every rebuild is full |
| `embedding_device` | `OMNICODE_EMBEDDING_DEVICE` | `cpu` | `cpu` / `cuda` |
| `embedding_model` | `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model name |

#### `[search]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `reranker` | `OMNICODE_RERANKER` | `false` | Cross-encoder reranker. Adds 50 – 200 ms per query |
| `reranker_model` | `OMNICODE_RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | HuggingFace cross-encoder |

#### `[security]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `api_key` | `OMNICODE_API_KEY` | `""` | Legacy single-key auth (`X-API-Key` header) |
| `require_api_key` | `OMNICODE_REQUIRE_API_KEY` | `false` | Refuse to start without a key |
| `allow_apply_patch` | `OMNICODE_ALLOW_APPLY_PATCH` | `true` | When `false`, `/patch/apply` and `/patch/rollback` return 403 |
| `allow_shell` | `OMNICODE_ALLOW_SHELL` | `false` | Reserved for the future `execute_tool` gate |
| `mcp_tools` | `OMNICODE_MCP_TOOLS` | `core` | `core` (8) / `all` (24) / `legacy` (16) |

#### `[agent]`

| Key | Env | Default | Notes |
|---|---|---|---|
| `remote` | `OMNICODE_REMOTE` | `""` | Remote OmniCode URL (required for `omnicode agent`) |
| `token` | `OMNICODE_AGENT_TOKEN` | `""` | Bearer / X-API-Key sent on every push. Falls back to `OMNICODE_API_KEY` |
| `debounce_ms` | `OMNICODE_AGENT_DEBOUNCE_MS` | `800` | Coalesce bursts of file events |

#### `[env]` passthrough

For ad-hoc env-var overrides we don't model:

```toml
[env]
TRANSFORMERS_OFFLINE = "1"
HF_HUB_OFFLINE = "1"
GITHUB_TOKEN = "ghp_..."
```

### Environment variables

Selected reference (alphabetical, omitting the ones already covered
by the TOML table above):

| Env var | Type | Notes |
|---|---|---|
| `API_TITLE` / `API_DESCRIPTION` / `API_VERSION` | string | OpenAPI metadata |
| `CODEBASE_GIT_DIR` | string | Auto-commit git for AI edits. Default `.codebase` |
| `CORS_ORIGINS` / `CORS_METHODS` / `CORS_HEADERS` | list | CORS config |
| `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL` | string | Fallback when no role mapping exists |
| `FS_BROWSER_DENY_PATTERNS` | list | Paths the file picker refuses |
| `MAX_SEARCH_RESULTS` | int | Default page size |
| `MEMORY_MIN_IMPORTANCE` | int | Default importance filter |
| `OMNICODE_CONFIG` | path | Override path of the TOML file |
| `OMNICODE_EMBEDDING_BACKEND` | string | `local` / `remote` / `hybrid` |
| `OMNICODE_EMBEDDING_REMOTE_URL` | url | OpenAI-compatible /embeddings endpoint |
| `OMNICODE_EMBEDDING_REMOTE_KEY` | string | Bearer for the remote embedding API |
| `OMNICODE_EMBEDDING_REMOTE_MODEL` | string | Model name to send to the remote |
| `PROVIDER_DB_PATH` | path | Override the provider DB location |
| `QUALITY_THRESHOLD` | float | Edit pipeline quality gate |

LLM provider env vars (only used when `omnicode-mcp[llm]` is installed):

| Env var | Built-in providers |
|---|---|
| `ANTHROPIC_API_KEY` | `claude`, `claude_fast` |
| `OPENAI_API_KEY` | `openai`, `openai_fast` |
| `GEMINI_API_KEY` | `gemini`, `gemini_fast` |
| `DEEPSEEK_API_KEY` | `deepseek` |

### Mode presets

`omnicode serve --mode <name>` applies a preset via `setdefault` —
existing env vars still win.

| Mode | `OMNICODE_MODE` | `OMNICODE_READ_ONLY` | `OMNICODE_ALLOW_APPLY_PATCH` |
|---|---|---|---|
| `local` (default) | `local` | `false` | `true` |
| `cloud` | `cloud` | `true` | `false` |
| `hybrid` | `hybrid` | `false` | `false` |

Hybrid keeps writes ON because the agent endpoints under `/index/*`
need to push file bodies. `apply_patch` is still blocked because the
local editor is the canonical writer.

### Worked examples

#### Local single-user dev box

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

#### Cloud read-only deployment

```toml
[server]
mode = "cloud"
host = "127.0.0.1"
port = 6789

[security]
api_key = ""                  # use RBAC instead
allow_apply_patch = false

[search]
reranker = true               # better quality on shared infra
```

```bash
omnicode serve --headless --mode cloud
# Bootstrap an admin via /admin/users (see api.md)
```

#### Hybrid (cloud index + local apply)

```bash
# On the cloud machine
omnicode serve --headless --mode hybrid

# On the user's local machine
omnicode agent \
  --remote https://omnicode.example.com \
  --token sk-... \
  --workspace . \
  --debounce-ms 800
```

`pip install -e ".[agent]"` adds the optional `watchfiles`
dependency for low-CPU change detection. Without it the agent falls
back to a 5 s poll loop.

#### Multi-tenant cloud

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
`<wd>/.data/shards/wk_<id>/`.

### Validating the config

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

---

## Optional LLM features

> By design, **OmniCode-MCP's core does not depend on any LLM**.
> Patches preview, validate, apply, and roll back without ever
> calling out. The features in this section are opt-in extensions
> for users who want OmniCode itself to talk to a model — typically
> when no AI editor is in front of it (CI agent, headless script,
> integration test).
>
> If you're already calling OmniCode from Cursor / Claude /
> Continue, let *that* tool drive the LLM and skip this section.

### Install

```bash
pip install -e ".[llm]"
```

Pulls `litellm` and `google-generativeai`. Without these the
provider router gracefully reports `Capability.LLM_ENHANCEMENT =
unavailable` in `/capabilities`.

### What lights up

| Feature | Module | Default state |
|---|---|---|
| Provider Registry | `omnicode/llm/provider_registry.py` | always loaded; rows just sit unused without `[llm]` |
| LLM Router | `omnicode/llm/router.py` | available when `[llm]` extras present |
| Best-of-N | `omnicode/llm/router.py::race_providers` | experimental, opt-in via `?best_of=N` |
| AI edit pipeline | `omnicode/pipelines/edit.py` | wired by `POST /edit` when a router exists |
| AI review / repair | `omnicode/pipelines/edit.py` | optional flags inside `/edit` |

### What stays core (never depends on LLM)

- `POST /patch/preview` / `validate` / `apply` / `rollback` /
  `explain`
- `GET /graph/*`
- `GET /lsp/*`
- `POST /search/*`
- `POST /memory/advisory`
- `POST /intelligence/context` (composer is happy with no LLM —
  the `llm_enhancement` capability just shows `available=false`)
- The whole MCP tool surface routes via REST so it inherits the
  same isolation.

### Built-in providers (env-driven)

When the env var has a real-looking key the router auto-registers
the matching provider:

| Env var | Built-in providers | Models |
|---|---|---|
| `ANTHROPIC_API_KEY` | `claude`, `claude_fast` | `claude-3-opus-20240229`, `claude-3-haiku-20240307` |
| `OPENAI_API_KEY` | `openai`, `openai_fast` | `gpt-4o`, `gpt-4o-mini` |
| `GEMINI_API_KEY` | `gemini`, `gemini_fast` | `gemini/gemini-1.5-pro`, `gemini/gemini-1.5-flash` |
| `DEEPSEEK_API_KEY` | `deepseek` | `deepseek/deepseek-coder` |

These have `built_in=True` and live in process memory — disable
them by clearing the env var.

### Custom providers

Persisted Fernet-encrypted at `~/.kiro/codebase-mcp/providers.db`.
Each row: `name`, `model` (LiteLLM string),
`api_key`, `api_base`, `provider_type`, `group`, `extra_headers`,
`enabled`.

```bash
curl -X POST http://127.0.0.1:6789/providers \
  -H "X-API-Key: $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
        "name": "my-vllm",
        "model": "openai/llama-3-70b",
        "api_base": "http://localhost:8000/v1",
        "api_key": "sk-anything",
        "provider_type": "openai-compatible",
        "group": "balanced"
      }'
```

Test with:

```bash
curl -X POST http://127.0.0.1:6789/providers/my-vllm/test \
  -H "X-API-Key: $TOKEN"
# 20 s timeout. Returns ok + hint + hint_field for UI red-border feedback.
```

### Role-based selection

Configure via `PUT /selections` or in the Web Console **Model
Providers** page. Roles: `default / quality / cost / fastest /
edit / scan / review / summary / chat`.

### AI edit pipeline (`POST /edit`)

Three-layer defence so reasoning models don't leak `<thinking>`
blocks into the file:

1. Prose detector — refuses to overwrite code with prose.
2. Reasoning strip — removes `<thinking>` / `## Plan`-style
   sections.
3. Final-shrink check — refuses replacements that shrink the file
   by ≥ 60 % unless the prompt explicitly asked.

Three modes: `whole_file` (rewrite everything), `surgical`
(replace one named symbol; rest byte-identical), `patch` (apply a
fenced unified diff).

When the LLM fails, the response is HTTP 200 with a
`failure_analysis` object describing stage / root cause / suggested
fix / raw LLM excerpt — the UI uses it to show actionable error
panels.

### Disabling LLM features entirely

```toml
[features]
llm_router = false
ai_edit = false
```

The `[llm]` extras stay installed but unused; the capability
fingerprint reports the feature off.

---

## Troubleshooting

### "Port 6789 already in use"

```bash
omnicode serve --port 6790
```

Or kill the existing process. On Windows: `netstat -ano | findstr 6789`
to find the PID.

### "MCP tool count is 24, I expected 8"

The default flipped to `core` in commit `9199f52`. If you're still
seeing 24, your client launched OmniCode with
`OMNICODE_MCP_TOOLS=all`. Drop the env var or set it to `core`.

### "Embeddings model isn't downloading"

By design — see [Install](#install) for priming the cache.

### "Workspace path under Windows reports invalid"

Use forward slashes or escape backslashes when sending JSON
bodies:

```jsonc
// good
{ "path": "C:/Users/me/projects/my-app" }
// also good
{ "path": "C:\\Users\\me\\projects\\my-app" }
// BAD — single backslash gets parsed as escape
{ "path": "C:\Users\me\projects\my-app" }
```

### "Tests fail with `Settings cache_clear`"

Wrong env. Use the conda env that has the package installed editable:

```bash
conda run --no-capture-output -n omnicode-env python -m pytest tests -q
```

### "MCP shows 0 tools after a while"

Stdio MCP is stateless — if Kiro / Claude restarts and the server
process dies it'll reconnect on next prompt. If it doesn't, check
the `omnicode mcp` process is alive:

```bash
omnicode status                                    # via /health
ps aux | grep mcp_server                           # Linux/macOS
Get-Process | Where-Object Name -like 'python*'    # Windows
```

### "I want logs, but `*.log` is gitignored"

That's the design — logs are session-local. Use the `_keep_/`
escape hatch to share a single redacted file:

```bash
mkdir -p _keep_/regressions
journalctl -u omnicode -n 200 --no-pager > _keep_/regressions/2026-05-28-segfault.log
git add _keep_/regressions/2026-05-28-segfault.log
```

See [`_keep_/README.md`](../_keep_/README.md) for conventions.
