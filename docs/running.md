# Running OmniCode-MCP locally

> Cookbook for the `omnicode` CLI. All paths assume Windows
> PowerShell or Bash; substitute as needed. The conda environment
> name `omnicode-env` is a convention — change to whatever you
> prefer.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [Install](#install)
- [Five ways to start the server](#five-ways-to-start-the-server)
- [Connecting an AI editor](#connecting-an-ai-editor)
- [The other CLI commands](#the-other-cli-commands)
- [Hybrid mode (cloud index + local apply)](#hybrid-mode-cloud-index--local-apply)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Python ≥ 3.11** (3.12 also tested in CI)
- **git**
- **Optional**:
  - Node.js 18+ for the JS / TS static-analysis path
  - The 10 LSP servers you want to use (only the ones you have
    installed will activate; see `omnicode doctor`)
  - Docker if you want to run via compose

---

## Install

### Conda (recommended)

```bash
conda create -n omnicode-env python=3.11 -y
conda activate omnicode-env
pip install -e .
```

### venv

```bash
python -m venv .venv
. .venv/Scripts/activate           # Windows PowerShell
# . .venv/bin/activate             # macOS / Linux
pip install -e .
```

### Optional extras

```bash
pip install -e ".[dev]"             # pytest + ruff
pip install -e ".[llm]"             # multi-provider LLM router
pip install -e ".[agent]"           # filesystem watcher (watchfiles) for hybrid
pip install -e ".[dev,llm,agent]"   # everything
```

### Environment variables

```bash
cp .env.example .env
# Edit .env — or skip and use omnicode.toml + the Web Console UI instead.
```

---

## Five ways to start the server

### 1 · Web Console (default for humans)

```bash
omnicode serve --console
# alias: omnicode serve  (console is the default)
```

Opens API + Web UI at <http://127.0.0.1:6789/>.

### 2 · Headless API (for AI editors over HTTP)

```bash
omnicode serve --headless
```

Same routes, no `/` static files served. Useful when the front-end
lives somewhere else (or you just don't want it).

### 3 · Dev mode (hot reload)

```bash
omnicode dev
```

Equivalent to `serve --console --reload`.

### 4 · MCP stdio (for AI editors over stdio)

```bash
omnicode mcp
```

This is what Claude Desktop / Cursor / Kiro spawn under the hood
when you wire up the MCP config (next section).

### 5 · MCP-over-HTTP (SSE / streamable-http)

```bash
python mcp_server.py --transport sse --port 6790 --auth required
# or
python mcp_server.py --transport streamable-http --auth auto
```

Use only when you need remote MCP clients to connect over the wire
— stdio is the right call locally.

---

## Connecting an AI editor

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

Both follow the same pattern via their MCP config UIs. Use the same
`command` + `args` shape. Continue's MCP integration also accepts
SSE if you'd rather run `mcp_server.py --transport sse --port 6790`.

---

## The other CLI commands

| Command | Purpose |
|---|---|
| `omnicode init` | Write the `.data/` skeleton in the current dir. |
| `omnicode index [--force]` | Force / incremental rebuild. Default is incremental. |
| `omnicode status` | Hits `/health` on the running server. |
| `omnicode doctor` | Checks Python, deps, LSP servers on PATH, embedding model cache, port 6789. |
| `omnicode rotate-master-key` | Rotate the Fernet key for `providers.db`. See [`security.md`](security.md). |
| `omnicode agent --remote URL --token TOK --workspace .` | Local file-sync watcher for hybrid mode (see below). |

`omnicode doctor` is the friendliest diagnostic. Sample output:

```
Python:           3.11.9
Required deps:    fastapi 0.111, uvicorn 0.30, tree-sitter 0.22, …
Language Servers:
  ✅ pyright           — Pyright (Python)
  ✅ typescript-language-server — tsserver (TypeScript)
  ⚠️  gopls             not installed (go install golang.org/x/tools/gopls@latest)
  …
Working directory:   C:\Users\me\projects\my-app
Port 6789:           ✅ server is running
```

---

## Hybrid mode (cloud index + local apply)

**Server side** (run on a VM with more RAM than your laptop):

```bash
# Bootstrap an admin user once
curl -X POST https://omnicode.example.com/admin/users \
  -H 'content-type: application/json' \
  -d '{"username": "alice"}'
# Save the bootstrap_token from the response

# Service unit boots in hybrid mode
omnicode serve --headless --mode hybrid --port 6789
```

**Local side** (where the actual codebase lives):

```bash
omnicode agent \
  --remote https://omnicode.example.com \
  --token "$BOOTSTRAP_TOKEN" \
  --workspace . \
  --debounce-ms 800
```

The agent does an initial walk + push, then watches for changes. AI
editors can ask the cloud OmniCode for impact / search / advisory,
and the editor (not the cloud) applies the patches locally.

`pip install -e ".[agent]"` to get the optional `watchfiles`
dependency for low-CPU change detection. Without it the agent falls
back to a 5 s poll loop.

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

By design. Default is offline (`TRANSFORMERS_OFFLINE=1`,
`HF_HUB_OFFLINE=1`) so a fresh install on a network-restricted
machine doesn't fail. To prime the cache:

```bash
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
  python -c "from sentence_transformers import SentenceTransformer; \
             SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

The Docker image already does this in the build step.

### "Workspace path under Windows reports invalid"

Always use forward slashes or escape backslashes when sending JSON
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

You're running tests in the wrong env. Use the conda env that has
the package installed editable:

```bash
conda run --no-capture-output -n omnicode-env python -m pytest tests -q
```

### "MCP shows 0 tools after a while"

Stdio MCP is stateless — if Kiro / Claude restarts and the server
process dies it'll reconnect on next prompt. If it doesn't, check
the `omnicode mcp` process is alive:

```bash
omnicode status
# or
ps aux | grep mcp_server         # Linux/macOS
Get-Process | Where-Object Name -like 'python*'   # Windows
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
