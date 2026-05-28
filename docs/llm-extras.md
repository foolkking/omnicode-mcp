# Optional LLM Features

> By design, **OmniCode-MCP's core does not depend on any LLM**.
> Patches preview, validate, apply, and roll back without ever
> calling out. The features in this doc are opt-in extensions for
> users who want OmniCode itself to talk to a model — typically
> when no AI editor is in front of it (CI agent, headless script,
> integration test).
>
> If you're already calling OmniCode from Cursor / Claude / Continue,
> let *that* tool drive the LLM and skip this whole document.

---

## Install the extras

```bash
pip install -e ".[llm]"
```

Pulls `litellm` and `google-generativeai`. Without these the
provider router gracefully reports `Capability.LLM_ENHANCEMENT =
unavailable` in `/capabilities`.

---

## What lights up

| Feature | Module | Default state |
|---|---|---|
| Provider Registry | `omnicode/llm/provider_registry.py` | always loaded; provider rows just sit unused without `[llm]` |
| LLM Router | `omnicode/llm/router.py` | available when `[llm]` extras present |
| Best-of-N | `omnicode/llm/router.py::race_providers` | experimental, opt-in via `?best_of=N` |
| AI edit pipeline | `omnicode/pipelines/edit.py` | wired by `POST /edit` when a router exists |
| AI review / repair | `omnicode/pipelines/edit.py` | optional flags inside `/edit` |

### What stays core (never depends on LLM)

- `POST /patch/preview / validate / apply / rollback / explain`
- `GET /graph/*`
- `GET /lsp/*`
- `POST /search/*`
- `POST /memory/advisory`
- `POST /intelligence/context` (composer is happy with no LLM —
  the `llm_enhancement` capability just shows `available=false`)
- The whole MCP tool surface (omni_*) routes via REST so it
  inherits the same isolation.

---

## Built-in providers (env-driven)

When the env var has a real-looking key the router auto-registers
the matching provider:

| Env var | Built-in providers | Models |
|---|---|---|
| `ANTHROPIC_API_KEY` | `claude`, `claude_fast` | `claude-3-opus-20240229`, `claude-3-haiku-20240307` |
| `OPENAI_API_KEY` | `openai`, `openai_fast` | `gpt-4o`, `gpt-4o-mini` |
| `GEMINI_API_KEY` | `gemini`, `gemini_fast` | `gemini/gemini-1.5-pro`, `gemini/gemini-1.5-flash` |
| `DEEPSEEK_API_KEY` | `deepseek` | `deepseek/deepseek-coder` |

These have `built_in=True` and are **not** stored in the registry DB —
they live in process memory. Disable them by clearing the env var.

---

## Provider registry (custom providers)

Persisted at `~/.kiro/codebase-mcp/providers.db` (or
`PROVIDER_DB_PATH` if set). Each row:

| Field | Notes |
|---|---|
| `name` | Unique identifier |
| `model` | LiteLLM model string (`openai/gpt-4o`, `ollama/llama3`, `azure/<deploy>`, `claude-3-opus-20240229`, …) |
| `api_key` | Stored Fernet-encrypted (`ofb1:` prefix at rest) |
| `api_base` | Optional custom base URL for self-hosted / proxy endpoints |
| `provider_type` | `openai-compatible` / `anthropic` / `gemini` / `ollama` / `azure` / `bedrock` / `custom` |
| `group` | Routing group: `quality` / `cost` / `balanced` |
| `extra_headers` | JSON object of headers sent with every request |
| `enabled` | Participate in routing |
| `built_in` | True for env-derived rows (not stored in DB) |
| `description` | Free-form |

REST endpoints under `/providers/*` (see
[`api-reference.md`](api-reference.md)).

### Adding a provider via the REST API

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

### Testing a provider

```bash
curl -X POST http://127.0.0.1:6789/providers/my-vllm/test \
  -H "X-API-Key: $TOKEN"
# 20s timeout. Returns ok + hint + hint_field for UI red-border feedback.
```

---

## Role-based selection

`omnicode/llm/router.py` exposes a per-role lookup so different
parts of the pipeline can pin different models. Default roles:

| Role | Used by |
|---|---|
| `default` | `EditPipeline` fallback |
| `quality` | High-stakes refactors |
| `cost` | Bulk re-summarisation |
| `fastest` | Inline assist |
| `edit` | The `POST /edit` pipeline |
| `scan` | Static analysis post-processing |
| `review` | Auto-review of generated patches |
| `summary` | One-line diff explanations |
| `chat` | Reserved for the (parked) chat UI |

Configure via:

```bash
PUT /selections
{ "edit": "openai_fast", "review": "claude", ... }
```

Or interactively in the Web Console **Model Providers** page.

---

## Best-of-N

Race the top N providers in the requested role's group; pick the
longest non-empty completion. Useful when latency matters less than
quality (e.g. a single critical refactor at the end of a
session).

```bash
POST /edit?best_of=3
{ ... }
```

> **Experimental.** Cost is N× a single completion. Don't enable for
> high-volume agents.

---

## AI edit pipeline (`POST /edit`)

Three-layer defence so reasoning models don't leak `<thinking>`
blocks into the file:

1. **Prose detector** — refuses to overwrite code with prose blocks.
2. **Reasoning strip** — removes `<thinking>` / `## Plan`-style
   sections.
3. **Final-shrink check** — refuses replacements that shrink the
   file by ≥ 60 % unless the prompt explicitly asked.

Plus three modes:

| Mode | Use case |
|---|---|
| `whole_file` | Rewrite the whole file (default). |
| `surgical` | Replace exactly one named symbol; rest of file is byte-identical. |
| `patch` | Apply a unified diff fenced as `\`\`\`diff …`. |

When the LLM fails, the response is HTTP 200 with a
`failure_analysis` object describing stage / root cause / suggested
fix / raw LLM excerpt — the UI uses it to show actionable error
panels.

---

## Composer integration

`POST /intelligence/context` reports LLM availability via
`capability_status[7].available`. When `false`:

- `code_understanding`, `search`, `impact`, `memory`,
  `git_history` still run.
- The composer skips any future LLM-driven enrichment passes.
- Existing AI editors connecting via MCP can still pull the
  composer payload and use *their own* LLM to summarise it.

This keeps OmniCode useful even on air-gapped boxes: pull
intelligence locally, send it to whichever AI client you trust.

---

## Disabling LLM features entirely

Set in `omnicode.toml`:

```toml
[features]
llm_router = false
ai_edit = false
```

Or env:

```bash
OMNICODE_LLM_ROUTER=false
```

The `[llm]` extras stay installed but unused; the capability
fingerprint reports the feature off.

---

## Why the strict separation

Architecture-v2 §3 (the original prompt) called out three reasons:

1. **Don't compete with Cursor / Continue / Aider.** They're far
   ahead on the chat loop. We provide *primitives* — search,
   impact, patch — they pick the model.
2. **Privacy.** Many users want OmniCode to never call out. The
   default install honours that.
3. **Air-gap operability.** All the core capabilities run with
   `TRANSFORMERS_OFFLINE=1` and a downloaded sentence-transformer.
   Adding LLM router would otherwise force a network requirement
   that isn't always acceptable.

If you find yourself rewriting `EditPipeline` to be more clever, ask
first whether the right move is making the **calling editor**
smarter (publish a docs / steering file) instead of pushing logic
into OmniCode.
