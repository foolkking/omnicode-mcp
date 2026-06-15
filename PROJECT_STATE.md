# Project State

Last updated: 2026-06-15

## 1. Project Snapshot

| Field | Current State |
|---|---|
| Project type | MCP code intelligence backend, CLI tool, FastAPI backend, local/cloud hybrid sync system |
| Primary language | Python |
| Runtime / version | Python >= 3.11 |
| Package manager | `pip install -e .`; optional extras in `pyproject.toml` |
| Main entry points | `omnicode` CLI, `omnicode serve`, `omnicode mcp`, FastAPI routers under `api/v1/routers/`, MCP high-level tools under `omnicode_adapters/mcp_server/high_level_tools.py` |
| Test command | `python -m pytest tests -q` |
| Lint command | `ruff check omnicode omnicode_core omnicode_adapters api core tests` |
| Docs status | Existing docs updated in place; `docs/index.md` is the navigation entry point |

## 2. Current Purpose

- Provide an AI editor MCP backend that can read, search, diagnose, contextualize, and safely patch a codebase.
- Keep local file reads and writes authoritative while allowing cloud/hybrid backends to provide indexing, search, context, and impact analysis from synchronized snapshots.
- Prioritize deterministic, non-misleading behavior: exact read/search/patch must work before semantic or graph features are treated as ready.

## 3. Current Repository Map

```text
.
|-- api/v1/routers/              FastAPI HTTP routers
|-- core/                        FastAPI lifecycle, auth, middleware, dependencies
|-- omnicode/                    legacy AST/search/LLM/vector modules still in use
|-- omnicode_core/               refactored core services
|   |-- capabilities/            capability registry for status/discover/tool policy
|   |-- edit/                    safe patch preview/validate/apply/rollback
|   |-- embeddings/              embedding backend and model-cache contract
|   |-- search/                  planner and text fallback providers
|   `-- workspace/               exact SQLite snapshot/workspace index
|-- omnicode_adapters/           CLI, MCP server, local sync agent
|-- scripts/                     benchmark and utility scripts
|-- tests/                       unit, integration, and large-repo benchmark tests
`-- docs/                        durable human and AI-facing documentation
```

## 4. Important Files

| Path | Why it matters |
|---|---|
| `omnicode_adapters/mcp_server/high_level_tools.py` | High-level MCP tool surface, handler version, tool routing, status/discover contract |
| `omnicode_core/capabilities/registry.py` | Shared capability status used by `omni_status`, `discover_tools`, and preflight policy |
| `omnicode_core/workspace/exact_index.py` | SQLite exact index for files, symbols, lines, FTS metadata, freshness revision |
| `omnicode_core/search/planner.py` | Query intent and provider-chain planning for exact, text, semantic, references, hybrid search |
| `omnicode_core/search/text_grep.py` | ripgrep/Python text fallback when FTS is unavailable or empty |
| `omnicode_core/embeddings/models.py` | Supported embedding models, cache-dir, local-files-only status, model pull helpers |
| `omnicode_core/embeddings/backend.py` | Local/remote/hybrid embedding backend behavior |
| `omnicode/search/vector_store.py` | FAISS vector metadata and semantic index compatibility checks |
| `omnicode_core/edit/patch.py` | Safe-edit session, rollback, new-file unlink, preview conflict guard |
| `api/v1/routers/sync.py` | Cloud sync batch, strict content hash, revision and workspace validation |
| `api/v1/routers/search.py` | HTTP search/index endpoints and local/cloud exact index routing |
| `tests/benchmarks/test_django_large_repo_hybrid.py` | Django large-repo hybrid benchmark gate |
| `tests/benchmarks/test_kafka_large_repo_hybrid.py` | Kafka large-repo hybrid benchmark gate |

## 5. Known Working Commands

| Command | Purpose | Status |
|---|---|---|
| `python -m pytest tests -q -m "not large_repo" --basetemp=<workspace-temp> -p no:cacheprovider` | Full non-large suite | Verified after embedding cache-completeness fix: 1155 passed, 16 skipped, 17 deselected |
| `python -m pytest tests/integration -q` | Integration route regressions | Verified in r60 sweep: 37 passed |
| `python -m pytest tests/unit/test_sharding.py -q --basetemp=<workspace-temp> -p no:cacheprovider` | Sharding default/state-dir behavior | Verified after test-isolation fix: 14 passed |
| `python -m pytest tests/integration/test_route_regressions.py::test_symbol_search_finds_chunker_metadata_match -q --basetemp=<workspace-temp> -p no:cacheprovider` | `/search/index` route regression with workspace-local state dir | Verified after test-isolation fix: 1 passed |
| `ruff check api/v1/routers/search.py omnicode_adapters/mcp_server/high_level_tools.py --select F,E9` | Targeted syntax/unused-import check | Verified in r60 sweep |
| `python scripts/benchmark_large_repo_hybrid.py --repo C:/omnicode-sim/benchmark-repos/django --state-dir .tmp_benchmarks/state-django-r60-final --cloud-workspace .tmp_benchmarks/cloud-django-r60-final --workspace-id django-r60-final --port 6870 --reset-state --symbol BaseHandler --expected-file django/core/handlers/base.py --text-query "class BaseHandler:" --text-file-pattern "*.py" --min-files 6000 --json` | Django clean-room hybrid benchmark | Verified after final test-isolation fix: 6994 files, 45754 symbols, exact symbol 20ms, exact text 265ms, context 195ms |
| `python scripts/benchmark_large_repo_hybrid.py --repo C:/omnicode-sim/benchmark-repos/kafka --state-dir .tmp_benchmarks/state-kafka-r60-final --cloud-workspace .tmp_benchmarks/cloud-kafka-r60-final --workspace-id kafka-r60-final --port 6871 --reset-state --symbol ReplicaManager --expected-file core/src/main/scala/kafka/server/ReplicaManager.scala --text-query "class ReplicaManager" --text-file-pattern "*.scala" --min-files 7000 --json` | Kafka clean-room hybrid benchmark | Verified after final test-isolation fix: 7272 files, 16501 symbols, exact symbol 24ms, exact text 504ms, context 409ms |
| `python scripts/soak_hybrid_durability.py --root .tmp_soak/hybrid-r60-short --duration-s 30 --max-iterations 6 --sleep-s 0 --rollback-every 2 --cloud-down-at 2 --reset-state --json` | Short hybrid durability soak | Verified: 6 edit/sync/search cycles, 3 rollback cycles, 1 cloud-down pending flush, final pending=0 and exact indexed revision caught up |
| `python scripts/soak_hybrid_durability.py --root .tmp_soak/hybrid-r60-duration-check --duration-s 60 --max-iterations 0 --sleep-s 1 --rollback-every 3 --cloud-down-at 2 --reset-state --json` | Duration-bound hybrid durability smoke | Verified after soak semantics fix: `ended_by=duration`, target 60s, elapsed 66.656s, 39 edit/sync/search cycles, 13 rollback cycles, pending=0, accepted/exact indexed revision=54 |
| `python scripts/soak_hybrid_durability.py --root .tmp_soak/hybrid-r60-readonly-fix --duration-s 120 --max-iterations 0 --sleep-s 1 --rollback-every 3 --cloud-down-at 2 --reset-state --json` | Readonly mirror repeated-update soak | Verified after Windows readonly mirror replacement fix: 83 edit/sync/search cycles, accepted/exact indexed revision=112, pending=0 |
| `python scripts/soak_hybrid_durability.py --root .tmp_soak/hybrid-r60-long --duration-s 1800 --max-iterations 0 --sleep-s 1 --rollback-every 5 --cloud-down-at 3 --reset-state --json` | 30-minute hybrid durability soak | Verified after readonly mirror fix: `ok=true`, 0 failed steps, 1316 edit/sync/search cycles, 263 rollback cycles, 1 cloud-down pending flush, `ended_by=duration`, elapsed 1806.062s, pending=0, accepted/exact indexed revision=1581 |
| `python -m pytest tests/unit/test_models_cli.py tests/unit/test_embedding_model_contract.py tests/unit/test_vector_metadata.py tests/unit/test_memory_manager_embedding_fallback.py -q` | Embedding/model CLI/vector metadata unit gate | Verified in sandbox with workspace-local temp dirs; embedding/model CLI subset passed after adding incomplete-cache detection |
| `python -m pytest tests/unit/test_capability_preflight_injection.py tests/unit/test_discover_dynamic_capabilities.py tests/unit/test_deterministic_fallback_contract.py tests/unit/test_search_planner.py tests/unit/test_text_grep_provider.py tests/unit/test_state_dir_paths.py -q` | Capability/discover/deterministic fallback/planner/text provider/state-dir gate | Verified in sandbox with workspace-local temp dirs: 31 passed |
| `python -m pytest tests/unit/test_omni_read_contract.py tests/unit/test_omni_search_source_confidence.py tests/unit/test_intelligence_composer.py -q` | Local-first read/search confidence/context composer gate | Verified in sandbox with workspace-local temp dirs: 65 passed |
| `python -m pytest tests/unit/test_exact_index.py tests/unit/test_sync_router.py tests/unit/test_snapshot_read_search_routes.py tests/unit/test_patch_manager_conflict.py tests/unit/test_omni_status.py tests/unit/test_hybrid_analysis_freshness.py -q` | Safety/sync/freshness/exact-index regression gate | Verified after latest continuation: 94 passed |
| `python -m pytest tests/unit/test_omni_status.py tests/unit/test_handler_version_stamps.py tests/unit/test_omni_index_tool.py tests/unit/test_mcp_cloud_bridge.py -q` | MCP registration/status/index/cloud-bridge contract gate | Verified after latest continuation: 36 passed |
| `ruff check api core memory_system omnicode omnicode_adapters omnicode_core scripts tests --select F,E9` | Full-tree syntax/undefined/unused smoke | Verified after cleaning legacy test unused imports/variables |
| `omnicode models list --json` | Show supported embedding models | Verified: four supported models and local/cloud defaults returned |
| `omnicode models status --model <model> --cache-dir <dir> --json` | Inspect embedding cache and local-files-only state | Verified for all four supported models with empty cache; returns structured `EMBEDDING_MODEL_NOT_FOUND` |
| `omnicode models pull --model <model> --cache-dir <dir>` | Pre-download embedding model into a fixed cache | Real pull/load verified for all four supported models in fixed cache `E:\omnicode-model-cache-r60` |
| `omnicode mcp --transport stdio ...` | MCP stdio tool surface and safe-edit smoke | Verified in r60 smoke: 14 tools listed, `omni_status` r60, patch apply/rollback new-file unlink passed; hybrid cloud-down stdio smoke verified local preview/validate/apply/read/rollback and post-rollback `File not found` |

## 6. Current Architecture Summary

- MCP, CLI, and HTTP adapters call shared core services where possible.
- `omni_read` and `omni_patch` are local-authority operations in hybrid mode.
- Cloud/hybrid search, context, and impact use synchronized snapshots and freshness/barrier checks before claiming results are current.
- SQLite exact index is the deterministic baseline for files, symbols, lines, and optional FTS line search.
- ripgrep/Python grep are deterministic fallbacks for exact text search.
- FAISS semantic search is optional and must pass embedding/vector metadata compatibility before use.
- Capability Registry is the source of truth for status, discoverability, and degraded/unavailable tool behavior.

## 7. Current Implementation Status

| Area | Status | Notes |
|---|---|---|
| Path guard and path redaction | Done | Must not regress; all file-bound tools should reject workspace escape and avoid local absolute path leaks |
| Safe patch and rollback | Done | Includes preview, validate, apply, rollback, new-file unlink, existing-file restore, repeated rollback handling |
| Preview conflict guard | Done | Apply rejects when file changed after preview |
| Hybrid local-authority patch/read | Done | Local files remain authoritative for read and write |
| Sync batch / hash no-op / strict hash | Done | Sync accepts workspace-relative paths and verified content hashes |
| Freshness / barrier / stale prevention | Done | Cloud analysis must not pretend stale results are fresh |
| Cloud snapshot store / readonly mirror | Done | Cloud stores content-addressed objects and mirror, not local absolute paths; repeated updates over Windows readonly mirror files are covered |
| SQLite exact index | Done | Files, lines, symbols, FTS status, revision metadata |
| Text fallback | Done | FTS, ripgrep, Python fallback provider chain |
| Local index bootstrap | Done | `omni_index(scope="workspace")` builds deterministic local exact index |
| Query planner | Done | Search responses expose plan/provider/fallback metadata |
| Embedding model cache contract | Done | Status supports model/cache/local-files-only; pull/status CLI exists |
| Vector metadata | Done | Semantic index records embedding model/dimension/revision/chunker metadata |
| Capability Registry | Done | Status/discover/tool policy use capability states |
| Impact/context non-misleading degradation | Done | Missing graph/semantic should produce degraded/partial results, not false precision |
| Language capability matrix | Done | Scala diagnostics/validate should be unsupported or not_performed, not fake passed |
| Dynamic discover_tools | Done | Recommends default tools based on current capability states |
| Codex app MCP mount | Partial | Stdio MCP smoke passed, including hybrid cloud-down local-authority safe-edit; current Codex tool discovery did not expose `mcp__omnicode`, so target-client mounting still needs environment-level verification |
| Four embedding model pull/load | Done | Real pull/load/status verified for all-MiniLM, BGE small, e5 small, and mpnet base in `E:\omnicode-model-cache-r60`; cache size about 762 MB |
| Large-repo live revalidation after final fixture fix | Done | Django and Kafka clean-room gates passed with exact-first fallback and no semantic stale false-success |
| Short hybrid durability soak | Done | `scripts/soak_hybrid_durability.py` passed short, duration-bound 60s, and readonly-mirror 120s runs with edit/sync/search, rollback, cloud-down pending preservation, restart, and pending drain |
| Long-running soak | Done | 30-minute duration-bound run passed after readonly mirror replacement fix: 1316 edit/sync/search cycles, 263 rollback cycles, 1 cloud-down pending flush, pending=0 |

## 8. Known Issues / Risks

| Issue | Impact | Current fix / next step |
|---|---|---|
| Large dirty worktree | Blocks merge confidence | Review diff, ensure no debug leftovers, stage intentionally |
| `docs/` and README historically stale | Can mislead future AI/client setup | Keep `docs/index.md` and this file current; avoid raw conversation logs |
| Codex-mounted MCP not visible in this session | Cannot claim target-client integration from SDK smoke alone | Restart target MCP client and run live `mcp__omnicode` smoke |
| Embedding models may not be cached | Semantic search unavailable with `local_files_only=true` | Fixed cache `E:\omnicode-model-cache-r60` currently contains all four supported models; status rejects incomplete/partial model caches and returns structured missing-model errors |
| Semantic/graph are optional | AI may over-trust them if descriptions drift | Keep capability registry and docs explicit: exact/read/patch are default baseline |
| Windows temp dirs with restricted ACLs may remain | Cosmetic cleanup issue | Clean with owner/admin permissions only if needed |

## 9. Documentation Map

| Doc | Purpose |
|---|---|
| `README.md` | Human overview and quick start; may still contain older marketing posture |
| `README_zh.md` | Chinese overview; may lag current r60 details |
| `PROJECT_STATE.md` | Current AI-readable repo snapshot |
| `docs/index.md` | Documentation navigation |
| `docs/architecture.md` | Architecture and capability-aware contract |
| `docs/usage.md` | Install, CLI, MCP, index, model-cache usage |
| `docs/deployment.md` | Local/cloud/hybrid deployment and security posture |
| `docs/api.md` | HTTP API reference |
| `docs/roadmap.md` | Historical roadmap and future work |

## 10. Next Best Tasks for an AI Agent

1. Verify actual target MCP client mounting, not only SDK stdio smoke.
2. Review and stage the large r60 diff in coherent groups.
3. Keep documentation synchronized with any final gate results.

## 11. Do Not Assume

- Do not assume semantic or graph search is ready just because exact search works.
- Do not assume cloud search is fresh unless revision/freshness fields prove it.
- Do not assume Scala diagnostics or validation are equivalent to Python.
- Do not write internal state into the real repository root when `OMNICODE_STATE_DIR` is configured.
- Do not remove tests for path guard, rollback, sync, freshness, workspace isolation, or exact index while refactoring.
