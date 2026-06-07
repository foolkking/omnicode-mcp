# Session Memory Summary

This file summarizes the restored Codex sessions for this project so future work can resume without rereading the full exports.

## Restored Sessions

- Included project sessions: 3.
- Source index: `restored_sessions/INDEX.md`.
- Excluded candidate: one session whose `cwd` was `E:\1project\repopilot`; it only mentioned this project in text.

## Project Direction

The project is OmniCode/codebase MCP work. The historical thread moved from audit-bundle MCP contract fixes into hybrid local/cloud production hardening, then into large-repo readiness.

The guiding rule established in the sessions is: do not trust source inspection or unit tests alone for release sign-off. Important claims need live MCP or live HTTP backend verification, with explicit pre-checks for handler version, module sha, warnings, backend health, workspace root, and diagnostics.

## Release Timeline And Decisions

- r18: Fixed and live-verified `next_actions` completeness for MCP tools. Early output without live MCP calls was rejected as invalid. Final r18 live state loaded correctly and passed its target.
- r19: Closed the new-file preview P0 for `omni_patch`, but a fuller final review later found remaining P0s: rollback of new files left a 0-byte file, and `omni_read` could return stale content after rollback.
- r20: Narrow fix for the two P0s: rollback new files should unlink, and read cache should invalidate. Safe-edit core passed, then a confidence sweep found P1 issues in diagnostics and unsafe path-denial `next_actions`.
- Post-r20/r21: Fixed `high_level_tools.py` lint/diagnostics and path-denial `next_actions`. Targeted r14-r20/r21 tests and live MCP checks passed. A previous false negative was caused by backend not started or started with the wrong cwd; this was recorded historically through `omni_memory` as memory id 1.
- r27: Hybrid production hardening. Added safe-edit conflict guard, strict sync hash validation, workspace auto-register, search workspace guard, and HTTP 409 on patch conflicts. Clean-room full sync passed.
- r30/r31 onward: Shifted focus to large-repo hybrid readiness. Main problems were no longer safe-edit/data safety, but sync/index/search observability and large-repo bootstrap.
- r35: Fixed embedded agent initial sync handoff so local manifest/local revision becomes visible to MCP after Django initial sync. This removed `freshness unknown` for large-repo MCP search/context.
- r36: Improved `omni_status` readiness for cloud snapshot state. Live status showed Django cloud snapshot source, 6991 indexed files, and text/symbol readiness.

## Current Functional State From History

- Safe-edit pipeline is considered production-ready after r27:
  - preview/apply/rollback lifecycle works.
  - new-file rollback deletes the file.
  - read cache invalidates after rollback.
  - path escape attempts are rejected with safe `next_actions`.
  - preview conflict guard rejects apply after external modification.
- Local/cloud hybrid for small and medium repos is production-usable.
- Django large-repo hybrid reached candidate/default-usable status:
  - Django snapshot state used `accepted_revision=329`, `indexed_revision=329`, about 6991 files/chunks, pending 0.
  - `pytest tests/benchmarks -m large_repo -q` reached 11 passed.
  - MCP exact search, natural-language semantic search, context, and impact bootstrap were live-verified.
  - `omni_impact` still honestly degrades to low-confidence/unknown graph depth in some cases; this was treated as non-blocking if the fallback is explicit.

## Important Remaining Work

The latest major unresolved area is Java large-repo symbol bootstrap, discovered with Apache Kafka.

Historical Kafka findings:

- Infrastructure passed: sync, object store, mirror, background index, freshness, health/status.
- Kafka cloud revision caught up around `accepted_revision=154`, `indexed_revision=154`, pending 0.
- Tool-layer P1 issues remained:
  - MCP `omni_search(mode="symbol")` defaults through `fuzzy=true` and can miss/underrank exact symbol hits. `KafkaProducer` was ranked behind test/noise even though backend exact search could find `KafkaProducer.java`.
  - `omni_read(mode="symbol")` does not properly crop Java generic class symbols; it returned the whole `KafkaProducer.java` file with `symbol=null`.
  - `omni_impact("KafkaProducer")` did not use exact symbol fallback and returned `not_found` despite exact backend evidence.
- Recommended next round name: `r37-java-symbol-bootstrap`.
- Recommended r37 scope:
  - Merge exact symbol hits into fuzzy/default MCP symbol search.
  - Add Java class/generic class symbol range read.
  - Add exact symbol fallback for impact.
  - Keep this separate from broader semantic ranking or graph-depth improvements.

## Backlog And Cautions

- Full-suite test pollution existed historically: targeted audit suites passed, but wide unit suite had cross-test monkeypatch/isolation failures. Treat as test-infra backlog, not product-contract failure, unless it reproduces in targeted/live paths.
- Codex-mounted stdio MCP can become stale after code edits. `omni_status` may show old handler version or `module_mtime_after_process_start`. Re-mount/restart MCP before live sign-off.
- Do not kill stdio MCP host casually inside Codex; prefer restarting HTTP backends and note when stdio needs user-side remount.
- Backend cwd matters. Wrong cwd can cause false negatives such as file-not-found diagnostics or guard failures.
- Temporary backend ports used historically:
  - 6789: local real repo backend.
  - 6791: cloud/repo-a backend.
  - 6792: clean-room backend for isolated sync tests.
  - 6819: Django large-repo temporary backend.
  - 6821: Kafka Java large-repo temporary backend, left running at the end of the Kafka evaluation thread to support r37 follow-up.
- Always verify current process state before assuming those ports are still running.

## Useful Verification Pattern

Before sign-off:

1. `omni_status`: check `ok=true`, expected `handler_version`, module sha/mtime, `warnings=[]`, workspace root, sync/index readiness.
2. `omni_diagnostics(high_level_tools.py)`: expect 0 errors / 0 warnings for release claims.
3. Run targeted unit/benchmark tests relevant to the touched surface.
4. Use live MCP calls or live HTTP backend calls for the actual user workflow.
5. Clean all `tests/tmp_*` and temporary state/backend processes unless deliberately keeping a benchmark backend for the next round.

## Files And Areas Often Touched

- `omnicode_adapters/mcp_server/high_level_tools.py`: MCP handler version/features, tool contract behavior, status/diagnostics/path next-actions.
- `omnicode_core/edit/patch.py`: safe-edit lifecycle, preview/apply/rollback, conflict guard.
- `api/v1/routers/sync.py`: sync batch, hash validation, workspace registration/isolation.
- Search/snapshot/index worker modules: large-repo search readiness, semantic/exact boost, status responsiveness.
- `tests/benchmarks` and large-repo markers: Django/Kafka repeatable evaluation.

## Short Resume Prompt

If continuing from the latest historical state, start with:

`Resume r37-java-symbol-bootstrap. Verify current git status and live backend/MCP versions first. Focus only on Java large-repo symbol bootstrap: exact symbol merge for fuzzy/default search, Java class/generic symbol range read, and impact exact fallback. Use Kafka as live benchmark if available, and do not change safe-edit logic unless a regression is directly observed.`
