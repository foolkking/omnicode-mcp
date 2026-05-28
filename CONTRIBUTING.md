# Contributing to OmniCode-MCP

Thanks for considering a contribution. This guide is the developer
on-ramp; **users** should start at [`README.md`](README.md) and
[`docs/usage.md`](docs/usage.md).

---

## Table of contents

- [Project vision](#project-vision)
- [Development setup](#development-setup)
- [Architecture rules](#architecture-rules)
- [Coding conventions](#coding-conventions)
- [Tests, lint, and CI](#tests-lint-and-ci)
- [Regression matrix](#regression-matrix)
- [Submitting a pull request](#submitting-a-pull-request)
- [Common patterns](#common-patterns)
- [Where to ask questions](#where-to-ask-questions)

---

## Project vision

OmniCode-MCP is a **Codebase Intelligence Layer**, not an AI editor.
We provide primitives — search, impact analysis, safe patch
operations, memory advisory — that any AI editor (Cursor, Claude
Code, Continue, Aider, Kiro) calls into. We don't write our own
chat sidebar.

If a feature proposal sounds like "OmniCode should also do X"
where X is something Cursor / Claude Code already does, the answer
is usually no. The right shape is "OmniCode should expose Y so
Cursor can do X better".

When in doubt, re-read [`docs/architecture.md`](docs/architecture.md)
— it pins the project's identity.

---

## Development setup

### Required

- Python 3.11 (3.12 also works; CI runs both).
- Git.

### Recommended

- Conda — we use `omnicode-env` as the canonical name throughout
  scripts and docs.
- A GitHub fork of <https://github.com/foolkking/omnicode-mcp>.

### One-liner

```bash
git clone https://github.com/<your-fork>/omnicode-mcp.git
cd omnicode-mcp
conda create -n omnicode-env python=3.11 -y
conda activate omnicode-env
pip install -e ".[dev,llm,agent]"     # dev tools + every optional extra
```

Verify:

```bash
omnicode doctor                       # Python, deps, LSP servers, ports
python -m pytest tests -q             # ~30 s
```

### Editable install + offline embeddings

By default the embedding model is downloaded once. If you're
network-restricted, prime the cache offline:

```bash
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
  python -c "from sentence_transformers import SentenceTransformer; \
             SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

The Docker image already does this in the build step.

---

## Architecture rules

These are non-negotiable. Reviewers will reject PRs that violate
them:

1. **`omnicode_core/` does not depend on Web UI or specific LLM
   providers.** Adapters call core; never the reverse. New core
   modules should be importable in a stripped install with no
   FastAPI / no `litellm` / no templates.
2. **MCP / HTTP / Web Console are adapters.** They share the same
   underlying service objects in `core/dependencies.py`; new
   features go in core first, then get exposed via whichever
   adapter(s) make sense.
3. **LLM features are optional.** Anything new in `omnicode/llm/`
   or `omnicode/pipelines/edit.py` must guard imports so a
   no-extras install doesn't crash.
4. **Path inputs go through the sandbox.** Never call `Path.open()`
   on a caller-supplied string without `validate_file_path` or
   `ensure_within_workspace` first. See
   [`docs/deployment.md`](docs/deployment.md).
5. **State writes go through the read-only middleware**. Don't add
   per-endpoint manual `OMNICODE_READ_ONLY` checks unless you need
   a non-default behaviour (e.g. `/patch/apply` has its own gate).
6. **Per-workspace data lives in shards**. New persistent files
   for a feature go under `<wd>/.data/shards/<id>/`, not the
   legacy `<wd>/.data/`. The sharding layer auto-migrates legacy
   layouts on first run.
7. **Imports stay one-way**:

   ```text
   adapters → core → standard library / third-party
   ```

   Tests can import anything; production code can't import from
   tests.

---

## Coding conventions

### Python

- **Style**: ruff (configured in `pyproject.toml`). Run
  `ruff check omnicode omnicode_core omnicode_adapters api core tests`
  before pushing.
- **Type hints**: required on all public functions. Use
  `Optional[X]` rather than `X | None` when the file is shared
  with Python < 3.10 callers; mostly we're on 3.11+ so either
  works.
- **Docstrings**: triple-double-quoted, full sentences, explain
  *why* not *what*. The reader can read the code for what.
- **Imports**: `isort` order is enforced by ruff (`I001`). Group
  stdlib → third-party → first-party with blank lines.
- **Async**: prefer `async def` for I/O paths (HTTP handlers,
  LSP, file reads). Use `httpx.AsyncClient` not `requests`.

### Frontend (Web Console)

- **Vanilla JS modules + Tailwind**. No React / Vue / Svelte
  build step. The whole front-end is hand-served from
  `templates/`.
- **Naming**: kebab-case for HTML files and CSS classes,
  camelCase for JavaScript identifiers, snake_case for
  data-attributes when they map directly to a Python field name.
- **No new runtime dependencies** without a discussion. We don't
  bundle, so every script tag is fetched separately; size matters.

### Commit messages

We follow the conventional-commits *spirit* but not the strict
syntax. Prefixes we use:

- `feat:` — new user-visible behaviour
- `fix:` — bug fix
- `chore:` — internal cleanup, gitignore changes, doc renames
- `docs:` — documentation-only changes
- `refactor:` — code shape changes with no behaviour change
- `test:` — test-only changes
- `perf:` — measurable performance improvement

Multi-line messages are encouraged for non-trivial changes.
Keep the first line under 72 characters; wrap the body at 78.

---

## Tests, lint, and CI

### Running tests

```bash
# Full suite (~30 s)
python -m pytest tests -q

# Just the regressions ring (~12 s) — UI-bug-driven
python -m pytest tests/integration/test_route_regressions.py -q

# A single file
python -m pytest tests/unit/test_user_store.py -q

# With coverage
python -m pytest tests --cov=omnicode_core --cov-report=term-missing
```

### Adding a test

Pick the right directory:

- `tests/unit/` — pure-Python, no FastAPI, no network. Fast.
- `tests/integration/` — uses `fastapi.testclient.TestClient`,
  may stand up the full app. Slower but lets you exercise
  middleware + routing.

Conventions:

- One file per module (`test_<module>.py`).
- Use `tmp_path` and `monkeypatch` from pytest, not global state.
- For middleware-related tests that mutate env vars, **clear
  `get_settings.cache_clear()` in fixture teardown** — the
  Pydantic Settings object is `@lru_cache`d and leaks across
  tests otherwise.
- Skip tests that need optional binaries with `pytest.importorskip`
  or `pytest.skip(reason=...)`. Don't make CI red because someone
  doesn't have `jdtls` installed.

### Linting

```bash
# Read-only check across the CI scope
ruff check omnicode omnicode_core omnicode_adapters api core tests

# Auto-fix is allowed everywhere EXCEPT tests/
ruff check omnicode omnicode_core omnicode_adapters api core --fix
```

> **Don't run `ruff --fix tests/`.** Historical reason: a previous
> automation run accidentally deleted the test directory tree. Fix
> test issues by hand. Yes, it's annoying. The risk is worse.

### CI

`.github/workflows/ci.yml` runs:

1. ruff lint on `omnicode + omnicode_core + omnicode_adapters +
   api + core + tests`
2. pytest matrix on Python 3.11 and 3.12
3. Docker image build smoke (push to `main` only)

Branch protection: PRs cannot merge until lint + tests pass.

---

## Regression matrix

> Absorbed from the previous standalone `docs/test_plan.md`. Use
> this as a manual smoke-test list when shipping anything that
> touches user-visible UI or core endpoints. Status legend:
> ✅ pass · ⚠️ partial / known issue · ❌ fail · 🔄 manual verify.

### Quick command reference

```cmd
:: Unit tests
conda run --no-capture-output -n omnicode-env python -m pytest tests/unit -v

:: Integration tests
conda run --no-capture-output -n omnicode-env python -m pytest tests/integration -v

:: Full suite, quiet
conda run --no-capture-output -n omnicode-env python -m pytest tests -q

:: Lint
conda run --no-capture-output -n omnicode-env ruff check omnicode omnicode_core omnicode_adapters api core tests

:: Performance benchmarks
conda run --no-capture-output -n omnicode-env python benchmarks/run_all.py

:: TestClient smoke (avoids spinning up uvicorn)
conda run --no-capture-output -n omnicode-env python -c "from fastapi.testclient import TestClient; from main import app; c=TestClient(app); print(c.get('/healthz').status_code)"
```

### Automated suites

Coverage as of the 433-test baseline:

| File | Cases | What it covers |
|---|---|---|
| `tests/unit/test_ast_parser.py` | ~16 | 7 languages — symbol / import / call extraction |
| `tests/unit/test_call_graph.py` | ~10 | CallGraph add / remove / update_file |
| `tests/unit/test_inheritance.py` | ~8 | 5 languages — extends / implements / impl trait |
| `tests/unit/test_token_manager.py` | ~23 | CommentStripper, FunctionFolder, ContextPruner, CostGuard |
| `tests/unit/test_llm_router.py` | ~21 | FakeProvider, 4 strategies, fallback, circuit breaker, best-of-N |
| `tests/unit/test_provider_registry.py` | ~17 | SQLite CRUD, built-in detection, placeholder filter |
| `tests/unit/test_secret_box.py` | ~10 | Fernet round-trip, idempotency, placeholder migration |
| `tests/unit/test_master_key_rotation.py` | 7 | Master-key rotation idempotency + rollback |
| `tests/unit/test_session_idempotent.py` | ~9 | Idempotent session start, branch detection, trunk resolve |
| `tests/unit/test_guard.py` | ~14 | Ruff JSON, JS guard fallback, cppcheck XML |
| `tests/unit/test_issue_linker.py` | ~12 | 6 reference patterns, closing verbs, GitHub token |
| `tests/unit/test_edit_safety.py` | ~32 | Three-layer defence vs. LLM prose / patch / surgical |
| `tests/unit/test_sandbox.py` | — | Prefix-collision, symlink-out, Windows paths |
| `tests/unit/test_lsp_fleet.py` | 19 | All 10 LSP servers (skips when binary absent) |
| `tests/unit/test_reranker.py` | 10 | Cross-encoder enable / disable / failure-fallback |
| `tests/unit/test_sharding.py` | 11 | Shard create / migrate / drop / list / idempotency |
| `tests/unit/test_token_expiry.py` | 8 | `expires_at`, auto-revoke, revoke-by-user |
| `tests/integration/test_api.py` | — | Provider CRUD, selections, model status, fs, git |
| `tests/integration/test_edit_pipeline.py` | — | Happy path, guard upgrade, large-file compress |
| `tests/integration/test_issue_linker_github.py` | 4 | In-process GitHub mock |
| `tests/integration/test_route_regressions.py` | — | `/symbols/graph`, `/read` null, 422 → 200 |

### Manual UI smoke tests

These live outside CI; run them when you change anything in
`templates/`. Status column shows the last known good state.

| Area | Test | Status |
|---|---|---|
| Provider CRUD UI | Add / edit / disable / enable / delete in **Model Providers** panel; "Test" button shows Pong on success and a one-line readable error on failure. | 🔄 |
| Best-of-N routing | `RoutingStrategy.QUALITY_FIRST` with `best_of_n=3` returns the longest non-empty response across N healthy providers. | ✅ |
| Call graph viewer | **Code Graph Viewer** → mode "Call graph" → Reload. Force-directed layout doesn't pile up; max-nodes input clamps; theme switch redraws. | 🔄 |
| Inheritance graph | Same panel, mode "Inheritance". Subclass → base direction. | ✅ |
| Symbols counter | Dashboard "Symbols" card non-zero after `POST /search/index`. | ✅ |
| View Code modal | Search result → "View Code" → full-screen modal with copy button + ESC dismiss. | ✅ |
| Edit pipeline (small file) | < 60 lines + `#` instruction → whole-file mode → quality_score >= 0.8. | ✅ |
| Edit pipeline (large + symbol) | `mcp_server.py` + "为 main 函数" → surgical mode triggers, only 20-line slice rewritten. | ✅ |
| Edit pipeline (large + sketch) | Same file + sketched edit → patch mode (SEARCH/REPLACE) triggers. | ✅ |
| Quality gate | Write syntactically broken Python → 200 with `result.success=false` and `failure_reasons` populated. | ✅ |
| Session idempotency | `POST /session start` twice with same name → second returns `reused=true`, no `branch already exists` error. | ✅ |
| Branch detection | After `start session 你好`, Git panel shows "🟢 Active Session: 你好". | ✅ |
| Git blame / history | `POST /git blame` returns `{commit, author, date, line, content}` per line. `history` returns risk_score 0-1. | ✅ |
| Memory dedupe | Repeated `store_memory` with same `(category, normalised content)` increments `access_count` instead of inserting a new row. | ✅ |
| Memory edit modal | Pencil icon opens full modal (not browser `prompt`) with content / category / importance / tags / related_files. | ✅ |
| Memory advisory in edit | Stored SOLUTION memory shows up as priority=18 context in next matching edit. | ✅ |
| i18n | EN ↔ 中文 toggle re-renders all panels live, including dynamically injected DOM. | ✅ |
| Live log stream | `/ws/logs` connects, backfills up to 500 lines, pushes new entries < 1 s. | ✅ |
| File browser | 📁 button opens cross-drive picker; deny-list (`C:\Windows\System32\config`, `/etc/shadow`) rejected. | ✅ |
| Dark mode | Header toggle cycles Light → Dark → System; reload preserves; OS theme change in System mode flips immediately. | ⚠️ requires hard-refresh after theme.js update |
| MCP tool visibility | Restart Claude Desktop after config change; type `/` and verify omnicode tools listed. Default 8 (`OMNICODE_MCP_TOOLS=core`); `all` = 24; `legacy` = 16. | ✅ |
| MCP tool execution | Ask Claude "list all symbols in main.py" → calls `ast_query_tool`, returns structured JSON. | ✅ |

### Performance baselines

Targets validated against the current ~125-source-file workspace:

| Benchmark | Target | Last measured |
|---|---|---|
| Call graph cold build | < 1.5 s | 702 ms |
| Call graph `update_file` median | < 50 ms | 10 ms |
| Inheritance cold build | < 1 s | 503 ms |
| Inheritance `update_file` | < 20 ms | 2 ms |
| Token compress 5 KB | < 10 ms | 2 – 2.5 ms |
| Incremental rebuild (no file changes) | — | < 1 s |

### Known issues to keep on the radar

- Patch mode LLMs occasionally hallucinate SEARCH anchors for
  symbols that resolve ambiguously. Surgical mode preempts when
  AST uniqueness check passes; otherwise we fall back and the
  guard catches most drift.
- Thinking-mode models (e.g. `gemini-2.5-flash`) sometimes spill
  inner monologue into the diff fence. Prose detection + prompt
  constraint mitigate but don't eliminate.
- Old vector_store DBs without `chunk_type` need a one-shot
  `POST /search/index` rebuild before the Symbols counter shows
  non-zero.
- `match_field` granularity in memory search is coarse — the
  backend reports `Semantic similarity` / `Filter match`; UI
  falls back to that string until per-field highlighting lands.

---

## Submitting a pull request

1. **Branch**: `git checkout -b feat/<short-name>` off the
   current head.
2. **Commit small**: prefer 5 small commits over 1 megacommit. We
   squash on merge anyway, but reviewers thank you.
3. **Update docs**:
   - User-visible API changes → `docs/api.md`
   - New env var or TOML key → `docs/usage.md`
   - Security implications → `docs/deployment.md`
   - Architecture changes → `docs/architecture.md`
4. **Add a regression test** for any UI-visible fix. We've been
   bitten by the same bugs twice when there isn't one.
5. **Run `omnicode doctor` + the test suite**. CI will catch you
   eventually but it's faster to catch it locally.
6. **Open the PR** with:
   - A summary of *why*, not just *what*.
   - A line referring to the docs you updated.
   - Screenshots / GIFs for UI changes.

Push to `feat/<name>` and open a PR against `main`. Maintainers
push directly to `main` for trivial fixes; non-maintainer
contributions go through review.

### What gets merged fast

- Bug fixes with a regression test.
- Doc improvements.
- Test additions.
- Adapter-layer additions (new REST endpoint that wraps an
  existing core feature).

### What gets pushback

- New core modules without a clear adapter consumer.
- Anything that adds a runtime dependency.
- "I added a chat UI to the Web Console". (See
  [project vision](#project-vision).)
- PRs that touch >1000 lines without a design doc.

---

## Common patterns

### Adding a new REST endpoint

1. Decide whether the underlying functionality is core or adapter:
   - If it does code understanding / search / impact / patch /
     memory → core. Add a method on the relevant
     `omnicode_core/...` class.
   - If it's only a Web Console concern (e.g. dashboard
     aggregator) → adapter only.
2. Create the endpoint in `api/v1/routers/<topic>.py`. Sandbox
   any path inputs.
3. Register the router in `api/v1/routers/__init__.py`'s
   `all_routers` list.
4. Test in `tests/integration/test_<topic>_endpoints.py` using
   `TestClient`.
5. Document in `docs/api.md`.

### Adding a new MCP tool

For pure routing wrappers (most tools should be), add to
`omnicode_adapters/mcp_server/high_level_tools.py`:

```python
@mcp.tool()
async def omni_my_thing(
    arg: str,
    optional: int = 5,
) -> str:
    """Short, action-oriented description.

    Detailed paragraph explaining when to use this vs a sibling
    tool. Mention the underlying REST endpoint(s).
    """
    try:
        res = await make_request(
            "POST", "/my-endpoint", json={"arg": arg, "optional": optional}
        )
        if not res.get("success"):
            return f"❌ failed: {res.get('error')}"
        # Render the relevant slice of the result; don't dump everything.
        return json.dumps(res.get("result", {}), indent=2)
    except Exception as exc:
        return f"❌ omni_my_thing failed: {exc}"
```

Update `discover_tools`'s catalog dict so it shows up in the
runtime discovery.

### Adding a new env var

1. Add the field to `omnicode/config/settings.py` with a
   sensible default and a comment explaining what it does.
2. Add the row to [`docs/usage.md`](docs/usage.md)
   under both the TOML section AND the env-var alphabetical
   reference.
3. If it should be configurable via TOML too, add the
   `(section, key) → env_name` mapping in
   `omnicode_core/config/toml_loader.py::_SECTION_KEY_MAP` and
   update `omnicode.example.toml`.

### Adding a new LSP server

One entry in `omnicode_core/lsp/bridge.py::LSP_SERVERS`:

```python
"my-lang": {
    "command": ["my-lang-server", "--stdio"],
    "install_hint": "<package manager> install my-lang-server",
    "extensions": [".myext"],
}
```

Update `omnicode doctor` (`omnicode_adapters/cli/commands/doctor_cmd.py::lsp_servers`).
Add a row to `docs/architecture.md` §LSP. Tests in
`tests/unit/test_lsp_fleet.py` will pick it up automatically (they
iterate over `LSP_SERVERS`).

### Adding a new Web Console page

1. Create `templates/components/sections/<name>.html`. Self-
   contained: HTML + inline `<script>` IIFE.
2. Register in `templates/components/layout/sidebar.html` with a
   `loadSection('<name>')` button.
3. If the page calls a new endpoint, add a route helper to
   `templates/static/js/api/routes.js` first.
4. Use `data-feature="<flag>"` for any panel that should be
   off-by-default; `templates/static/js/utils/features.js`
   handles the toggle.

### Bumping the version

`pyproject.toml` → `version`. We track loosely SemVer; the
current target is `1.0.0`. Pre-release tags like `1.0.0-rc1` are
fine while we shake out post-Wave-2 issues.

---

## Where to ask questions

- **Bugs**: open an issue on GitHub with steps to reproduce + the
  output of `omnicode doctor`.
- **Architecture proposals**: open a *Discussion* (not an issue)
  with the rationale + how it fits the
  [project vision](#project-vision).
- **Security concerns**: send a private email to the maintainers
  listed in `pyproject.toml`. Do NOT open a public issue with
  exploit details.

Thanks for being here.
