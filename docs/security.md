# Security Model

> Defence-in-depth: each layer below is independent, composable, and
> auditable. No single switch turns OmniCode-MCP into a "secure
> deployment" — you opt into the layers you need.

---

## Threat model

OmniCode-MCP is designed to handle three deployment shapes:

1. **Local single-user** — process runs as you, on your laptop, talks
   to your AI editor over stdio. Trust boundary is the OS user.
2. **Self-hosted shared cloud** — small team behind a corporate VPN,
   one VM, multi-user RBAC, HTTPS reverse proxy. Trust boundary is
   the proxy + RBAC token.
3. **Hybrid** — heavy compute on a remote VM, local agent pushes
   file bodies. Trust boundary is the agent's bearer token + remote
   workspace sandbox.

We **do not** target untrusted multi-tenant SaaS. There's no per-tenant
network sandbox, no compute quota, no pen-test against malicious
peers.

---

## Layer 1 · Path sandbox

Every endpoint that accepts a caller-supplied path runs through
`omnicode_core/security/sandbox.py::ensure_within_workspace`:

1. Reject empty / whitespace input.
2. Reject absolute paths (clueless callers can't exfiltrate
   `/etc/passwd` even when a sandbox isn't installed elsewhere).
3. Resolve against the workspace root with `Path.resolve()` — this
   normalises `..` and **follows symlinks**, so a symlink that
   points outside the workspace fails the next check.
4. Verify the resolved path is `relative_to(workspace_root)`. If
   not, raise `WorkspacePathError` (mapped to HTTP 403).

Affected endpoints:

- `POST /read` (early, before mode dispatch)
- `POST /search/update_file`
- `POST /index/upsert-file` / `upsert-batch` / `DELETE /index/file`
- `POST /file_operations`
- `POST /patch/preview` / `validate` / `apply` / `rollback`
- LSP endpoints, git endpoints, etc.

> Tested in `tests/unit/test_sandbox.py` with prefix-collision cases
> (`/srv/ws` vs `/srv/ws_admin`) and symlink-out cases. Passes on
> Windows (skips symlink test when symlink creation isn't permitted).

---

## Layer 2 · Read-only mode

When `OMNICODE_READ_ONLY=true`, `core/read_only_middleware.py`
intercepts every mutating method (POST / PUT / PATCH / DELETE) with
**one exception**: a small allow-list of POSTs that don't actually
modify state on disk:

```text
POST /search                     -- query (legacy verb)
POST /intelligence/context       -- composer
POST /patch/preview              -- diff render
POST /patch/validate             -- static analysis
POST /patch/explain              -- text summary
POST /admin/users                -- bootstrap path
```

Use case: "look-but-don't-touch" cloud deployments where remote
callers can ask questions about the codebase but never modify it.

Hybrid mode preset (`--mode hybrid`) keeps read-only **off** because
the `omnicode agent` endpoints are writes by definition.

---

## Layer 3 · Apply-patch gate

`OMNICODE_ALLOW_APPLY_PATCH=false` blocks `/patch/apply` and
`/patch/rollback` with 403 even when read-only mode is off and the
caller has editor / admin role. Used by the cloud + hybrid presets.

The preview / validate / explain endpoints stay open — the editor
still needs to render the diff to its user, just not write anything
on the wire.

---

## Layer 4 · Authentication

Two independent sources of truth, evaluated in order:

### 4a · Legacy single-key (`core/auth_middleware.py`)

`OMNICODE_API_KEY` env var. When set, every request must carry it as
either `X-API-Key: <value>` or `Authorization: Bearer <value>`.
Empty value disables the middleware. Public paths
(`/health`, `/docs`, `/redoc`, `/openapi.json`) and OPTIONS
preflights bypass the gate.

### 4b · Multi-user RBAC (`core/rbac_middleware.py`)

SQLite-backed at `~/.kiro/codebase-mcp/users.db`. Three roles:

| Role | Read | Write | `/admin/*` |
|---|---|---|---|
| `admin` | ✅ | ✅ | ✅ |
| `editor` | ✅ | ✅ | ❌ (403) |
| `viewer` | ✅ | ❌ (403 on writes) | ❌ |

Tokens are stored as **SHA-256 hashes**; the plain-text token is
returned exactly once at issue time. Format: `omn_<urlsafe-32-bytes>`.

When the user table is empty the middleware is a no-op so the first
`POST /admin/users` can bootstrap. The bootstrap call **forces**
`role=admin` and **auto-issues** a token, so there's no chicken-and-
egg lockout.

### 4c · MCP-over-HTTP gate (`omnicode_adapters/mcp_server/http_auth.py`)

The same auth sources, applied as a Starlette ASGI middleware around
FastMCP's `sse_app()` / `streamable_http_app()`. SSE handshakes get
the same 401 response when no token is present.

Stdio MCP intentionally has **no** auth — single-process, single-client,
the OS user boundary is enough.

```bash
# Refuse to start when no auth source is configured
python mcp_server.py --transport sse --port 6790 --auth required

# auto-mode: gate on if a source exists, off otherwise
python mcp_server.py --transport sse --auth auto

# unauthenticated (NOT recommended for cloud)
python mcp_server.py --transport sse --auth off
```

---

## Layer 5 · Encryption at rest

Provider API keys go through `omnicode/llm/secret_box.py`:

- Master key in `~/.kiro/codebase-mcp/providers.key` (file mode `0600`).
- Cipher: Fernet (AES-128 + HMAC).
- Encrypted blobs prefixed with `ofb1:` so legacy plain-text rows can
  be detected and migrated on first read.
- Decryption failures log a hint and refuse to return the row —
  prefer "no key" to "wrong plaintext".

### Master-key rotation

```bash
# Rotates the active key, leaves a timestamped backup, re-encrypts
# every row in a transaction. Rolls back on any failure.
omnicode rotate-master-key

# Or pin a specific key (e.g. one fetched from a secrets manager)
omnicode rotate-master-key --new-key "$(cat /path/to/new.key)"

# Or specify alternate paths
omnicode rotate-master-key --db /var/lib/omnicode/providers.db \
                            --key /var/lib/omnicode/providers.key
```

Algorithm (`omnicode_core/auth/rotation.py`):

1. Open the existing key, decrypt every row in memory.
2. Backup the old key file as `providers.key.bak.<UTC-ts>`.
3. Write the new key to disk, validate Fernet shape.
4. Re-encrypt every row in a single SQLite transaction.
5. On any failure, restore from backup and abort.

> Tested in `tests/unit/test_master_key_rotation.py` including
> idempotency (twice in a row), explicit custom keys, invalid keys,
> missing DB / key files.

---

## Layer 6 · Token expiry & revocation

Token rows have an optional `expires_at` column (added by migration
v1).

```bash
# Issue a token that expires in 90 days
curl -X POST http://server/admin/tokens \
  -H "X-API-Key: $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"username": "alice", "label": "laptop", "expires_in_days": 90}'
```

Authentication checks the column on every call. Expired tokens are
**auto-revoked** (DELETE row) on first use after expiry — keeps the
table clean without a separate sweep job.

### Departing employee

One call to invalidate every device:

```bash
curl -X DELETE http://server/admin/users/alice/tokens \
  -H "X-API-Key: $ADMIN_TOKEN"
# Returns { "username": "alice", "revoked": <count> }
```

---

## Layer 7 · Per-workspace data isolation

Each registered workspace gets its own FAISS shard at
`<wd>/.data/shards/<wk_id>/`. The `SearchEngine` mounts the active
shard's `vector_store.faiss` + `vector_store.db` so:

- Search results from workspace A can't leak into workspace B.
- Re-indexing one workspace doesn't churn another's FAISS.
- `DELETE /workspaces/{id}` drops the shard atomically.

`drop_shard()` refuses to delete the `default` shard so legacy data
stays accessible. Tested in `tests/unit/test_sharding.py`.

---

## Layer 8 · Reverse-proxy hygiene

For real cloud deployments see [`cloud-deployment.md`](cloud-deployment.md).
The recommended setup binds OmniCode to `127.0.0.1:6789`, terminates
TLS at nginx / Caddy, and enforces:

- HSTS + modern cipher suites.
- `client_max_body_size 25m` (edit operations send full file bodies).
- `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto` forwarding.
- `/admin/*` blocked from the public internet, allowed only from
  trusted CIDRs.
- `/ws/logs` upgrades work with the long-timeout block in the
  sample `nginx.conf`.

systemd units in `deploy/` ship with `ProtectSystem=strict`,
`ReadWritePaths` scoped to the workspace, `MemoryMax=4G`, and
`MemoryDenyWriteExecute`.

---

## Anti-patterns (don't do this)

1. **Don't put real secrets in `omnicode.toml`** committed to git.
   Use env vars or a secrets manager. The TOML is meant for
   *config*, not credentials.
2. **Don't run with `OMNICODE_API_KEY` AND no proxy on the public
   internet.** Single-key auth has no rate limiting; combine it with
   nginx limit-req or just use RBAC + HTTPS.
3. **Don't expose `/admin/*` publicly** even with auth — there's no
   2FA, no audit log, no rate limiter. Restrict the path at the
   proxy.
4. **Don't run `omnicode serve --mode local` on a public IP.** The
   preset doesn't enforce read-only. Use `--mode cloud`.
5. **Don't mount provider DBs from a shared filesystem across hosts.**
   `~/.kiro/codebase-mcp/providers.db` assumes a single writer; a
   shared NFS mount with two writers will corrupt the DB.
6. **Don't try to bypass `_keep_/` by editing `.gitignore`.** That
   directory is the explicit allow-list. Anything else is a hint to
   redact further.

---

## Audit checklist for new endpoints

When you add a new HTTP endpoint:

- [ ] Caller-supplied paths go through `validate_file_path` or
      `ensure_within_workspace`.
- [ ] Mutating methods sit behind one of the existing middlewares
      (no need to re-check `OMNICODE_READ_ONLY` manually unless you
      need a custom error).
- [ ] If it can leak workspace data, it's keyed by the active
      workspace shard.
- [ ] If it can write, it's blockable via
      `OMNICODE_ALLOW_APPLY_PATCH=false`.
- [ ] `/health`-style probes don't require auth.
- [ ] Error messages don't echo back full paths from disk.

---

## What if you don't enable any of this?

OmniCode-MCP runs fine. The single-user laptop case (`omnicode mcp`,
`omnicode serve` on localhost, no API key, no users) is the default
and intentionally has zero auth overhead. Every layer above is
opt-in.

The architecture-v2 prompt's promise is "the same service can be a
local trust-zero process AND a multi-tenant cloud server" — the
toggle for that is the *combination* of `--mode`, the env vars in
this doc, and the proxy / systemd / RBAC bootstrap steps in
[`cloud-deployment.md`](cloud-deployment.md).
