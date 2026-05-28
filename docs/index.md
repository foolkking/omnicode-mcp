# Documentation Index

> One-page map of every doc in this project. If you're not sure
> where to look, start here.

---

## By role

### "I want to use OmniCode-MCP"

1. [`README.md`](../README.md) — what it is + quickstart.
2. [`docs/running.md`](running.md) — every way to start the server.
3. [`docs/configuration.md`](configuration.md) — env vars + TOML keys.
4. [`docs/llm-extras.md`](llm-extras.md) — opt-in LLM features.

### "I'm calling OmniCode-MCP from another tool"

1. [`docs/api-reference.md`](api-reference.md) — full REST + MCP
   catalog.
2. [`docs/features.md`](features.md) §Capabilities — eight things
   you can ask for.
3. The live `/docs` Swagger UI at `http://<host>:6789/docs`.

### "I'm deploying to a server"

1. [`docs/cloud-deployment.md`](cloud-deployment.md) — systemd /
   docker compose, hardening checklist.
2. [`docs/security.md`](security.md) — auth tiers, sandbox,
   master-key rotation.
3. [`docs/configuration.md`](configuration.md) §"Worked examples"
   for cloud / hybrid presets.

### "I'm contributing code"

1. [`CONTRIBUTING.md`](../CONTRIBUTING.md) — full developer on-ramp.
2. [`docs/test_plan.md`](test_plan.md) — manual + automated test
   matrix.
3. [`docs/architecture-v2.md`](architecture-v2.md) — architecture
   rationale.

### "I want to know what's coming next"

1. [`docs/roadmap.md`](roadmap.md) — post-1.0 research directions.
2. [`docs/wave2-plan.md`](wave2-plan.md) — Wave 2 implementation log.

---

## All documents

| Document | Audience | Purpose | Length |
|---|---|---|---|
| [`README.md`](../README.md) | Everyone | One-page intro + quickstart | medium |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Contributors | Dev setup + conventions + PR rules | long |
| [`docs/index.md`](index.md) | Everyone | This doc — map of all the docs | short |
| [`docs/architecture-v2.md`](architecture-v2.md) | Contributors | Long-form §1–§17 design plan | very long |
| [`docs/features.md`](features.md) | Everyone | Feature inventory — endpoints, CLI, modules | long |
| [`docs/api-reference.md`](api-reference.md) | API consumers | Full REST + MCP catalog | very long |
| [`docs/configuration.md`](configuration.md) | Operators | Every env var + TOML key, precedence rules | long |
| [`docs/security.md`](security.md) | Operators | Auth tiers, sandbox, key rotation, anti-patterns | long |
| [`docs/cloud-deployment.md`](cloud-deployment.md) | Operators | systemd + nginx, docker + Caddy patterns | long |
| [`docs/llm-extras.md`](llm-extras.md) | Operators | Opt-in `[llm]` extras: router, providers, AI edit | medium |
| [`docs/running.md`](running.md) | Users | Local-run cookbook | medium |
| [`docs/test_plan.md`](test_plan.md) | Contributors | Manual + automated regression matrix | long |
| [`docs/wave2-plan.md`](wave2-plan.md) | Contributors | Wave 2 backlog (10 items, all shipped) | medium |
| [`docs/final-audit.md`](final-audit.md) | Contributors | §1–§17 audit against the original prompt | medium |
| [`docs/roadmap.md`](roadmap.md) | Everyone | Post-1.0 research; permanent non-goals | medium |
| [`extensions/vscode/README.md`](../extensions/vscode/README.md) | VS Code users | Thin extension (3 commands) | short |
| [`_keep_/README.md`](../_keep_/README.md) | Contributors | How to share artefacts past `.gitignore` | short |
| [`omnicode.example.toml`](../omnicode.example.toml) | Operators | Sample TOML config with every key annotated | medium |

---

## Reading order for new contributors

If you're new to the project and have ~30 minutes, read in this
order:

1. `README.md` (5 min) — what / why.
2. `docs/architecture-v2.md` §1, §3, §17 (5 min) — project identity.
3. `docs/features.md` §3 + §4 (5 min) — eight capabilities, REST
   surface.
4. `CONTRIBUTING.md` §"Architecture rules" (5 min) — what reviewers
   reject.
5. `docs/api-reference.md` skim (5 min) — concrete shapes.
6. `docs/configuration.md` skim (5 min) — operator's view.

After that you should be able to navigate the codebase without
getting lost.

---

## Doc status policy

- **Generated against a specific commit hash.** When the head moves,
  these docs may drift. The audit doc (`final-audit.md`) gets
  regenerated on every release; everything else is updated as the
  feature lands.
- **Examples are tested.** `omnicode.example.toml` is parsed by the
  TOML loader's test suite. CLI examples in this doc set are
  smoke-tested manually before each release.
- **Out-of-date doc → fix-or-flag.** If you find a doc that doesn't
  match the code, either fix it in the same PR (preferred) or open
  an issue with the diff. Stale docs are worse than missing docs.

---

## Conventions used across docs

- **Code blocks** use language hints (`bash` / `python` / `jsonc`)
  so syntax highlighters render properly.
- **Paths** are written with forward slashes even on Windows
  examples — paste-friendly across shells.
- **Status emoji**:
  - ✅ shipped
  - ⏳ planned / in progress
  - ❌ explicitly out of scope
  - ⚠️ shipped with caveats (look for the note next to it)
- **Link style**: relative links between docs in this repo, full
  URLs for external references.
- **Don't reproduce more than 30 consecutive words** from any
  external source. Paraphrase + link instead.
