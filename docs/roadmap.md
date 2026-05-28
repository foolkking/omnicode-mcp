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

### 2 · Skills framework alignment ✅ shipped (P2-B)

**User need (still relevant).** Anthropic's "Agent Skills" pattern
packages a *workflow* (prompts + tools + steering files) that the
agent auto-loads when it sees relevant keywords.

**What landed.** ``omnicode_core/skills/`` registers a
documentation-only skills framework with three first-party
recipes (``omni-impact-review``, ``omni-safe-refactor``,
``omni-test-coverage``). The MCP tool ``omni_skill`` lists / shows
the recipes; the AI editor follows them itself. OmniCode never
auto-executes a skill, which keeps the trust model simple.

**Open work.** Cross-client portability. Cursor's MCP currently
lacks a "skills" concept — for now AI editors that want to use
OmniCode skills call ``omni_skill(action='show', name=…)`` and
follow the steps manually.

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

### 4 · Telemetry-driven prompt feedback ⚙️ partially shipped (P1-4)

**What landed.** Failure ingest — when an LLM-driven `/edit` fails
and ``OMNICODE_TELEMETRY_INGEST=true``, a ``MISTAKE``-category
memory is recorded with the instructions excerpt + first 3 failure
reasons. The Memory Advisor then surfaces it on the next similar
prompt. Default off for privacy.

**Still open.**

- ``/memory/patterns?file=...`` endpoint that returns prompt
  templates that have worked vs failed for similar files.
- Aggregation across edit-session shards (currently one-row-per-
  failure, no rollup).
- Successful-pattern ingest (the success branch already writes a
  `SOLUTION` memory but doesn't categorise prompt patterns).

---

## Known limitations to fix in 1.1

All shipped — see commit history `5df92ab` (P2-batch-A), `aa46595`
(LSP envelopes), `edcd5cf` (failure-memory + no-LLM CI), `681e5d7`
(skills framework).

- [x] Per-call rate limit on `/admin/*` — token-bucket per IP, default
      30 req/min, tune via `OMNICODE_ADMIN_RATE_LIMIT`.
- [x] Audit log for admin + patch actions — CSV append-only at
      `~/.kiro/codebase-mcp/audit.log`, override via
      `OMNICODE_AUDIT_LOG`.
- [x] Better error envelopes from the LSP bridge — `LSPTimeout`
      structured 504 with method / elapsed / hint.
- [x] Idempotency-Key on `/patch/apply` — SQLite-backed cache, same
      key + payload → cached response, conflict → 409.
- [x] Prometheus metrics endpoint — `GET /monitoring/metrics?format=prometheus`
      (or `format=json`). Counters + histograms, no extra deps.
- [x] First-class `--mode local-readonly` preset.
- [x] Auto failure-memory ingest — opt-in via `OMNICODE_TELEMETRY_INGEST`.
- [x] Skills framework alignment — three first-party skills shipped,
      drop user manifests under `~/.kiro/skills/`.

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
