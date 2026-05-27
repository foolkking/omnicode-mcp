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

## W2-4 · Master-key & token rotation ✅ DONE

> Shipped in commit `6dcb81c`. See `omnicode_core/auth/migrations.py`,
> `omnicode_core/auth/rotation.py`, `omnicode_adapters/cli/commands/rotate_cmd.py`.

* SQLite migration runner uses `PRAGMA user_version`. First migration
  adds `tokens.expires_at`.
* `issue_token(..., expires_in_days=N)` writes the column; auth
  auto-revokes on first use after expiry.
* `revoke_user_tokens(username)` + `DELETE /admin/users/{u}/tokens`
  REST endpoint for the "departing employee" scenario.
* `omnicode rotate-master-key [--db ...] [--key ...] [--new-key BASE64]`
  re-encrypts every provider row under a fresh Fernet key, with a
  timestamped backup of the old key file and rollback on any failure.
* 15 new unit tests (8 expiry + 7 rotation).

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

## W2-6 · Web Console: edit-session + impact viewer ✅ DONE

> Shipped in commit `9cb2de9`. See `templates/components/sections/edit-sessions.html`,
> `templates/components/sections/impact-viewer.html`, and the new
> sidebar entries.

* **Edit Sessions** — list (left) + detail (right) with diff render,
  checks_before/after panels, and one-click rollback.
* **Impact Viewer** — type a symbol, parallel-fire `/graph/impact`,
  `/graph/risk`, `/graph/related-tests`. Returns blast radius, callers,
  callees, files, suggested tests + commands, and a low/medium/high
  risk badge.
* **Memory Advisory drawer** in search results — every result row now
  shows `why_matched` chips and a "Memory advisory" button that
  inline-loads `/memory/advisory` for that file/symbol.
* `apiRoutes.patch.*`, `apiRoutes.graph.*`, `apiRoutes.advisory.*`
  added so future panels can call the same endpoints without
  reinventing.

---

## W2-7 · LSP fleet expansion ✅ DONE

> Shipped in commit `ec5c0d2`. See `omnicode_core/lsp/bridge.py`
> ``LSP_SERVERS`` table.

Added `ruby` (solargraph), `php` (intelephense), `java` (jdtls),
`kotlin` (kotlin-language-server), `csharp` (omnisharp). Doctor
checks all 10 servers now. 19 unit tests + 10 binary-resolution
probes that auto-skip when the server isn't installed locally.

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

## W2-9 · Reranker (cross-encoder) for hybrid search ✅ DONE

> Shipped in commit `6d76ca7`. See `omnicode_core/search/reranker.py`.

Three-tier abstraction: `Reranker` (base), `NoOpReranker` (default
zero-cost passthrough), `BGEReranker` (lazy-loaded
`BAAI/bge-reranker-v2-m3` cross-encoder). Toggle with
``OMNICODE_RERANKER=true``; failure to load the model falls back to
NoOp without exceptions. Promoted items get `"reranked"` appended to
their `why_matched` tag list and the bi-encoder score preserved on
`bi_encoder_score`. 10 unit tests covering enable/disable, predict
failure, empty input, and identity ordering.

---

## W2-10 · Multi-tenant FAISS sharding

**Why.** Workspaces today share a single `vector_store.faiss`. The
workspace registry already has separate IDs; the index should follow
suit so cloud-mode tenants can't trip over each other's chunks.

**Plan.** Per-workspace data dir at `<wd>/.data/wk_<id>/` and a router
shard cache keyed by workspace id.

---

## Priority ordering

Done so far (in chronological order):
1. **W2-3** HTTPS reverse-proxy & systemd ✅
2. **W2-5** MCP-over-HTTP auth ✅
3. **W2-1** TOML config ✅
4. **W2-2** Local agent ✅
5. **W2-7** LSP fleet expansion ✅
6. **W2-9** Reranker ✅
7. **W2-4** Master-key + token rotation ✅
8. **W2-6** Web Console new pages ✅

Still parked:
9. **W2-10** FAISS sharding (per-workspace shards) — needed before
   real multi-tenant cloud.
10. **W2-8** VS Code extension — polish, do last.

## Out of scope for the foreseeable future

Same as architecture-v2 §16 "暂缓":
* Full chat-style AI editor — Cursor's territory.
* Self-built Agent framework — LangGraph / autogen do this better.
* SaaS billing / multi-org — needs a different team.

---

*Updated 2026-05-27, after the Wave 1 audit closeout.*
