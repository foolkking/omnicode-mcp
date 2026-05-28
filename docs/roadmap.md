# Roadmap

> Last updated 2026-05-28.
>
> The original architecture-v2 plan (P0 + P1 + P2), the Wave 1
> audit, and the Wave 2 backlog are **all shipped**. This document
> tracks long-term research directions that are explicitly *not*
> part of 1.0 — they're parked for later milestones (1.1, 1.2,
> beyond).
>
> See [`architecture.md`](architecture.md) for what's already in.

---

## Status of shipped work

| Phase | Items | Status |
|---|---|---|
| P0 — core / adapter split, headless mode, LSP bridge, incremental index, patch ops, MCP slim, structured read modes | 7 | ✅ |
| P1 — search rerank scaffolding, memory advisory, impact, edit sessions, search debug, API key auth, Docker, GH Actions | 8 | ✅ |
| P2 — cloud / hybrid / local modes, MCP-over-HTTP, multi-workspace, RBAC, WebGL graph, multi embedding backends | 9 | ✅ |
| §17 final | composer assembly + capability fingerprint | 1 | ✅ |
| Wave 1 audit | sandbox, read-only, why_matched, REST exposure for impact + advisory, LSP rename, modes flag, MCP slim | 9 | ✅ |
| Wave 2 (10 items) | TOML, HTTPS+systemd, MCP-over-HTTP auth, local agent, master-key rotation, Web Console pages, LSP fleet, reranker, FAISS shards, VS Code ext | 10 | ✅ |
| 暂缓 (out of scope) | full chat sidebar, agent framework, SaaS billing, formatters, real-time collab | 6 | ❌ correctly not done |

### Wave 2 shipped log (2026-05)

| ID | Title | Where it landed |
|---|---|---|
| W2-1 | TOML config (`omnicode.toml`, env merge) | `omnicode_core/config/toml_loader.py` |
| W2-2 | Local agent (file-sync to remote index) | `omnicode_adapters/agent/`, `api/v1/routers/agent.py` |
| W2-3 | HTTPS reverse-proxy + systemd unit | `deploy/nginx.conf`, `deploy/omnicode.service`, `deploy/docker-compose.cloud.yml` |
| W2-4 | Master-key + token rotation (with expiry, revoke-by-user) | `omnicode_core/auth/rotation.py`, `omnicode_core/auth/migrations.py` |
| W2-5 | MCP-over-HTTP bearer-token gate | `omnicode_adapters/mcp_server/http_auth.py` |
| W2-6 | Web Console: edit-session viewer + impact viewer + memory advisory drawer | `templates/components/sections/{edit-sessions,impact-viewer}.html` |
| W2-7 | LSP fleet expansion (10 languages) | `omnicode_core/lsp/bridge.py::LSP_SERVERS` |
| W2-8 | Thin VS Code extension (3 commands, no chat) | `extensions/vscode/` |
| W2-9 | Cross-encoder reranker (opt-in) | `omnicode_core/search/reranker.py` |
| W2-10 | Per-workspace FAISS sharding + auto-migrate | `omnicode_core/index/sharding.py` |

---

## Post-1.0 research directions

The four buckets below are **investigation tracks**, not committed
features. Each lists the user need, the proposed approach, the
known unknowns, and a concrete first experiment.

### 1 · Code-specific embeddings

**User need.** `all-MiniLM-L6-v2` is a generic English sentence
encoder. Code search frequently retrieves "documentation that
*talks about* X" rather than "the function X". A code-tuned
encoder would help especially for symbol-heavy queries.

**Approach.**

- Try `unixcoder` / `codebert` / starcoder embeddings /
  `text-embedding-3-small` (OpenAI, via the existing remote
  backend).
- Add `OMNICODE_EMBEDDING_BACKEND=remote-jina` shorthand for the
  Jina v3 code embedder — they ship a HuggingFace endpoint with
  generous free tier.
- A/B harness: build a corpus of 50 hand-crafted "natural-language
  → expected hit" queries and measure NDCG@5 for each backend.

**Unknowns.** Index size grows with embedding dim — a 1024-d
backend triples FAISS memory. Need a Plan B: per-shard
quantisation.

**First experiment.** Wire `text-embedding-3-small` (1536-d, but
matryoshka-truncated to 512) and run the A/B harness. Document
the trade-off in [`usage.md`](usage.md).

### 2 · Skills framework alignment

**User need.** Anthropic's "Agent Skills" pattern packages a
*workflow* (prompts + tools + steering files) that the agent
auto-loads when it sees relevant keywords. Today, AI editors load
the entire OmniCode tool surface up front; skills could let them
load only the patch-edit subset for refactor tasks, only the
search-impact subset for code-review tasks, etc.

**Approach.**

- Define a `skills/` directory schema mirroring Kiro's existing
  `~/.kiro/skills/`.
- Ship three first-party skills:
  - `omni-impact-review` — pulls the impact + risk + advisory
    bundle for a symbol.
  - `omni-safe-refactor` — preview / validate / apply / rollback
    loop with built-in confirmations.
  - `omni-test-coverage` — runs `/graph/related-tests` and
    suggests pytest commands.
- Surface them through MCP via the `discover_tools` tool already
  present.

**Unknowns.** Cross-client portability — Cursor's MCP currently
lacks a "skills" concept. May need a vendored manifest format.

**First experiment.** Define the skill manifest and ship one
example skill plus a smoke test that verifies Kiro auto-activates
it on keyword match.

### 3 · Code-execution sandbox

**User need.** AI editors sometimes need to *run* a piece of code
to verify a fix (e.g. a regex replacement that needs to behave on
edge cases). Today the closest we have is the edit pipeline's
quality gate that runs ruff / pytest selected — useful but
limited.

**Approach.**

- Wrap the existing `execute_tool` MCP function (currently
  unsandboxed and gated off in the legacy 16) in a
  `seccomp` / `bubblewrap` / `firejail` sandbox.
- Token limits and CPU-time limits per call.
- Write-mode disabled by default (`OMNICODE_ALLOW_SHELL=false`
  already in place).

**Unknowns.** Cross-platform sandboxing is hard. Linux is doable;
macOS would need `sandbox-exec`; Windows essentially requires
`AppContainer` or running in a VM.

**First experiment.** Linux-only `bubblewrap`-backed wrapper,
gated by `OMNICODE_ALLOW_SHELL=true` AND an explicit
`X-Allow-Sandboxed-Exec` header. Refuse the call on macOS /
Windows with a clear "platform not supported" error.

### 4 · Telemetry-driven prompt feedback

**User need.** Edit sessions are stored locally
(`<wd>/.data/shards/<id>/edit_sessions/`) but no aggregate
intelligence is mined from them. We could surface "this prompt
failed 3 / 5 times in this codebase last week" via the memory
advisory endpoint.

**Approach.**

- Ingest edit sessions into the existing memory store with
  category `failed_attempt` / `successful_pattern`.
- Expose a `/memory/patterns?file=...` endpoint that returns
  prompt templates that have worked vs failed for similar files.
- The composer already has the hook (`MemoryAdvisor`); just feed
  it more.

**Unknowns.** Privacy. A user may not want their prompts mined.
This must be opt-in per-deployment.

**First experiment.** Prototype the ingestion script, gate behind
`OMNICODE_TELEMETRY_INGEST=true`, write tests that confirm zero
ingestion when the flag is unset.

---

## Known limitations to fix in 1.1

These are smaller polish items that didn't make 1.0 but should
land before a "ready for production" release:

- [ ] Per-call rate limit on `/admin/*` (currently unbounded).
- [ ] Audit log for every admin action (CSV append-only file
      under `~/.kiro/codebase-mcp/audit.log`).
- [ ] Better error envelopes from the LSP bridge — today a hung
      server times out at 30 s with a generic message.
- [ ] Idempotency keys on `/patch/apply` so a network retry
      doesn't double-apply.
- [ ] Gauge metrics endpoint
      (`/monitoring/metrics?format=prometheus`).
- [ ] First-class `--mode local-readonly` preset for "demo to a
      colleague but don't let them write" (today requires manual
      env vars).

---

## Permanent non-goals

These are listed here so future contributors don't repeatedly
propose them:

- A built-in chat sidebar — Cursor / Continue / Claude Code /
  Copilot do this; we're a service they call.
- A self-built Agent framework — LangGraph / autogen /
  smolagents are better at it. We provide *tools* for those
  frameworks.
- Multi-org SaaS billing / Stripe integration — a different
  product. Single-tenant self-host is the deployment model.
- Per-language code formatters bundled in — let `prettier` /
  `black` / `gofmt` handle that on the editor side.
- Real-time collaborative editing — that's `tldraw` / `figma`
  territory; OmniCode is "between commits".
- MCP-over-SSE protocol exposure (we already speak HTTP REST and
  MCP-stdio; SSE is a transport detail with low demand).
- WebGL / multi-million-node graph renderer — current SVG +
  canvas hybrid handles >2k nodes fine, beyond that the cost
  isn't justified.
- Multi-user permission system beyond the existing 3-role RBAC —
  single-tenant self-host with API keys covers the deployment
  shape we target.
