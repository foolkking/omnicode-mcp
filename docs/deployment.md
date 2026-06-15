# Deployment & Security

This document covers production deployment patterns and the
security model that underpins them. The two are intertwined:
defence-in-depth only matters if your reverse proxy and systemd
unit are configured to actually invoke it.

> Local single-user laptops can ignore most of this — that's the
> default mode and it intentionally has zero auth overhead. If
> you're putting OmniCode-MCP on a VM that anyone else can reach,
> read this end to end.

---

## Table of contents

- [Threat model](#threat-model)
- [Deployment patterns](#deployment-patterns)
  - [Pattern A — systemd + nginx](#pattern-a--systemd--nginx-on-a-single-vm)
  - [Pattern B — Docker Compose + Caddy](#pattern-b--docker-compose-with-caddy)
- [Embedding models in deployment](#embedding-models-in-deployment)
- [Security layers (defence in depth)](#security-layers-defence-in-depth)
  - [1 · Path sandbox](#layer-1--path-sandbox)
  - [2 · Read-only mode](#layer-2--read-only-mode)
  - [3 · Apply-patch gate](#layer-3--apply-patch-gate)
  - [4 · Authentication (3 sources)](#layer-4--authentication)
  - [5 · Encryption at rest + master-key rotation](#layer-5--encryption-at-rest)
  - [6 · Token expiry & revocation](#layer-6--token-expiry--revocation)
  - [7 · Per-workspace data isolation](#layer-7--per-workspace-data-isolation)
  - [8 · Reverse-proxy hygiene](#layer-8--reverse-proxy-hygiene)
- [Hardening checklist](#hardening-checklist)
- [Anti-patterns (don't do this)](#anti-patterns-dont-do-this)
- [Audit checklist for new endpoints](#audit-checklist-for-new-endpoints)
- [Troubleshooting](#troubleshooting)
- [Why cloud mode defaults to read-only](#why-cloud-mode-defaults-to-read-only)

---

## Threat model

OmniCode-MCP targets three deployment shapes:

1. **Local single-user** — process runs as you, on your laptop,
   talks to your AI editor over stdio. Trust boundary is the OS
   user.
2. **Self-hosted shared cloud** — small team behind a corporate
   VPN, one VM, multi-user RBAC, HTTPS reverse proxy. Trust
   boundary is the proxy + RBAC token.
3. **Hybrid** — heavy compute on a remote VM, local agent pushes
   file bodies. Trust boundary is the agent's bearer token + the
   remote workspace sandbox.

We **do not** target untrusted multi-tenant SaaS. There's no
per-tenant network sandbox, no compute quota, no pen-test against
malicious peers.

---

## Deployment patterns

Both patterns keep OmniCode-MCP itself bound to loopback and put a
TLS-terminating reverse proxy in front. The only difference is the
tooling.

### Pattern A — systemd + nginx on a single VM

Suitable for a 4 vCPU / 8 GB cloud VM running a single project.

#### 1. Provision

```bash
# As a non-root sudoer
sudo useradd -r -s /usr/sbin/nologin omnicode
sudo mkdir -p /opt/omnicode /srv/omnicode/workspaces /etc/omnicode \
              /var/log/omnicode
sudo chown -R omnicode:omnicode /opt/omnicode /srv/omnicode \
                                /var/log/omnicode

# Install Python 3.11 and create a venv
sudo apt install -y python3.11 python3.11-venv git
sudo -u omnicode python3.11 -m venv /opt/omnicode/.venv
sudo -u omnicode /opt/omnicode/.venv/bin/pip install --upgrade pip
sudo -u omnicode /opt/omnicode/.venv/bin/pip install \
    -e /opt/omnicode/codebase-mcp
```

Drop your project into `/srv/omnicode/workspaces/project-a`
(typically via `git clone`).

#### 2. Configure

```bash
sudo cp /opt/omnicode/codebase-mcp/omnicode.example.toml \
        /etc/omnicode/omnicode.toml
sudo chown omnicode:omnicode /etc/omnicode/omnicode.toml
sudo chmod 640 /etc/omnicode/omnicode.toml
```

Important keys for cloud:

```toml
[server]
mode = "cloud"
host = "127.0.0.1"
port = 6789

[workspace]
root = "/srv/omnicode/workspaces/project-a"
read_only = true            # browse-only

[security]
api_key = "REPLACE_ME"      # or use RBAC below
allow_apply_patch = false
```

For multi-user RBAC, leave `api_key` blank and bootstrap an admin
via `POST /admin/users` after the server is up.

#### 3. Install the systemd unit

```bash
sudo cp /opt/omnicode/codebase-mcp/deploy/omnicode.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omnicode
journalctl -u omnicode -f
```

For MCP-over-HTTP (only do this if remote AI editors will connect
— local AI editors should keep using stdio):

```bash
sudo cp /opt/omnicode/codebase-mcp/deploy/omnicode-mcp.service \
        /etc/systemd/system/
sudo systemctl enable --now omnicode-mcp
```

#### 4. nginx + Let's Encrypt

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp /opt/omnicode/codebase-mcp/deploy/nginx.conf \
        /etc/nginx/sites-available/omnicode
sudo ln -s /etc/nginx/sites-available/omnicode \
           /etc/nginx/sites-enabled/omnicode
# edit server_name BEFORE first reload
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d omnicode.example.com
```

#### 5. Verify

```bash
curl https://omnicode.example.com/health
curl -H "X-API-Key: REPLACE_ME" \
     https://omnicode.example.com/capabilities
```

Both should return 200. The first `curl` works without the header
because `/health` is on the public allow-list.

For hybrid deployments, run the durability soak before treating a build as
production-ready:

```bash
python scripts/soak_hybrid_durability.py \
  --duration-s 1800 \
  --max-iterations 0 \
  --sleep-s 1 \
  --rollback-every 5 \
  --cloud-down-at 3 \
  --reset-state \
  --json
```

The soak creates a throwaway workspace, starts a temporary cloud-index backend,
applies safe local edits, validates exact search freshness, rolls edits back,
simulates a cloud outage, and verifies the pending sync queue drains after the
backend restarts.

---

### Hybrid MCP + cloud sync

Hybrid mode keeps file authority local while allowing cloud compute for
search, context, and impact analysis. The local MCP process owns
`omni_read` and `omni_patch`; cloud-backed tools run only after
`/sync/barrier` confirms the indexed revision is current.

Local `omnicode.toml`:

```toml
[workspace]
root = "C:/repo/project-a"
id = "project-a"

[mcp]
executor = "hybrid"
transport = "stdio"

[cloud]
url = "https://omnicode.example.com"
auth_mode = "token"
token_env = "OMNICODE_API_KEY"

[sync]
mode = "smart"
agent = "auto"
debounce_ms = 1200
max_file_bytes = 1000000
batch_max_files = 25
batch_max_bytes = 250000

[capabilities]
llm_mode = "off"
embedding_mode = "cloud"
diagnostics_mode = "local-first"
```

Cloud side must register the same workspace id:

```bash
curl -X POST https://omnicode.example.com/workspaces \
  -H "X-API-Key: $OMNICODE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "workspace_id": "project-a",
    "name": "project-a",
    "path": "/srv/omnicode/workspaces/project-a-cache",
    "set_active": true
  }'
```

Run local MCP:

```bash
omnicode mcp \
  --workspace C:/repo/project-a \
  --workspace-id project-a \
  --executor hybrid \
  --backend-url https://omnicode.example.com \
  --backend-token "$OMNICODE_API_KEY" \
  --sync-mode smart \
  --agent auto \
  --embedding-mode cloud \
  --diagnostics-mode local-first
```

The sync protocol uses:

- `POST /sync/batch` for accepted file/deletion batches.
- `GET /sync/status` for `accepted_revision`, `indexed_revision`, and
  snapshot counts.
- `POST /sync/barrier` before cloud `omni_search`, `omni_context`, or
  `omni_impact` execution.

Verification:

```bash
curl -H "X-API-Key: $OMNICODE_API_KEY" \
     -H "X-Omnicode-Workspace: project-a" \
     https://omnicode.example.com/sync/status
```

Then call MCP `omni_status()`. A healthy hybrid setup reports:

- `sync.configured = true`
- `sync.routes.omni_read.local_authority = true`
- `sync.routes.omni_patch.local_authority = true`
- `sync.routes.omni_search.requires_barrier = true`
- `capability_contract.embedding.target = "cloud"` with
  `available = true` only when the cloud backend URL is configured
- `agent_auto.target = "embedded"` and `should_start = true` when
  `agent = "auto"`, `executor = "hybrid"`, sync is enabled, and cloud is
  configured

Cloud storage for accepted sync snapshots lives under
`<OMNICODE_STATE_DIR>/cloud-sync/workspaces/<workspace_id>/` when
`OMNICODE_STATE_DIR` is configured, otherwise under the default user state
directory. It is content-addressed and stores only workspace-relative paths
in the index. The readonly mirror is for cloud analysis only; the user's
local checkout remains the file authority.

Do not point the cloud backend at the user's real local project directory
when simulating hybrid on one machine. Use a separate cloud mirror root:

```text
local project:        <PROJECT_ROOT>
cloud mirror root:    <CLOUD_WORKSPACE_ROOT>/<workspace_id>
local state:          <STATE_DIR>/local
cloud state:          <STATE_DIR>/cloud
embedding cache:      <MODEL_CACHE>
```

---

### Pattern B — Docker Compose with Caddy

Suitable when your VM already runs other Docker workloads or you
want TLS without the certbot dance.

#### 1. Provision

```bash
git clone <your-fork> /opt/omnicode
cd /opt/omnicode
cp omnicode.example.toml omnicode.toml      # edit this
echo "OMNICODE_API_KEY=$(openssl rand -hex 32)" > .env
```

Update `deploy/Caddyfile` to point at your real hostname.

#### 2. Bring it up

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/docker-compose.cloud.yml \
  up -d
```

The base `docker-compose.yml` already builds the OmniCode image;
the overlay forces cloud-mode env vars, drops the loopback port
mapping, and adds the Caddy proxy. Caddy auto-provisions a TLS
cert on first request.

#### 3. Verify

```bash
curl https://your-host/health
curl -H "X-API-Key: $(grep OMNICODE_API_KEY .env | cut -d= -f2)" \
     https://your-host/capabilities
```

---

## Embedding models in deployment

Semantic search is optional and should be treated as an enhancement over the
deterministic exact index. For predictable startup, pre-download embedding
models during deployment rather than allowing the service to download at
runtime.

Supported models:

| Model | Suggested use |
|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | Local default; small and fast |
| `BAAI/bge-small-en-v1.5` | Cloud/hybrid default candidate |
| `intfloat/e5-small-v2` | Small E5 alternative |
| `sentence-transformers/all-mpnet-base-v2` | Larger local option |

Deployment steps:

```bash
omnicode models pull \
  --model sentence-transformers/all-MiniLM-L6-v2 \
  --cache-dir <MODEL_CACHE>

export OMNICODE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
export OMNICODE_EMBEDDING_CACHE_DIR=<MODEL_CACHE>
export OMNICODE_EMBEDDING_LOCAL_FILES_ONLY=true
export OMNICODE_EMBEDDING_DEVICE=cpu
```

For cloud/hybrid, set the model to `BAAI/bge-small-en-v1.5` if you have
pre-downloaded it and sized memory accordingly.

`omni_status()` and `omnicode models status` must show the selected model,
dimension, cache directory, local-files-only state, and whether a download is
required. If the model is absent with local-files-only enabled, the service
continues to run and reports `EMBEDDING_MODEL_NOT_FOUND`; exact
symbol/text/read/patch workflows remain available.

FAISS semantic indexes are tied to embedding metadata:

- embedding model id
- embedding revision
- embedding dimension
- embedding backend
- chunker version
- normalization
- workspace id

Changing model or dimension marks the semantic index stale/invalid. Rebuild
only the semantic index; do not rebuild deterministic exact search unless
files changed.

---

## Security layers (defence in depth)

Each layer below is independent, composable, and auditable. No
single switch turns OmniCode-MCP into a "secure deployment" — you
opt into the layers you need.

### Layer 1 · Path sandbox

Every endpoint that accepts a caller-supplied path runs through
`omnicode_core/security/sandbox.py::ensure_within_workspace`:

1. Reject empty / whitespace input.
2. Reject absolute paths (clueless callers can't exfiltrate
   `/etc/passwd` even when a sandbox isn't installed elsewhere).
3. Resolve against the workspace root with `Path.resolve()` —
   normalises `..` and **follows symlinks**, so a symlink that
   points outside the workspace fails the next check.
4. Verify the resolved path is `relative_to(workspace_root)`. If
   not, raise `WorkspacePathError` (HTTP 403).

Affected endpoints: `POST /read`, `POST /search/update_file`,
`POST /index/upsert-file` / `upsert-batch` / `DELETE /index/file`,
`POST /file_operations`, every `POST /patch/*`, every LSP and git
endpoint.

> Tested in `tests/unit/test_sandbox.py` with prefix-collision
> cases (`/srv/ws` vs `/srv/ws_admin`) and symlink-out cases.
> Passes on Windows (skips symlink test when symlink creation
> isn't permitted).

### Layer 2 · Read-only mode

When `OMNICODE_READ_ONLY=true`, `core/read_only_middleware.py`
intercepts every mutating method (POST / PUT / PATCH / DELETE)
with **one exception**: a small allow-list of POSTs that don't
actually modify state on disk:

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

Hybrid mode preset (`--mode hybrid`) keeps read-only **off**
because the `omnicode agent` endpoints are writes by definition.

### Layer 3 · Apply-patch gate

`OMNICODE_ALLOW_APPLY_PATCH=false` blocks `/patch/apply` and
`/patch/rollback` with 403 even when read-only mode is off and
the caller has editor / admin role. Used by the cloud + hybrid
presets.

The preview / validate / explain endpoints stay open — the editor
still needs to render the diff to its user, just not write
anything on the wire.

### Layer 4 · Authentication

Three independent sources, evaluated in order:

#### 4a · Legacy single-key (`core/auth_middleware.py`)

`OMNICODE_API_KEY` env var. When set, every request must carry it
as either `X-API-Key: <value>` or `Authorization: Bearer <value>`.
Empty value disables the middleware. Public paths (`/health`,
`/docs`, `/redoc`, `/openapi.json`) and OPTIONS preflights bypass
the gate.

#### 4b · Multi-user RBAC (`core/rbac_middleware.py`)

SQLite-backed at `~/.kiro/codebase-mcp/users.db`. Three roles:

| Role | Read | Write | `/admin/*` |
|---|---|---|---|
| `admin` | ✅ | ✅ | ✅ |
| `editor` | ✅ | ✅ | ❌ (403) |
| `viewer` | ✅ | ❌ (403 on writes) | ❌ |

Tokens are stored as **SHA-256 hashes**; the plain-text token is
returned exactly once at issue time. Format:
`omn_<urlsafe-32-bytes>`.

When the user table is empty the middleware is a no-op so the
first `POST /admin/users` can bootstrap. The bootstrap call
**forces** `role=admin` and **auto-issues** a token, so there's
no chicken-and-egg lockout.

#### 4c · MCP-over-HTTP gate

`omnicode_adapters/mcp_server/http_auth.py`. The same auth
sources, applied as a Starlette ASGI middleware around FastMCP's
`sse_app()` / `streamable_http_app()`. SSE handshakes get the
same 401 response when no token is present.

Stdio MCP intentionally has **no** auth — single-process,
single-client, the OS user boundary is enough.

```bash
# Refuse to start when no auth source is configured
omnicode mcp --transport sse --port 6790 --auth required

# auto-mode: gate on if a source exists, off otherwise
omnicode mcp --transport sse --auth auto

# unauthenticated (NOT recommended for cloud)
omnicode mcp --transport sse --auth off
```

For local editors that only speak stdio, keep the MCP process local
and point it at the cloud FastAPI backend:

```bash
omnicode mcp \
  --backend-url https://omnicode.example.com \
  --backend-token "$OMNICODE_API_KEY" \
  --workspace C:/repo \
  --workspace-id repo-a \
  --executor hybrid
```

The local bridge sends tool requests to the remote backend with
`X-API-Key`, `X-Omnicode-Workspace`, and `X-Omnicode-Executor`.
If the editor supports SSE or streamable HTTP directly, connect it to
the cloud MCP transport instead.

For hybrid deployments, register the same logical id on the cloud
before starting the local agent:

```bash
curl -X POST https://omnicode.example.com/workspaces \
  -H "X-API-Key: $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"workspace_id":"repo-a","name":"repo-a","path":"/srv/omnicode/workspaces/repo-a-cache","set_active":true}'

omnicode agent \
  --remote https://omnicode.example.com \
  --token "$OMNICODE_AGENT_TOKEN" \
  --workspace C:/repo \
  --workspace-id repo-a
```

The `/index/*` endpoints reject unknown or inactive `workspace_id`
values. This is intentional: it prevents a local checkout from being
indexed into the wrong cloud workspace.

### Layer 5 · Encryption at rest

Provider API keys go through `omnicode/llm/secret_box.py`:

- Master key in `~/.kiro/codebase-mcp/providers.key` (file mode
  `0600`).
- Cipher: Fernet (AES-128 + HMAC).
- Encrypted blobs prefixed with `ofb1:` so legacy plain-text rows
  can be detected and migrated on first read.
- Decryption failures log a hint and refuse to return the row —
  prefer "no key" to "wrong plaintext".

#### Master-key rotation

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
> idempotency (twice in a row), explicit custom keys, invalid
> keys, missing DB / key files.

### Layer 6 · Token expiry & revocation

Token rows have an optional `expires_at` column (added by
migration v1).

```bash
# Issue a token that expires in 90 days
curl -X POST http://server/admin/tokens \
  -H "X-API-Key: $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"username": "alice", "label": "laptop", "expires_in_days": 90}'
```

Authentication checks the column on every call. Expired tokens
are **auto-revoked** (DELETE row) on first use after expiry —
keeps the table clean without a separate sweep job.

Departing-employee scenario, one call to invalidate every device:

```bash
curl -X DELETE http://server/admin/users/alice/tokens \
  -H "X-API-Key: $ADMIN_TOKEN"
# Returns { "username": "alice", "revoked": <count> }
```

### Layer 7 · Per-workspace data isolation

Each registered workspace gets its own FAISS shard at
`<wd>/.data/shards/<wk_id>/`. The `SearchEngine` mounts the
active shard's `vector_store.faiss` + `vector_store.db` so:

- Search results from workspace A can't leak into workspace B.
- Re-indexing one workspace doesn't churn another's FAISS.
- `DELETE /workspaces/{id}` drops the shard atomically.

`drop_shard()` refuses to delete the `default` shard so legacy
data stays accessible. Tested in `tests/unit/test_sharding.py`.

### Layer 8 · Reverse-proxy hygiene

The recommended setup binds OmniCode to `127.0.0.1:6789`,
terminates TLS at nginx / Caddy, and enforces:

- HSTS + modern cipher suites.
- `client_max_body_size 25m` (edit operations send full file
  bodies).
- `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto` forwarding.
- `/admin/*` blocked from the public internet, allowed only from
  trusted CIDRs.
- `/ws/logs` upgrades work with the long-timeout block in the
  sample `nginx.conf`.

systemd units in `deploy/` ship with `ProtectSystem=strict`,
`ReadWritePaths` scoped to the workspace, `MemoryMax=4G`, and
`MemoryDenyWriteExecute`.

---

## Hardening checklist

- [ ] OmniCode bound to `127.0.0.1`, never `0.0.0.0`, when behind
      a proxy.
- [ ] TLS termination at the proxy with HSTS + modern cipher
      suites.
- [ ] `OMNICODE_MODE=cloud` (so read-only and apply-blocked are
      the default; flip them deliberately when needed).
- [ ] `OMNICODE_ALLOW_SHELL=false` (reserved for future use of
      the `execute_tool`; opt in only if you've audited callers).
- [ ] `/admin/*` blocked at the proxy except from a trusted CIDR.
- [ ] MCP-over-HTTP behind `--auth required` so the server
      refuses to start if neither `OMNICODE_API_KEY` nor an RBAC
      user exists.
- [ ] Workspace path on a dedicated mount; nginx/Caddy
      `client_max_body_size` tuned so large diffs aren't
      truncated.
- [ ] Log rotation for `/var/log/omnicode` (logrotate or
      journal-only).
- [ ] Backups of `~/.kiro/codebase-mcp/` (provider DB, users DB,
      workspaces.json) — they live outside the project tree on
      purpose.

---

## Anti-patterns (don't do this)

1. **Don't put real secrets in `omnicode.toml`** committed to git.
   Use env vars or a secrets manager. The TOML is meant for
   *config*, not credentials.
2. **Don't run with `OMNICODE_API_KEY` AND no proxy on the public
   internet.** Single-key auth has no rate limiting; combine it
   with nginx limit-req or just use RBAC + HTTPS.
3. **Don't expose `/admin/*` publicly** even with auth — there's
   no 2FA, no audit log, no rate limiter. Restrict the path at
   the proxy.
4. **Don't run `omnicode serve --mode local` on a public IP.**
   The preset doesn't enforce read-only. Use `--mode cloud`.
5. **Don't mount provider DBs from a shared filesystem across
   hosts.** `~/.kiro/codebase-mcp/providers.db` assumes a single
   writer; a shared NFS mount with two writers will corrupt the
   DB.
6. **Don't try to bypass `_keep_/` by editing `.gitignore`.**
   That directory is the explicit allow-list. Anything else is a
   hint to redact further.

---

## Audit checklist for new endpoints

When you add a new HTTP endpoint:

- [ ] Caller-supplied paths go through `validate_file_path` or
      `ensure_within_workspace`.
- [ ] Mutating methods sit behind one of the existing middlewares
      (no need to re-check `OMNICODE_READ_ONLY` manually unless
      you need a custom error).
- [ ] If it can leak workspace data, it's keyed by the active
      workspace shard.
- [ ] If it can write, it's blockable via
      `OMNICODE_ALLOW_APPLY_PATCH=false`.
- [ ] `/health`-style probes don't require auth.
- [ ] Error messages don't echo back full paths from disk.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` on `/` | systemd unit failed | `journalctl -u omnicode -n 50` |
| `401` on every call after enabling auth | client missing header | `X-API-Key: …` or `Authorization: Bearer …` |
| `403 read-only` on legitimate writes | cloud preset still active | set `OMNICODE_ALLOW_APPLY_PATCH=true` *and* `OMNICODE_READ_ONLY=false` |
| MCP-over-HTTP refuses to start | `--auth required` and no key/user | set `OMNICODE_API_KEY` or bootstrap a user |
| Slow startup on every restart | cold sentence-transformers download | pre-download in the Docker image (already done by Dockerfile) |
| `/admin/users` keeps 401-ing the bootstrap call | a user already exists | RBAC is now active — provide a token |

---

## Why cloud mode defaults to read-only

Cloud mode keeps "writes from the wire" off by default because:

1. **Blast radius.** A wrong patch applied via a remote endpoint
   can silently corrupt the source tree. Local editors apply
   patches through their own confirm-prompt UX; the cloud server
   has none.
2. **Audit trail.** `/patch/preview` + `/patch/validate` +
   `/patch/explain` are still open under read-only mode, so an
   editor can show the diff and the analysis to the user before
   doing the actual write on the user's local machine.
3. **Hybrid story.** This matches the long-term hybrid mode:
   cloud for "understanding", local for "applying".

If you really need remote write — say a sandboxed CI agent —
flip `OMNICODE_ALLOW_APPLY_PATCH=true` and require RBAC role
`editor` or `admin` for the calling token. Don't open it for the
legacy single key.

---

## What if you don't enable any of this?

OmniCode-MCP runs fine. The single-user laptop case
(`omnicode mcp`, `omnicode serve` on localhost, no API key, no
users) is the default and intentionally has zero auth overhead.
Every layer above is opt-in.

The promise of the architecture is "the same service can be a
local trust-zero process AND a multi-tenant cloud server" — the
toggle for that is the *combination* of `--mode`, the env vars
in this doc, the proxy / systemd / RBAC bootstrap steps in
[Pattern A or B](#deployment-patterns), and the layers above.
