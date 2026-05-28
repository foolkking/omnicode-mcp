# OmniCode-MCP — VS Code extension (very thin)

> **Not** an AI editor. Cursor / Continue / Copilot / Claude Code do
> that better. This extension exposes three OmniCode-MCP commands so a
> plain-VS-Code user can reach the Codebase Intelligence Layer
> without opening the Web Console.

## Commands

| Command | What it does |
|---|---|
| `OmniCode: Show Impact` | Runs `/graph/impact` + `/graph/risk` + `/graph/related-tests` against the symbol under the cursor (or one you type). Renders blast radius, callers, callees, suggested tests in a side panel. |
| `OmniCode: Apply Patch` | Sends the active editor's contents through `/patch/preview` → confirm modal → `/patch/apply`. Snapshot rollback handled by the server. |
| `OmniCode: Capability Status` | Quick-pick of the `/capabilities` fingerprint. The status bar item also shows `OmniCode N/M` where `N` is the count of online capabilities. |

## Settings

| Key | Default | Notes |
|---|---|---|
| `omnicode.serverUrl` | `http://127.0.0.1:6789` | Base URL of the OmniCode-MCP HTTP server. |
| `omnicode.apiKey` | *(empty)* | Sent as `X-API-Key`. Required if the server has `OMNICODE_API_KEY` set or any RBAC user exists. |
| `omnicode.confirmApplyPatch` | `true` | Show a confirmation modal before `/patch/apply`. |

## Build

The extension is intentionally tiny — Node's built-in `http`/`https`
modules only, no axios / node-fetch. CI does not build it; release it
manually with:

```bash
cd extensions/vscode
npm install
npm run compile
npx vsce package
```

The resulting `.vsix` can be installed via VS Code's
"Extensions: Install from VSIX…" command.

## Why this is short on purpose

The architecture-v2 §17 "最终目标" pins OmniCode-MCP as a *Codebase
Intelligence Layer* — a service AI editors call. This extension keeps
that promise: it just surfaces the existing endpoints, never tries to
write its own LLM glue, never replaces Continue / Copilot.

If you want a chat sidebar with code edits, install one of those.
If you want to know "what will break when I change `ProviderRegistry.upsert`?"
before letting an LLM rewrite it — that's what this extension is for.
