# Wave 2 — Post-Audit Plan

> Wave 1 (this commit) closed the audit's must-fix gaps:
> read-modes, REST exposure for impact + advisory, sandbox, read-only
> mode, LSP rename, MCP tool slim, search `why_matched`, and the
> `--mode local|cloud|hybrid` flag.
>
> Wave 2 collects everything that surfaced in the audit but was either
> too large for one commit or required a deployment story rather than
> code. Items here are **not** yet implemented and should not block
> the 1.0.0-rc1 release; they're parked for the next milestone.

---

## W2-1 · TOML configuration file

**Why.** Section 11 of the architecture prompt sketched an
`omnicode.toml` with `[server] / [workspace] / [features] / [index] /
[security]` blocks. Today everything comes from environment variables.
A TOML loader makes deployments reproducible and reviewable in version
control.

**Sketch.**
* New module `omnicode_core/config/toml_loader.py` reads
  `<working_dir>/omnicode.toml` (override path via
  `OMNICODE_CONFIG`).
* On load, populates the same env vars the existing `Settings`
  Pydantic model already understands → no schema changes needed.
* CLI: `omnicode init` writes a starter file.

**Risks.** Need a precedence rule: explicit `--flag` > env > TOML >
Pydantic default. Document this clearly.

---

## W2-2 · Local-Agent file-sync (hybrid mode glue) ✅ DONE

> Shipped in commit `<wave2-w22>`. See `omnicode_adapters/agent/` and
> `api/v1/routers/agent.py`.

**Why.** The hybrid mode currently runs the *same* server in either
local or cloud posture; what's missing is the actual *bridge* between a
remote index and a local apply. The architecture prompt called this
"omnicode-agent watch ." (Section 9-B).

**What landed.**
* Server-side: ``api/v1/routers/agent.py`` adds
  ``POST /index/upsert-file``, ``POST /index/upsert-batch``,
  ``DELETE /index/file``, ``GET /index/sync-status``,
  ``GET /index/stats``. All sandbox-checked.
* Client-side: ``omnicode_adapters/agent/`` ships ``AgentClient`` (pure
  HTTP, retry, exclude/binary filter) and ``Watcher`` (debounced
  watchfiles loop, polling fallback when watchfiles isn't installed).
* CLI: ``omnicode agent --remote URL --token TOKEN --workspace .``.
* TOML: ``[agent]`` section maps to ``OMNICODE_REMOTE``,
  ``OMNICODE_AGENT_TOKEN``, ``OMNICODE_AGENT_DEBOUNCE_MS``.
* Hybrid preset reworked: cloud index but writes-via-agent allowed
  (``OMNICODE_READ_ONLY=false``), patch-apply still blocked on the
  wire so the local editor stays the source of truth for actual writes.

**Out of scope (explicit).** Conflict resolution between concurrent
editors; large-file streaming; end-to-end encryption; pull-mode patch
suggestions.

---

## W2-3 · HTTPS reverse-proxy & systemd unit

**Why.** Cloud mode without TLS is not a serious deployment.

**Deliverables.**
* `deploy/nginx.conf` — minimal reverse proxy with TLS termination.
* `deploy/omnicode.service` — systemd unit that runs
  `omnicode serve --headless --mode cloud --host 127.0.0.1 --port 6789`.
* `deploy/docker-compose.cloud.yml` — adds caddy / traefik in front of
  the existing app container.
* `docs/cloud-deployment.md` — step-by-step.

**Why W2 not W1.** Pure ops; no code change inside the app itself.

---

## W2-4 · Master-key & token rotation

**Why.** Provider keys are encrypted at rest with Fernet (good), but the
master key is read from `OMNICODE_MASTER_KEY` once at startup. There is
no rotation path. Cloud deployments need:
* A `rotate-master-key` CLI command that re-encrypts every row.
* Token expiry on RBAC tokens (`expires_at` column added to
  `tokens`).
* A revoke-by-username operation (currently you must list tokens and
  revoke each by hash).

**Schema migration.** First real `users.db` migration; a chance to put a
basic migration helper into `omnicode_core/auth/migrations.py`.

---

## W2-5 · MCP-over-HTTP bearer-token gate

**Why.** Wave 1 made the FastMCP server runnable with `--transport sse`
or `--transport streamable-http`, but those transports inherit nothing
from the FastAPI auth middleware. Anyone who can hit the SSE port can
call any tool.

**Plan.**
* Add a small ASGI wrapper around the FastMCP HTTP transport that
  enforces the same `OMNICODE_API_KEY` / RBAC token check.
* CLI: `omnicode mcp --transport sse --auth required`.
* Document the recommended pattern: stdio for local, sse + reverse
  proxy with mTLS for cloud.

---

## W2-6 · Web Console: edit-session + impact viewer

**Why.** The composer surfaces impact + edit-sessions, but the
console still only shows the existing graph viewer + provider page. We
need:
* Edit-Session list page (drives `GET /patch/sessions`).
* Impact viewer (drives `GET /graph/impact`, `/graph/risk`).
* Memory advisory drawer in the search results page.

**Why W2.** Wave 1 already exposed all four endpoints; the front-end
work is independent and can ship separately.

---

## W2-7 · LSP fleet expansion

**Why.** Wave 1 added rename across the existing 5-server fleet
(pyright / tsserver / gopls / rust-analyzer / clangd). Long tail:
* Ruby (solargraph)
* PHP (intelephense)
* Java (jdtls — heavy, maybe via Eclipse JDT LS Docker image)
* Kotlin (kotlin-language-server)
* C# (omnisharp / roslyn-language-server)

Each addition is an entry in `LSP_SERVERS` plus a smoke test.

---

## W2-8 · VS Code extension (very thin)

**Why.** Cursor / Continue / Aider already work via MCP stdio. A
purpose-built VS Code extension would only need to:
* Add a "OmniCode: Show Impact" command that calls `/graph/impact`.
* Add an "OmniCode: Apply Patch" command that calls `/patch/apply`
  with a confirm prompt.
* Display the capability fingerprint in the status bar.

Strict limit: **no chat UI, no AI editor — that's the rest of the
ecosystem's job.**

---

## W2-9 · Reranker (cross-encoder) for hybrid search

**Why.** Wave 1's search now reports *why* a result matched; Wave 2's
reranker should make the order itself better. Plan:
* Optional cross-encoder model (`bge-reranker-v2-m3`) loaded only when
  `OMNICODE_RERANKER=true`.
* Scores top-50 candidates and picks the top-K.
* Adds another `why_matched` tag (`reranked`) for transparency.

Out of scope: training a custom reranker on the project's own
edit-session data (interesting but premature).

---

## W2-10 · Multi-tenant FAISS sharding

**Why.** Workspaces today share a single `vector_store.faiss`. The
workspace registry already has separate IDs; the index should follow
suit so cloud-mode tenants can't trip over each other's chunks.

**Plan.** Per-workspace data dir at `<wd>/.data/wk_<id>/` and a router
shard cache keyed by workspace id.

---

## Priority ordering

1. **W2-3** HTTPS reverse-proxy & systemd — biggest blocker for any
   real cloud deployment; all code, docs, and presets.
2. **W2-5** MCP-over-HTTP auth — security hole if W2-3 ships first.
3. **W2-1** TOML config — quality-of-life.
4. **W2-2** Local agent — needed once two people actually try hybrid.
5. **W2-4** Rotation — important once real users exist.
6. **W2-9** Reranker — biggest quality jump for search.
7. **W2-6** Web Console pages.
8. **W2-7** LSP fleet expansion (incrementally as users ask).
9. **W2-10** FAISS sharding (only after W2-1 + W2-2 land).
10. **W2-8** VS Code extension (last, polish).

## Out of scope for the foreseeable future

Same as architecture-v2 §16 "暂缓":
* Full chat-style AI editor — Cursor's territory.
* Self-built Agent framework — LangGraph / autogen do this better.
* SaaS billing / multi-org — needs a different team.

---

*Updated 2026-05-27, after the Wave 1 audit closeout.*
