# Cloud Deployment Guide

This guide covers two production-ready deployment patterns. Both keep
OmniCode-MCP itself bound to loopback and put a TLS-terminating reverse
proxy in front; the only difference is the tooling.

> Wave 2, W2-3. Pairs with the `omnicode.toml` file format from W2-1
> and the MCP-over-HTTP auth gate from W2-5.

---

## Pattern A — systemd + nginx on a single VM

Suitable for a 4 vCPU / 8 GB cloud VM running a single project.

### 1. Provision

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

Drop your project into `/srv/omnicode/workspaces/project-a` (typically
via `git clone`).

### 2. Configure

Copy the sample TOML and edit it:

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

For multi-user RBAC, leave `api_key` blank and bootstrap an admin via
`POST /admin/users` after the server is up.

### 3. Install the systemd unit

```bash
sudo cp /opt/omnicode/codebase-mcp/deploy/omnicode.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omnicode
journalctl -u omnicode -f
```

If you want MCP-over-HTTP too (only do this if remote AI editors will
connect — local AI editors should keep using stdio):

```bash
sudo cp /opt/omnicode/codebase-mcp/deploy/omnicode-mcp.service \
        /etc/systemd/system/
sudo systemctl enable --now omnicode-mcp
```

### 4. nginx + Let's Encrypt

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp /opt/omnicode/codebase-mcp/deploy/nginx.conf \
        /etc/nginx/sites-available/omnicode
sudo ln -s /etc/nginx/sites-available/omnicode \
           /etc/nginx/sites-enabled/omnicode
# edit server_name in the file BEFORE the first reload
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d omnicode.example.com
```

### 5. Verify

```bash
curl https://omnicode.example.com/health
curl -H "X-API-Key: REPLACE_ME" \
     https://omnicode.example.com/capabilities
```

Both should be 200. The first `curl` works without the header because
`/health` is on the public allow-list.

---

## Pattern B — Docker Compose with Caddy

Suitable when your VM already runs other Docker workloads or you want
TLS without the certbot dance.

### 1. Provision

```bash
git clone <your-fork> /opt/omnicode
cd /opt/omnicode
cp omnicode.example.toml omnicode.toml      # edit this
echo "OMNICODE_API_KEY=$(openssl rand -hex 32)" > .env
```

Update `deploy/Caddyfile` to point at your real hostname.

### 2. Bring it up

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/docker-compose.cloud.yml \
  up -d
```

The base `docker-compose.yml` already builds the OmniCode image; the
overlay forces cloud-mode env vars, drops the loopback port mapping,
and adds the Caddy proxy. Caddy auto-provisions a TLS cert on first
request.

### 3. Verify

```bash
curl https://your-host/health
curl -H "X-API-Key: $(grep OMNICODE_API_KEY .env | cut -d= -f2)" \
     https://your-host/capabilities
```

---

## Hardening checklist

- [ ] OmniCode bound to `127.0.0.1`, never `0.0.0.0`, when behind a proxy.
- [ ] TLS termination at the proxy with HSTS + modern cipher suites.
- [ ] `OMNICODE_MODE=cloud` (so read-only and apply-blocked are the
      default; flip them deliberately when needed).
- [ ] `OMNICODE_ALLOW_SHELL=false` (reserved for future use of the
      `execute_tool`; opt in only if you've audited callers).
- [ ] `/admin/*` blocked at the proxy except from a trusted CIDR.
- [ ] MCP-over-HTTP behind `--auth required` so the server refuses to
      start if neither `OMNICODE_API_KEY` nor an RBAC user exists.
- [ ] Workspace path on a dedicated mount; nginx/Caddy `client_max_body_size`
      tuned so large diffs aren't truncated.
- [ ] Log rotation for `/var/log/omnicode` (logrotate or journal-only).
- [ ] Backups of `~/.kiro/codebase-mcp/` (provider DB, users DB,
      workspaces.json) — they live outside the project tree on
      purpose.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` on `/`  | systemd unit failed | `journalctl -u omnicode -n 50` |
| `401` on every call after enabling auth | client missing header | `X-API-Key: …` or `Authorization: Bearer …` |
| `403 read-only` on legitimate writes | cloud preset still active | set `OMNICODE_ALLOW_APPLY_PATCH=true` *and* `OMNICODE_READ_ONLY=false` |
| MCP-over-HTTP refuses to start | `--auth required` and no key/user | set `OMNICODE_API_KEY` or bootstrap a user |
| Slow startup on every restart | cold sentence-transformers download | pre-download in the Docker image (already done by Dockerfile) |
| `/admin/users` keeps 401-ing the bootstrap call | a user already exists | RBAC is now active — provide a token |

---

## Why cloud mode defaults to read-only

The W2-3 pattern wants to keep "writes from the wire" off by default
because:

1. **Blast radius.** A wrong patch applied via a remote endpoint can
   silently corrupt the source tree. Local editors apply patches
   through their own confirm-prompt UX; the cloud server has none.
2. **Audit trail.** `/patch/preview` + `/patch/validate` + `/patch/explain`
   are still open under read-only mode, so an editor can show the
   diff and the analysis to the user before doing the actual write
   on the user's local machine.
3. **Hybrid story.** This matches the long-term hybrid mode (Wave 2,
   W2-2): cloud for "understanding", local for "applying".

If you really need remote write — say a sandboxed CI agent — flip
`OMNICODE_ALLOW_APPLY_PATCH=true` and require RBAC role `editor` or
`admin` for the calling token. Don't open it for the legacy single
key.
