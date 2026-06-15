"""
High-level MCP tools — the eight-tool core surface.

These eight tools are designed so an external AI editor can map the
"what do I want to do" question to a single tool with one ``mode`` /
``action`` parameter — no need to navigate 25 fine-grained tools and
spend 10k tokens on schema descriptions.

Token savings: ~10k schema tokens → ~3.5k. Tool descriptions kept short.

Tool roster:

  omni_search       — semantic / symbol / text / hybrid / references
  omni_read         — outline / symbols / full / range / imports / diagnostics
  omni_impact       — callers / callees / risk / related tests
  omni_diagnostics  — lint / type / security checks for a file or workspace
  omni_context      — composer: outline + impact + memory + git in one call
  omni_memory       — store / search / advisory
  omni_patch        — preview / validate / apply / rollback (safe edit)
  discover_tools    — discovery + capability listing

Backwards compatibility: ``omni_analyze``, ``omni_edit``,
``omni_intelligence`` are kept as deprecated aliases that delegate to
the new tools, so older MCP configs don't break.
"""

import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple, Union

from omnicode_core.search.planner import build_search_plan, detect_search_mode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler version stamp.
#
# Bumped whenever we ship a contract-affecting change so live runtime can
# be matched against source + unit-test results during audits. The audit
# in late-May 2026 found a stale FastMCP binding (omni_search reloaded
# but omni_read didn't); the omni_status tool reads this constant so the
# next round can verify which build is actually serving traffic.
# ---------------------------------------------------------------------------
_HANDLER_VERSION = "2026.06.15.sync-revision-r61"
_HANDLER_FEATURES: Tuple[str, ...] = (
    "sync.missing_upsert_delete",
    "freshness.pending_aware_cloud_revision",
    "exact_index.fts_auto_status",
    "search.text_provider_chain",
    "search.exact_text_fast_path",
    "search.ripgrep_provider",
    "search.scala_default_text_patterns",
    "search.shared_query_planner",
    "index.workspace_exact_bootstrap",
    "embedding.model_cache_contract",
    "embedding.vector_metadata",
    "embedding.semantic_metadata_query_guard",
    "embedding.semantic_metadata_status",
    "embedding.models_cli_pull_status",
    "capability.execution_policy",
    "capability.cloud_reachability_honesty",
    "capability.semantic_block_policy",
    "search.references_capability_contract",
    "search.semantic_local_provider_guard",
    "status.capability_registry",
    "tools.capability_preflight",
    "read.local_first_cloud_down",
    "read.local_first_outline",
    "patch.language_validate_matrix",
    "diagnostics.language_matrix",
    "discover.dynamic_capability_strategy",
    "search.index_not_ready_contract",
    "impact.deterministic_symbol_fallback",
    "context.deterministic_degraded_sections",
    "context.file_symbol_fast_path",
    "search.source_confidence",       # omni_search row stamping
    "read.diagnostics_aligned",       # omni_read[diagnostics] uses _collect_diagnostics_payload
    "read.language_fallback",         # _guess_language_from_path
    "read.next_actions_per_mode",     # _next_actions_for_mode
    "diagnostics.shared_envelope",    # _collect_diagnostics_payload
    "status.runtime_self_check",      # omni_status (this tool)
    "impact.boundary_contracts",      # omni_impact: empty-symbol guard,
                                      # risk=unknown on missing symbols,
                                      # max_files clamp + response truncation
    "context.composer_v2",            # omni_context: symbol-mode composer,
                                      # symbol_resolution, next_actions,
                                      # budget_utilization, lexical boost
    "memory.id_passthrough",          # omni_memory: store/search/advisory
                                      # surface real backend ids; advisory
                                      # synthesises action_items + risks;
                                      # dedup transparency via timestamp.
    "context.memory_v2_aligned",      # omni_context now drives its memory
                                      # section through _collect_advisory_payload
                                      # (the same helper omni_memory v2 uses)
                                      # so memory_id + memory_count match
                                      # end-to-end. Schema unchanged from
                                      # context.v2 — values are now correct.
    "patch.workspace_path_guard",     # omni_patch: reject ../, absolute,
                                      # and symlink-escape paths in
                                      # preview / validate / apply / rollback.
    "patch.apply_validate_gate",      # omni_patch: apply runs validate
                                      # internally and refuses on syntax /
                                      # type errors unless force=True.
    "patch.structured_validation",    # omni_patch: validate returns
                                      # diagnostics-shaped checks[] +
                                      # counts + tools_run/tools_skipped.
    "skill.recipes_2_1",              # omni_skill: bundled recipes upgraded
                                      # to 2.1.0 — safe-refactor adds memory
                                      # advisory step + patch.v2 path-guard /
                                      # validate-gate / force=True notes;
                                      # impact-review adds references step;
                                      # test-coverage adds no-test fallback;
                                      # every recipe carries success_criteria
                                      # + next_actions + per-step
                                      # failure_policy. contract_version
                                      # stays skill.v2 — manifest-only update.
    "skill.workflow_contract_alignment",
                                      # Each recipe lists the handler features
                                      # it was authored against
                                      # (recipe_for_handler_features) so a
                                      # future drift checker can flag stale
                                      # recipes when the bundle moves on.
    "search.illegal_mode_envelope",   # omni_search: illegal mode under
                                      # format='json' now returns a stamped
                                      # ok=false envelope with valid_modes +
                                      # next_actions instead of a plain-text
                                      # fallback. contract_version unchanged.
    "alias.compat_envelope",          # omni_analyze / omni_edit /
                                      # omni_intelligence JSON responses carry
                                      # deprecated=true + replacement +
                                      # use_instead + alias.compat.v1; omni_edit
                                      # now reuses the patch.v2 workspace path
                                      # guard at the alias layer so it can't be
                                      # a bypass around the new safety edge.
    "patch.new_file_markers",         # omni_patch preview / validate / apply
                                      # responses carry file_exists +
                                      # new_file booleans so an AI editor can
                                      # tell creation from modification. Path
                                      # guard still runs first; the markers
                                      # never let traversal/absolute through.
    "read.error_next_actions",        # omni_read / omni_diagnostics
                                      # file-not-found errors now carry
                                      # next_actions guiding the agent to a
                                      # recovery path (search/context/path
                                      # check). Schema-additive only.
    "read.valid_modes_envelope",      # omni_read illegal mode under
                                      # format='json' returns a stamped
                                      # ok=false envelope with valid_modes +
                                      # next_actions, mirroring omni_search.
    "workspace.root_alignment",       # _get_workspace_root() now sources the
                                      # workspace root from the same registry
                                      # / settings the backend uses (active
                                      # workspace -> Settings.WORKING_DIR ->
                                      # cwd fallback) so client-side path
                                      # validation matches backend file IO.
    "patch.workspace_root_aligned",   # omni_patch + omni_edit alias path
                                      # guard, file_exists / new_file markers,
                                      # and unsafe_legacy_session annotation
                                      # all use the aligned workspace root.
    "patch.backend_file_markers",     # audit-bundle.r12: file_exists /
                                      # new_file markers no longer use a
                                      # local Path.stat() (which can lie when
                                      # the MCP host CWD differs from the
                                      # backend's WORKING_DIR). Instead we
                                      # probe via /read so the markers come
                                      # from the same root the backend
                                      # actually writes to. file_marker_source
                                      # documents which path produced the
                                      # answer; file_marker_authoritative is
                                      # false when the probe couldn't be
                                      # performed.
    "workspace.backend_root_visibility",
                                      # audit-bundle.r12: omni_status now
                                      # exposes backend_workspace_root +
                                      # workspace_root_matches_backend so an
                                      # auditor can spot a process-cwd
                                      # mismatch without re-running the path
                                      # probe themselves. Best-effort: null
                                      # when the backend doesn't expose a
                                      # canonical root, with a
                                      # workspace_root_warning explaining why.
    "status.hybrid_sync_aggregate",   # workspace-bridge: omni_status reports
                                      # local sync revisions, snapshot-store
                                      # state, and HybridToolRouter decisions
                                      # for local-authority / cloud-barrier
                                      # tools.
    "status.capability_contract",     # workspace-bridge: omni_status exposes
                                      # the LLM / embedding / diagnostics
                                      # capability contract derived from
                                      # RuntimeConfig-compatible env.
    "status.agent_auto_contract",     # workspace-bridge: omni_status reports
                                      # whether agent auto mode would start an
                                      # embedded watcher for hybrid sync.
    "status.backend_url_visibility",  # workspace-bridge: omni_status exposes
                                      # the configured FastAPI backend URL so
                                      # hybrid/local MCP routing can be audited
                                      # from JSON without reading process logs.
    "alias.edit_validate_gate",       # audit-bundle.r13 (P0 close):
                                      # omni_edit(action='apply') now runs
                                      # the same _do_validate gate as
                                      # omni_patch. Bad-syntax content is
                                      # blocked unless force=True with
                                      # force_reason. Closes the safety
                                      # bypass surfaced by Round 4 P0-A.
    "alias.edit_force_visibility",    # audit-bundle.r13: omni_edit gained
                                      # force / force_reason params; force
                                      # apply records validation_bypassed=
                                      # true + force_reason and prepends a
                                      # ⚠️ warning to next_actions, parity
                                      # with omni_patch's force visibility.
    "alias.edit_json_ai_edit",        # audit-bundle.r13 (P1-E close):
                                      # omni_edit(action='ai_edit',
                                      # format='json') returns a stamped
                                      # JSON envelope (was plain text).
    "alias.edit_llm_off_gate",
                                      # workspace-bridge.r24: omni_edit
                                      # action=ai_edit checks LLM disabled
                                      # state before file/backend checks.
    "alias.analyze_unknown_alignment",
                                      # audit-bundle.r13 (P1-A/B close):
                                      # omni_analyze missing-symbol returns
                                      # risk='unknown' (was 'low'); empty
                                      # symbol returns ok=false. Aligned
                                      # with impact.v2 boundary contracts.
    "alias.intelligence_resolution",  # audit-bundle.r13 (P1-C/D close):
                                      # omni_intelligence missing-symbol
                                      # surfaces symbol_resolution=
                                      # 'not_found'; empty input returns
                                      # ok=false + suggested_next_action,
                                      # mirroring context.v2.
    "edit.error_field_alignment",     # audit-bundle.r14 (P1 close):
                                      # omni_edit preview/validate/apply/
                                      # rollback now lift the backend
                                      # error message to a top-level
                                      # ``error`` field whenever ok=false,
                                      # so AI editors don't have to parse
                                      # ``message`` to find the root cause.
    "impact.next_actions",            # audit-bundle.r14 (P2 close):
                                      # omni_impact JSON envelope ships a
                                      # top-level ``next_actions`` list on
                                      # success / missing-symbol / error
                                      # branches so callers always have a
                                      # ready-to-run follow-up.
    "diagnostics.source_alignment",   # audit-bundle.r14 (P2 close):
                                      # omni_diagnostics now also exposes
                                      # the requested sources under the
                                      # canonical singular ``source`` key
                                      # in addition to ``sources`` (kept
                                      # for back-compat). Adds top-level
                                      # ``next_actions``.
    "patch.sessions_truncation",      # audit-bundle.r14 (P2 close):
                                      # omni_patch sessions response now
                                      # carries ``truncated`` +
                                      # ``total_count`` + ``limit`` so
                                      # long-running workspaces stop
                                      # silently dropping rows past the
                                      # default page size.
    "alias.analyze_source_alignment", # audit-bundle.r14 (P2 close):
                                      # omni_analyze callers/callees/
                                      # graph branches stamp ``source`` +
                                      # ``confidence`` parity with the
                                      # impact branch and with omni_impact.
    "alias.intelligence_confidence_normalised",
                                      # audit-bundle.r14 (P2 close):
                                      # omni_intelligence numeric memory
                                      # confidence is converted to the
                                      # canonical {high, medium, low}
                                      # band at the top level, with the
                                      # raw float preserved as
                                      # ``memory.confidence_score``.
    "patch.rollback_error_alignment", # audit-bundle.r15 (P1 close):
                                      # omni_patch rollback failures
                                      # (e.g. "Session not found") now
                                      # lift the backend ``message`` to
                                      # the canonical top-level ``error``
                                      # field; ``message`` preserved for
                                      # back-compat. Mirrors the r14
                                      # omni_edit fix.
    "context.file_existence_guard",   # audit-bundle.r15 (P2 close):
                                      # omni_context returns ok=false +
                                      # top-level error + file_status=
                                      # 'not_found' when caller passed
                                      # file= but the file cannot be
                                      # resolved. Drops the unrelated
                                      # memory advisory rows so the
                                      # response no longer presents
                                      # phantom lessons under a failed
                                      # call.
    "impact.symbol_resolution_field", # audit-bundle.r15 (P3 close):
                                      # omni_impact JSON envelope adds
                                      # the canonical ``symbol_resolution``
                                      # field ('found' / 'not_found' /
                                      # 'n/a') for cross-tool parity
                                      # with omni_intelligence and
                                      # omni_context.
    "impact.confidence_caveats",      # audit-bundle.r16 (P3-A close):
                                      # omni_impact downgrades
                                      # ``confidence`` from ``high`` to
                                      # ``medium`` when the graph is
                                      # wide (>=50 files) or noisy with
                                      # builtin/method-style callees, and
                                      # surfaces ``confidence_caveats``
                                      # explaining why. ``high`` is now
                                      # reserved for tight, clean graphs
                                      # an AI editor can act on without
                                      # double-checking.
    "search.references_lsp_probe",    # audit-bundle.r16 (P3-B close):
                                      # omni_search(mode='references')
                                      # JSON envelope surfaces
                                      # ``lsp_attempted`` /
                                      # ``lsp_available`` /
                                      # ``lsp_returned_refs`` /
                                      # ``fallback_used`` /
                                      # ``fallback_reason`` so AI
                                      # editors don't mistake "text_grep
                                      # low confidence" for "LSP not
                                      # tried". Result rows unchanged.
    "read.source_confidence",         # audit-bundle.r16 (P3-C close):
                                      # omni_read responses now carry
                                      # ``source`` (ast / raw_file /
                                      # vector / guard+lsp / graph) and
                                      # ``confidence`` (high / medium /
                                      # low) per-mode for cross-tool
                                      # uniformity. Authoritative modes
                                      # (raw bytes, AST symbol, lint
                                      # rules) report ``high``; vector
                                      # retrieval reports ``medium`` /
                                      # ``low`` honestly.
    "search.budget_honesty",          # audit-bundle.r17 (P1 close):
                                      # omni_search JSON path now emits
                                      # ``token_estimate`` + ``truncated``
                                      # + ``token_budget`` (when set),
                                      # and trims ``results[]`` from the
                                      # lowest-relevance tail when the
                                      # estimate exceeds budget. The
                                      # references-mode mirror is kept
                                      # in sync. Pre-r17 the parameter
                                      # was silently ignored on the JSON
                                      # path.
    "context.primary_priority",       # audit-bundle.r17 (P3 close):
                                      # omni_context promotes the top
                                      # lexical hit per task term to
                                      # ``primary_symbols`` BEFORE
                                      # filling ``related_files``, when
                                      # no explicit symbol/file anchor
                                      # is provided. Prevents the
                                      # tight-budget regression where
                                      # primary_symbols=[] while
                                      # related_files burned the entire
                                      # budget on lower-value lexical
                                      # rows.
    "memory.advisory_budget",         # audit-bundle.r17 (P3 close):
                                      # omni_memory(action='advisory')
                                      # gains ``max_memories`` +
                                      # ``token_budget`` parameters and
                                      # surfaces ``token_estimate`` +
                                      # ``truncated`` + (on cap)
                                      # ``truncation_reasons``. Brings
                                      # advisory budget contract in
                                      # line with omni_search /
                                      # omni_read / omni_context.
    "search.next_actions",            # audit-bundle.r18 (P2 close):
                                      # omni_search JSON success path
                                      # now emits ``next_actions``,
                                      # branching on ``resolved_mode``
                                      # and the quality of the top hit
                                      # so AI editors get a
                                      # ready-to-run follow-up
                                      # (omni_read / omni_impact /
                                      # references / hybrid recovery).
    "memory.next_actions_interpolated",
                                      # audit-bundle.r18 (P2 close):
                                      # omni_memory advisory next_actions
                                      # now interpolate the actual
                                      # symbol/file/task into the
                                      # suggested commands instead of
                                      # leaving ``<symbol>`` placeholders
                                      # for the caller to substitute.
    "discover.next_actions_alias",    # audit-bundle.r18 (P3 close):
                                      # discover_tools mirrors the
                                      # ``pipeline`` field as
                                      # ``next_actions`` for cross-tool
                                      # field-name uniformity. Both
                                      # keys carry the same value;
                                      # ``pipeline`` is preserved for
                                      # back-compat.
    "diagnostics.error_locator",      # audit-bundle.r18 (P3 close):
                                      # omni_diagnostics next_actions
                                      # now include a targeted
                                      # ``omni_read(mode='range', ...)``
                                      # locator pointing at the first
                                      # error / warning line so AI
                                      # editors can read context
                                      # without manually computing the
                                      # range bounds.
    "patch.preview_new_file_ok",      # audit-bundle.r19 (P0 close):
                                      # omni_patch preview on a
                                      # nonexistent file now returns
                                      # ok=True with new_file=True
                                      # and a synthesized creation
                                      # diff (every content line as
                                      # an addition) instead of the
                                      # misleading ``ok=False``
                                      # /``Preview failed`` envelope.
                                      # The path guard and apply
                                      # validate gate still run; only
                                      # the success/error semantics of
                                      # the new-file preview branch
                                      # changed. Existing-file preview
                                      # failures continue to return
                                      # ok=False.
    "patch.rollback_new_file_unlink", # audit-bundle.r20 (P0-1 close):
                                      # rollback for new-file creation
                                      # sessions now restores the
                                      # pre-edit state honestly. The
                                      # backend rollback truncates the
                                      # file to 0 bytes; the host
                                      # follows up with a Path.unlink
                                      # so the file actually does not
                                      # exist after rollback. Detected
                                      # by ``original_hash`` ==
                                      # empty-bytes sha256 prefix.
                                      # Surfaces ``new_file_unlinked``
                                      # / ``new_file_unlink_warning``
                                      # on the rollback payload so the
                                      # caller can audit the cleanup.
    "patch.rollback_cache_invalidate",# audit-bundle.r20 (P0-2 close):
                                      # rollback ends with a no-op
                                      # backend file-marker probe to
                                      # refresh the ``/read`` cache;
                                      # subsequent omni_read calls then
                                      # reflect the current disk state
                                      # (in particular file-not-found
                                      # for unlinked new-file
                                      # rollbacks) instead of returning
                                      # stale post-apply content.
    "workspace.identity_visibility",  # status exposes workspace_id /
                                      # executor_mode for local-cloud
                                      # bridge audits.
    "workspace.explicit_local_root_priority",
                                      # workspace-bridge.r22: MCP-local file
                                      # authority prefers the explicit
                                      # --workspace / OMNICODE_WORKSPACE_ROOT
                                      # path over the global registry, so a
                                      # hybrid workspace_id registered to a
                                      # cloud mirror cannot redirect
                                      # omni_read / omni_patch away from the
                                      # user's real local checkout.
    "read.local_authority_full_range",
                                      # workspace-bridge.r23: omni_read
                                      # mode=full/range now honors the
                                      # local-authority route by reading from
                                      # the MCP workspace root before falling
                                      # back to the configured backend.
    "search.code_literal_auto_text",
                                      # workspace-bridge.r23: auto search
                                      # routes code-like literals such as
                                      # VALUE = "v2" to text search instead
                                      # of semantic/hybrid fuzzy search.
    "patch.hybrid_local_authority",
                                      # workspace-bridge.r24: in hybrid mode,
                                      # omni_patch preview / validate / apply
                                      # / rollback / sessions execute against
                                      # the explicit local workspace root, not
                                      # the configured cloud backend.
    "analysis.freshness_gate",
                                      # workspace-bridge.r24: hybrid cloud
                                      # analysis tools check local/cloud
                                      # revisions before returning search,
                                      # context, or impact results.
    "errors.absolute_path_redaction",
                                      # workspace-bridge.r25: public error
                                      # text redacts local/backend absolute
                                      # filesystem paths.
    "patch.hybrid_sync_flush",
                                      # workspace-bridge.r26: hybrid local
                                      # patch apply/rollback immediately
                                      # flush pending path changes to cloud
                                      # /sync/batch when configured.
    "patch.preview_conflict_guard",
                                      # workspace-bridge.r27: PatchManager
                                      # records preview baseline hashes and
                                      # rejects apply when the file changed
                                      # between preview and apply.
    "sync.workspace_auto_register",
                                      # workspace-bridge.r27: /sync endpoints
                                      # can register a new workspace_id to
                                      # the active backend root on first use.
    "sync.strict_content_hash",
                                      # workspace-bridge.r27: /sync/batch
                                      # requires sha256:<64 hex> and verifies
                                      # it against the uploaded content.
    "search.workspace_guard",
                                      # workspace-bridge.r27: search endpoints
                                      # reject workspace headers that do not
                                      # resolve to the active backend root.
    "read.local_authority_symbol",
                                      # workspace-bridge.r28: omni_read
                                      # mode=symbol honors the local-authority
                                      # route in hybrid mode, using local AST
                                      # ranges instead of cloud /read.
    "sync.initial_walk_observable",
                                      # workspace-bridge.r28: agent initial
                                      # sync defaults to no file-count cap,
                                      # supports
                                      # OMNICODE_AGENT_MAX_INITIAL_FILES, and
                                      # sends truncation metadata to
                                      # /sync/status.
    "sync.batch_async_index",
    "sync.index_worker_coalesced",
    "sync.index_worker_chunked",
    "status.index_worker_progress",
    "search.bulk_upsert_contents",
    "agent.initial_sync_small_batches",
                                      # workspace-bridge.r28: /sync/batch
                                      # accepts snapshot/object-store updates
                                      # quickly and advances indexed_revision
                                      # from a background index task so
                                      # health/status remain observable during
                                      # large sync.
    "index.bootstrap_cli",
                                      # workspace-bridge.r29: omnicode index
                                      # accepts backend/workspace options for
                                      # explicit cloud bootstrap.
    "search.snapshot_symbol_bootstrap",
                                      # workspace-bridge.r29: symbol search can
                                      # exact-match synced snapshot content
                                      # before a full vector/symbol index is
                                      # ready.
    "status.index_readiness",
    "search.batch_refresh_once",
                                      # workspace-bridge.r29: omni_status
                                      # surfaces text/symbol/graph readiness
                                      # so AI editors can decide what to trust.
    "sync.index_worker_stats_refresh",
                                      # workspace-bridge.r31: background sync
                                      # indexing refreshes cheap DB stats after
                                      # a batch instead of reinitializing the
                                      # semantic search engine.
    "search.snapshot_symbol_fast_path",
                                      # workspace-bridge.r31: exact symbol
                                      # lookup can return snapshot-store hits
                                      # before touching the semantic engine.
    "search.snapshot_store_threaded_scan",
                                      # workspace-bridge.r31: snapshot-store
                                      # text/symbol scans run off the event
                                      # loop so health/status stay responsive.
    "search.snapshot_record_direct_read",
                                      # workspace-bridge.r31: snapshot exact
                                      # symbol scans read known object records
                                      # directly instead of reloading the full
                                      # snapshot index once per file.
    "search.snapshot_text_record_direct_read",
                                      # workspace-bridge.r31: snapshot text
                                      # scans use the same direct object-read
                                      # path as symbol scans for large repos.
    "index.snapshot_bootstrap_marks_revision",
                                      # workspace-bridge.r31: explicit
                                      # /search/index over snapshot content
                                      # marks indexed_revision and updates
                                      # /sync/status after bootstrap.
    "index.snapshot_bootstrap_threaded",
                                      # workspace-bridge.r31: explicit
                                      # snapshot bootstrap runs in a worker
                                      # thread so health/status stay live.
    "index.snapshot_bootstrap_hash_skip",
                                      # workspace-bridge.r32: explicit
                                      # snapshot bootstrap skips files whose
                                      # indexed content_hash already matches
                                      # the snapshot record.
    "index.snapshot_bootstrap_revision_skip",
                                      # workspace-bridge.r32: explicit
                                      # snapshot bootstrap can trust the
                                      # persisted indexed_revision watermark
                                      # when hash metadata is not available.
    "index.snapshot_bootstrap_background",
                                      # workspace-bridge.r33: explicit
                                      # snapshot bootstrap can run as a
                                      # background job with status polling
                                      # instead of blocking large-repo CLI
                                      # calls.
    "serve.offline_transformers_default",
                                      # workspace-bridge.r34: omnicode serve
                                      # sets TRANSFORMERS_OFFLINE and
                                      # HF_HUB_OFFLINE by default so backend
                                      # startup does not block on provider
                                      # metadata probes in offline deployments.
    "index.snapshot_background_progress",
                                      # workspace-bridge.r34: background
                                      # snapshot indexing reports live
                                      # records_seen/indexed/skipped progress.
    "analysis.snapshot_freshness_layer",
                                      # workspace-bridge.r34: snapshot-backed
                                      # exact text/symbol/context routes may
                                      # proceed with snapshot_fresh while
                                      # semantic indexing catches up.
    "context.snapshot_exact_priority",
                                      # workspace-bridge.r34:
                                      # /intelligence/context seeds exact
                                      # snapshot symbol hits ahead of noisy
                                      # semantic results.
    "impact.graph_unknown_no_false_low",
                                      # workspace-bridge.r34: cloud snapshot
                                      # impact/risk marks graph unavailable
                                      # and risk unknown instead of reporting
                                      # false low risk from zero graph edges.
    "agent.initial_sync_manifest_handoff",
                                      # workspace-bridge.r35: embedded/local
                                      # agent writes LocalManifest after
                                      # successful /sync/batch acks so MCP
                                      # freshness gates can see local_revision
                                      # after large-repo initial sync.
    "search.index_metadata_hashes",
                                      # workspace-bridge.r32: sync/search
                                      # indexing stores snapshot hash,
                                      # revision, and workspace id in chunk
                                      # metadata.
    "status.cloud_snapshot_readiness",
                                      # workspace-bridge.r36: omni_status
                                      # prefers cloud /sync/status snapshot
                                      # counts over the MCP process-local
                                      # snapshot store when computing
                                      # index_readiness in hybrid mode.
    "status.semantic_index_readiness",
                                      # workspace-bridge.r37: omni_status
                                      # surfaces semantic_index_ready,
                                      # index_worker_busy,
                                      # search_degraded, and
                                      # semantic_pending_revisions from
                                      # cloud /sync/status so AI editors can
                                      # degrade gracefully while large-repo
                                      # indexing catches up.
    "search.snapshot_exact_fuzzy_fast_path",
                                      # workspace-bridge.r37: default fuzzy
                                      # symbol search returns exact
                                      # snapshot-store hits before touching
                                      # the semantic engine, so large repos
                                      # can resolve class/function names
                                      # while embedding bootstrap catches up.
    "search.exact_index_freshness_gate",
                                      # workspace-bridge.r38: hybrid
                                      # symbol/text search may proceed when
                                      # the lightweight exact index is fresh
                                      # even if semantic indexing is still
                                      # catching up.
    "sync.semantic_index_filter",
                                      # workspace-bridge.r39: cloud sync keeps
                                      # full snapshot/exact coverage while
                                      # routing only source-like files into
                                      # semantic indexing by default. Large
                                      # repos remain exact-searchable without
                                      # embedding every locale/data artifact.
    "sync.semantic_initial_exact_only_auto",
                                      # workspace-bridge.r40: initial syncs
                                      # over the configured file-count limit
                                      # default to exact-only semantic policy.
                                      # Later edits still update semantic
                                      # indexing; full initial semantic
                                      # bootstrap is an explicit opt-in.
    "search.state_dir_shard_isolation",
                                      # workspace-bridge.r41: when
                                      # OMNICODE_STATE_DIR / content/search
                                      # store is configured, FAISS/SQLite
                                      # search shards live outside the user
                                      # workspace and legacy .data migration is
                                      # skipped to avoid mutating project files.
    "freshness.semantic_coverage_exact_only",
                                      # workspace-bridge.r42: exact-only large
                                      # initial sync records semantic coverage
                                      # separately so exact search can be fresh
                                      # without claiming semantic/hybrid search
                                      # has full repo coverage.
    "search.explicit_semantic_bootstrap",
                                      # workspace-bridge.r43: /search/index on
                                      # snapshot workspaces supports explicit
                                      # semantic bootstrap and records
                                      # semantic_full coverage when complete;
                                      # exact-policy bootstrap remains
                                      # available for constrained deployments.
    "status.index_readiness_contract",
                                      # workspace-bridge.r44: omni_status and
                                      # /sync/status expose the same
                                      # exact-vs-semantic readiness contract,
                                      # including recommended_query_mode, so
                                      # AI editors know whether to trust exact
                                      # lookup, semantic enrichment, or local
                                      # fallback.
    "index.semantic_bootstrap_tool",
                                      # workspace-bridge.r46: semantic
                                      # bootstrap has first-class CLI/MCP
                                      # entry points via omnicode index
                                      # --scope semantic and omni_index.
    "agent.pending_auto_flush",
                                      # workspace-bridge.r47: failed agent
                                      # upsert/delete sync operations are
                                      # persisted to LocalManifest pending and
                                      # retried before newer changes.
    "workspace.multi_workspace_isolation_contract",
                                      # workspace-bridge.r47: exact/snapshot
                                      # search contracts prove workspace_id
                                      # queries cannot read another registered
                                      # workspace's snapshot/index rows.
    "sync.rename_move_contract",
                                      # workspace-bridge.r47: move/rename sync
                                      # is modeled as new-path upsert plus
                                      # old-path delete, with snapshot mirror
                                      # and exact index stale rows removed.
    "search.exact_index_semantic_rank",
                                      # workspace-bridge.r48: semantic search
                                      # boosts exact-index symbol hits ahead
                                      # of semantic/vector noise and explains
                                      # the rank_reason.
    "context.snapshot_same_file_promotion",
                                      # workspace-bridge.r48: snapshot exact
                                      # symbol context promotes same-file
                                      # rows ahead of unrelated semantic
                                      # results and reports context_quality.
    "memory.advisory_seed_redaction",
                                      # workspace-bridge.r48: memory advisory
                                      # redacts absolute paths from echoed
                                      # file/task seeds, why_recalled, and
                                      # synthesized advisory text.
)
_PROCESS_START_TIME = None  # set lazily on first omni_status call


# ---------------------------------------------------------------------------
# Per-tool contract version stamps.
#
# Bumped whenever a tool's JSON envelope changes shape. Every flagship
# tool injects ``handler_version`` + ``contract_version`` into its
# ``format="json"`` payload so a caller can detect a stale FastMCP
# binding even when the module-level checks in omni_status pass — the
# audit failure mode we hit was *exactly* "module helpers loaded but
# omni_read's registered handler still pointed at the old closure".
# Putting the stamp on the response forces the registered handler
# itself to carry the new version; if the stamp comes back missing or
# stale, the running handler is stale.
# ---------------------------------------------------------------------------
_CONTRACT_VERSIONS: Dict[str, str] = {
    "omni_search":      "search.source_confidence.v1",
    "omni_read":        "read.diagnostics_aligned.v1",
    "omni_impact":      "impact.v2",
    "omni_diagnostics": "diagnostics.shared_envelope.v1",
    "omni_patch":       "patch.v2",
    "omni_memory":      "memory.v2",
    "omni_context":     "context.v2",
    "omni_skill":       "skill.v2",
    "omni_index":       "index.v1",
    "omni_status":      "status.v1",
    "discover_tools":   "discover.v1",
}

# Valid omni_search modes. ``auto`` resolves to one of the non-auto members
# via _detect_mode; the rest are caller-supplied. Used by omni_search to
# reject illegal modes with a structured (stamped) envelope instead of a
# plain-text fallback. Order is the recommended-try order for next_actions.
_SEARCH_VALID_MODES: Tuple[str, ...] = (
    "auto", "semantic", "symbol", "text", "hybrid", "references",
)

# Valid omni_read modes. Used to reject illegal modes client-side with a
# stamped envelope (read.valid_modes_envelope) instead of relying on the
# backend's plain-text "Unknown read mode" string.
_READ_VALID_MODES: Tuple[str, ...] = (
    "outline", "symbols", "full", "range", "symbol",
    "imports", "diagnostics", "relevant_chunks", "tests",
)

# Deprecated aliases keep a separate, non-core contract version so they can
# carry a stable JSON envelope (deprecated/replacement/use_instead) without
# entering the core ``expected_contract_versions`` audit surface. They are
# intentionally NOT added to _CONTRACT_VERSIONS / tools_with_json_stamp.
_ALIAS_COMPAT_CONTRACT = "alias.compat.v1"
_ALIAS_REPLACEMENTS: Dict[str, str] = {
    "omni_analyze":      "omni_impact",
    "omni_edit":         "omni_patch",
    "omni_intelligence": "omni_context",
}

# Flagship tools that support JSON-mode stamping. Any tool listed in
# _CONTRACT_VERSIONS but missing here is treated as text-only — omni_status
# will emit a json_stamp_unsupported:<tool> warning so audits can refuse
# to validate the runtime against an under-instrumented tool.
_TOOLS_WITH_JSON_STAMP: Tuple[str, ...] = (
    "omni_search",
    "omni_read",
    "omni_impact",
    "omni_diagnostics",
    "omni_patch",
    "omni_memory",
    "omni_context",
    "omni_skill",
    "omni_index",
    "omni_status",
    "discover_tools",
)


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/][^\s'\"<>{}\[\]\|),;]+)"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./:-])/(?:Users|home|tmp|var|opt|mnt|etc|omnicode-sim)"
    r"(?:/[^\s'\"<>{}\[\]\|),;]+)*"
)


def _path_string_variants(path_text: str) -> List[str]:
    variants = {
        path_text,
        path_text.replace("\\", "/"),
        path_text.replace("/", "\\"),
    }
    return sorted((v for v in variants if v), key=len, reverse=True)


def _sanitize_error_text(text: str) -> str:
    """Redact absolute filesystem paths from public error text.

    Status payloads may intentionally expose roots for deployment audits. Error
    strings should not: they are often copied into AI-editor prompts and logs.
    """
    if not isinstance(text, str) or not text:
        return text

    redacted = text
    known_roots: List[Tuple[str, str]] = []

    try:
        import os as _os

        for env_name, placeholder in (
            ("OMNICODE_WORKSPACE_ROOT", "<workspace_root>"),
            ("OMNICODE_STATE_DIR", "<state_dir>"),
            ("OMNICODE_BACKEND_WORKSPACE_ROOT", "<backend_workspace_root>"),
        ):
            value = (_os.environ.get(env_name) or "").strip()
            if value:
                known_roots.append((value, placeholder))
    except Exception:
        pass

    try:
        root, _source, _warnings = _get_workspace_root()
        if root:
            known_roots.append((str(root), "<workspace_root>"))
    except Exception:
        pass

    for root_text, placeholder in sorted(
        known_roots, key=lambda pair: len(pair[0]), reverse=True
    ):
        for variant in _path_string_variants(root_text):
            redacted = redacted.replace(variant, placeholder)

    redacted = _WINDOWS_ABSOLUTE_PATH_RE.sub("<absolute-path>", redacted)
    redacted = _POSIX_ABSOLUTE_PATH_RE.sub("<absolute-path>", redacted)
    return redacted


def _sanitize_public_error_fields(payload: Dict[str, Any]) -> None:
    for key in (
        "error",
        "message",
        "file_marker_warning",
        "sync_pending_warning",
        "sync_flush_error",
        "sync_flush_warning",
        "new_file_unlink_warning",
        "warning",
        "manifest_warning",
        "status_warning",
        "reason",
        "fallback_reason",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = _sanitize_error_text(value)

    actions = payload.get("next_actions")
    if isinstance(actions, list):
        payload["next_actions"] = [
            _sanitize_error_text(action) if isinstance(action, str) else action
            for action in actions
        ]

    for row_key in ("diagnostics", "checks"):
        rows = payload.get(row_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            message = row.get("message")
            if isinstance(message, str):
                row["message"] = _sanitize_error_text(message)


def _sanitize_public_path_text(text: str) -> str:
    """Redact absolute paths while keeping workspace-relative hints useful."""
    if not isinstance(text, str) or not text:
        return text
    sanitized = _sanitize_error_text(text)
    for prefix in (
        "<workspace_root>/",
        "<workspace_root>\\",
        "<backend_workspace_root>/",
        "<backend_workspace_root>\\",
    ):
        sanitized = sanitized.replace(prefix, "")
    return sanitized.replace("\\", "/")


def _sanitize_public_path_ref(value: Any) -> str:
    """Return a public-safe file reference for memory/context payloads."""
    text = str(value or "")
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        if path.is_absolute():
            root, _source, _warnings = _get_workspace_root()
            resolved = path.resolve()
            root_resolved = root.resolve()
            if resolved == root_resolved or root_resolved in resolved.parents:
                return resolved.relative_to(root_resolved).as_posix()
    except Exception:
        pass
    return _sanitize_public_path_text(text)


def _sanitize_memory_match_fields(fields: Any) -> List[Any]:
    """Sanitize memory match snippets without changing backend shape."""
    if not isinstance(fields, list):
        return []
    sanitized: List[Any] = []
    for item in fields:
        if not isinstance(item, dict):
            sanitized.append(
                _sanitize_public_path_text(item) if isinstance(item, str) else item
            )
            continue
        row = dict(item)
        for key in ("snippet", "value", "text", "content", "path", "file"):
            value = row.get(key)
            if isinstance(value, str):
                if key in ("path", "file"):
                    row[key] = _sanitize_public_path_ref(value)
                else:
                    row[key] = _sanitize_public_path_text(value)
        sanitized.append(row)
    return sanitized


def _stamp(payload: Any, *, tool: str) -> Any:
    """Inject handler_version + contract_version on a payload dict.

    Idempotent: if the keys are already present (e.g. the caller is
    forwarding an already-stamped envelope) we don't overwrite them so
    nested tools can still tell who originated the payload.

    Non-dict payloads are returned unchanged — version stamping a list
    or a primitive doesn't make sense and would break the public
    contract for those tools that already return non-dict JSON.
    """
    if not isinstance(payload, dict):
        return payload
    if tool not in {"omni_status", "discover_tools"}:
        try:
            _attach_capability_preflight(payload, tool=tool)
        except Exception as exc:  # noqa: BLE001
            payload.setdefault("capability_preflight", {
                "ready": False,
                "error": _sanitize_error_text(f"{exc.__class__.__name__}: {exc}"),
            })
    _sanitize_public_error_fields(payload)
    payload.setdefault("handler_version", _HANDLER_VERSION)
    payload.setdefault("contract_version", _CONTRACT_VERSIONS.get(tool, ""))
    return payload


def _format_json(data: Any, max_lines: int = 80) -> str:
    """Format data as readable JSON, truncated if too long."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
    return text


# ---------------------------------------------------------------------------
# omni_search helpers
# ---------------------------------------------------------------------------

# Default globs when the caller doesn't pass file_pattern. Mirrors the
# backend default in omnicode_core.search.text_grep.
_DEFAULT_TEXT_GLOBS = (
    "*.py,*.js,*.jsx,*.ts,*.tsx,*.go,*.rs,*.java,*.cpp,*.c,*.h,"
    "*.rb,*.php,*.kt,*.kts,*.scala,*.cs,*.md,*.toml,*.yaml,*.yml,*.json"
)

_IDENT_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_QUOTED_RE = re.compile(r'^"[^"]+"$|^\'[^\']+\'$')

# Tiny words that mean nothing on their own and would pollute a symbol /
# semantic search. When the *entire* query is one of these we treat it as
# a literal text search instead.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for",
        "is", "be", "if", "do", "we", "it", "as", "by", "at", "no",
        "def", "class", "let", "var", "fn", "func", "return",
        "from", "import", "use", "pub",
    }
)

_SINGLE_TOKEN_TEXT_LITERALS = frozenset(
    {
        "before", "after", "true", "false", "none", "null", "yes", "no",
        "on", "off", "enabled", "disabled", "pass", "fail", "passed",
        "failed", "todo", "done",
    }
)


# audit-bundle.r16 (P3-A): names that show up in omni_impact callees
# without representing real cross-symbol dependencies. These are mostly
# Python builtins and ubiquitous string/sequence methods. When a graph
# reports many of these as callees, the blast radius is inflated by
# helper noise rather than by actual cross-symbol couplings, so
# omni_impact downgrades ``confidence`` from ``high`` to ``medium`` and
# emits a caveat. This is presentation-only; the raw callees list is
# unchanged.
_PYTHON_BUILTIN_CALLEE_NAMES: frozenset = frozenset({
    # builtins
    "len", "range", "enumerate", "zip", "map", "filter", "sorted",
    "reversed", "iter", "next", "all", "any", "sum", "min", "max",
    "abs", "round", "int", "float", "str", "bytes", "bool", "list",
    "tuple", "dict", "set", "frozenset", "type", "isinstance",
    "issubclass", "id", "hash", "repr", "print", "open", "input",
    "callable", "getattr", "setattr", "hasattr", "delattr",
    "globals", "locals", "vars", "dir", "object", "super",
    "format", "ord", "chr", "hex", "oct", "bin",
    # ubiquitous string methods often surfaced as "callees"
    "lower", "upper", "strip", "rstrip", "lstrip", "split", "rsplit",
    "join", "replace", "startswith", "endswith", "find", "rfind",
    "index", "count", "encode", "decode", "title",
    "capitalize", "casefold", "fullmatch", "match", "search",
    # ubiquitous container / dict / set methods
    "append", "extend", "insert", "remove", "pop", "clear", "copy",
    "keys", "values", "items", "get", "update", "setdefault", "add",
    "discard", "union", "intersection", "difference",
    # logging / debug noise
    "debug", "info", "warning", "error", "critical", "log",
})


def _strip_quotes(q: str) -> str:
    """Remove a single matched pair of surrounding quotes."""
    if len(q) >= 2 and q[0] == q[-1] and q[0] in ("\"", "'"):
        return q[1:-1]
    return q


def _detect_mode(query: str) -> str:
    """Pick a sensible default mode from the query shape.

    Heuristics, ordered by confidence:

    1. Empty / whitespace → ``semantic`` (caller will get a no-results hint).
    2. Quoted literal ``"foo bar"`` → ``text`` (caller wants the literal).
    3. Length ≤ 2 or stop-word-only token → ``text`` (no useful symbol/embed).
    4. Exact ALL_CAPS_IDENTIFIER → ``text``  (env vars, constants).
    5. Dotted / underscored identifier (no spaces, < 60 chars) → ``symbol``.
    6. Short natural-language query (≤ 3 words) → ``hybrid``.
    7. Anything else → ``semantic``.
    """
    return detect_search_mode(query)

    q = query.strip()
    if not q:
        return "semantic"

    # 2. Quoted literal — caller is asking for the string verbatim.
    if _QUOTED_RE.fullmatch(q):
        return "text"
    # Always strip outer quotes for the remaining heuristics so a user
    # who types `"login"` gets the same routing as `login`.
    q = _strip_quotes(q).strip()
    if not q:
        return "semantic"

    if re.match(
        r"^\s*(async\s+def|def|class|interface|enum|trait|object|case\s+class|"
        r"import|from|package)\b",
        q,
    ):
        return "text"

    # 3. Single-token / stop-word guard.
    if " " not in q:
        if len(q) <= 2:
            return "text"
        if q.lower() in _STOPWORDS:
            return "text"
        if q.lower() in _SINGLE_TOKEN_TEXT_LITERALS:
            return "text"
        if re.search(r"[-:/]", q):
            return "text"

    if _CONST_RE.fullmatch(q):
        return "text"

    if re.search(r"[=;{}()[\]\"']", q):
        return "text"

    if _IDENT_RE.fullmatch(q) and len(q) <= 60:
        return "symbol"

    word_count = len(q.split())
    if word_count <= 3 and len(q) <= 40:
        return "hybrid"

    return "semantic"


async def _run_semantic(
    make_request, query: str, file_pattern: Optional[str], max_results: int, rerank: bool
) -> Tuple[List[Dict[str, Any]], int]:
    """Pure semantic (FAISS) search. Optional cross-encoder rerank."""
    payload: Dict[str, Any] = {
        "query": query,
        "search_type": "semantic",
        "max_results": max(max_results, 10),
    }
    if file_pattern:
        # Forward the whole comma-separated glob list so the backend can
        # apply fnmatch over every entry. Previously this took only the
        # first glob, which silently dropped 80%+ of recall whenever the
        # caller passed ``"*.py,*.md"``.
        payload["file_pattern"] = file_pattern.strip()

    raw = await make_request("POST", "/search", json=payload)
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    results = list(data.get("results", []))

    if rerank:
        # Tag the rows so callers can see the reranker pass actually ran
        # at the MCP layer. The backend itself only invokes the cross
        # encoder when OMNICODE_RERANKER=true; the tag is informational.
        for r in results:
            why = list(r.get("why_matched", []) or [])
            # Strip the legacy ``reranker:requested`` artefact that older
            # indexed chunks may carry — we always want the canonical
            # ``reranker:on`` (or no tag at all when ``rerank=False``).
            why = [w for w in why if w != "reranker:requested"]
            if "reranker:on" not in why:
                why.append("reranker:on")
            r["why_matched"] = why
    else:
        # Strip stale tags so a caller asking for ``rerank=False`` doesn't
        # see misleading "reranker ran" markers from the backend payload.
        for r in results:
            why = list(r.get("why_matched", []) or [])
            r["why_matched"] = [
                w for w in why if w not in ("reranker:requested", "reranker:on")
            ]

    return results, data.get("total_results", len(results))


async def _run_symbol(
    make_request, query: str, file_pattern: Optional[str], max_results: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Fuzzy symbol-name search."""
    params = {"query": query, "fuzzy": True, "max_results": max_results}
    if file_pattern:
        # Backend supports a comma-separated glob list — forward whole.
        params["file_pattern"] = file_pattern.strip()

    results: List[Dict[str, Any]] = []
    total_results = 0
    try:
        raw = await make_request("POST", "/search/symbols", params=params)
        data = raw.get("result", raw) if isinstance(raw, dict) else {}
        results = list(data.get("results", []))
        total_results = int(data.get("total_results", len(results)) or len(results))
    except Exception as exc:
        logger.debug("backend symbol search failed; trying local exact index: %s", exc)

    local_hits = _lookup_local_exact_symbols(
        query=query,
        file_pattern=file_pattern,
        max_results=max_results,
        fuzzy=True,
    )
    if local_hits:
        merged: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, int]] = set()
        for row in [*local_hits, *results]:
            key = (
                str(row.get("file_path") or row.get("file") or ""),
                str(row.get("symbol_name") or row.get("name") or ""),
                int(row.get("line_start") or row.get("line_number") or 0),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= max_results:
                break
        results = merged
        total_results = max(total_results, len(results))
    _refresh_symbol_rows_from_local_outline(results)
    return results, total_results


def _exact_symbol_row_to_mcp(row: Any) -> Dict[str, Any]:
    path = str(getattr(row, "path", "") or "").replace("\\", "/")
    name = str(getattr(row, "name", "") or "")
    line_start = int(getattr(row, "line_start", 0) or 0)
    line_end = int(getattr(row, "line_end", line_start) or line_start)
    return {
        "file_path": path,
        "file": path,
        "symbol_name": name,
        "name": name,
        "kind": getattr(row, "kind", None),
        "line_number": line_start,
        "line_start": line_start,
        "line_end": line_end,
        "signature": getattr(row, "signature", "") or "",
        "score": float(getattr(row, "score", 1.0) or 1.0),
        "confidence": "high",
        "source": "local_exact_index",
        "hash": getattr(row, "hash", "") or "",
        "revision": int(getattr(row, "revision", 0) or 0),
        "why_matched": [
            str(getattr(row, "why", "") or "symbol:exact"),
            "local_exact_index",
        ],
    }


def _lookup_local_exact_symbols(
    *,
    query: str,
    file_pattern: Optional[str] = None,
    max_results: int = 5,
    fuzzy: bool = False,
) -> List[Dict[str, Any]]:
    """Resolve symbols from the local deterministic exact index.

    This is deliberately best-effort: if the local index is missing or stale,
    callers can still use backend/cloud results. When present, it prevents
    graph/semantic outages from being misreported as "symbol not found".
    """
    if not query or max_results <= 0:
        return []
    try:
        import os as _os

        from omnicode_core.workspace.exact_index import SnapshotExactIndex

        ws_root, _source, _warnings = _get_workspace_root()
        workspace_id = (
            _os.environ.get("OMNICODE_WORKSPACE_ID")
            or ws_root.name
            or "workspace"
        )
        index = SnapshotExactIndex()
        status = index.status(workspace_id=workspace_id)
        if int(status.get("symbols") or 0) <= 0:
            return []
        rows = index.search_symbols(
            workspace_id=workspace_id,
            query=query,
            file_pattern=file_pattern,
            fuzzy=fuzzy,
            max_results=max_results,
        )
        return [_exact_symbol_row_to_mcp(row) for row in rows]
    except Exception as exc:
        logger.debug("local exact symbol lookup failed: %s", exc)
        return []


def _exact_text_row_to_mcp(row: Any) -> Dict[str, Any]:
    path = str(getattr(row, "path", "") or "").replace("\\", "/")
    line_no = int(getattr(row, "line_no", 0) or 0)
    line_text = str(getattr(row, "line_text", "") or "")
    return {
        "file_path": path,
        "file": path,
        "line_number": line_no,
        "line_start": line_no,
        "line_end": line_no,
        "line_content": line_text,
        "context_before": list(getattr(row, "context_before", []) or []),
        "context_after": list(getattr(row, "context_after", []) or []),
        "match_type": "text",
        "kind": "text",
        "relevance_score": 1.0,
        "score": 1.0,
        "confidence": "high",
        "source": "local_exact_index",
        "hash": getattr(row, "hash", "") or "",
        "revision": int(getattr(row, "revision", 0) or 0),
        "why_matched": ["text:exact", "local_exact_index"],
    }


def _lookup_local_exact_text(
    *,
    query: str,
    file_pattern: Optional[str] = None,
    max_results: int = 10,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Search text from the local deterministic exact index."""
    meta: Dict[str, Any] = {}
    if not query or max_results <= 0:
        return [], meta
    try:
        import os as _os

        from omnicode_core.workspace.exact_index import SnapshotExactIndex

        ws_root, _source, _warnings = _get_workspace_root()
        workspace_id = (
            _os.environ.get("OMNICODE_WORKSPACE_ID")
            or ws_root.name
            or "workspace"
        )
        index = SnapshotExactIndex()
        status = index.status(workspace_id=workspace_id)
        meta = {
            "provider": "local_exact_index",
            "provider_chain": ["local_exact_index"],
            "exact_index_used": True,
            "line_fts_available": bool(status.get("line_fts_available")),
            "line_fts_reason": status.get("line_fts_reason"),
            "fallback_used": not bool(status.get("line_fts_available")),
            "fallback_reason": (
                "local_exact_lines_like"
                if not bool(status.get("line_fts_available"))
                else None
            ),
            "warnings": (
                [
                    "line_fts unavailable; used local exact lines LIKE fallback"
                ]
                if not bool(status.get("line_fts_available"))
                else []
            ),
        }
        if int(status.get("lines") or 0) <= 0:
            meta["empty_reason"] = "index_not_ready"
            return [], meta
        rows = index.search_text(
            workspace_id=workspace_id,
            query=query,
            file_pattern=file_pattern,
            max_results=max_results,
            context_lines=2,
        )
        if not rows:
            meta["empty_reason"] = "true_empty"
        return [_exact_text_row_to_mcp(row) for row in rows], meta
    except Exception as exc:
        logger.debug("local exact text lookup failed: %s", exc)
        return [], {"provider": "local_exact_index", "error": str(exc)}


def _local_exact_index_status_payload() -> Dict[str, Any]:
    try:
        import os as _os

        from omnicode_core.workspace.exact_index import SnapshotExactIndex

        ws_root, _source, _warnings = _get_workspace_root()
        workspace_id = (
            _os.environ.get("OMNICODE_WORKSPACE_ID")
            or ws_root.name
            or "workspace"
        )
        status = SnapshotExactIndex().status(workspace_id=workspace_id)
        return {
            "workspace_id": workspace_id,
            "ready": bool(
                int(status.get("files") or 0) > 0
                and int(status.get("symbols") or 0) > 0
            ),
            "files": int(status.get("files") or 0),
            "symbols": int(status.get("symbols") or 0),
            "lines": int(status.get("lines") or 0),
            "line_fts_available": bool(status.get("line_fts_available")),
            "line_fts_reason": status.get("line_fts_reason"),
            "schema_version": status.get("schema_version"),
            "exact_indexed_revision": status.get("exact_indexed_revision"),
        }
    except Exception as exc:
        logger.debug("local exact index status failed: %s", exc)
        return {
            "ready": False,
            "files": 0,
            "symbols": 0,
            "lines": 0,
            "error": _sanitize_error_text(str(exc)),
        }


def _local_index_required_for_search() -> bool:
    """Whether a local MCP invocation should fail fast when exact index is absent."""
    try:
        import os as _os

        executor_mode = (
            _os.environ.get("OMNICODE_EXECUTOR_MODE")
            or _os.environ.get("OMNICODE_EXECUTOR")
            or "local"
        ).strip().lower()
        if executor_mode != "local":
            return False
        return bool(
            _os.environ.get("OMNICODE_WORKSPACE_ROOT")
            or _os.environ.get("OMNICODE_WORKSPACE")
            or _os.environ.get("OMNICODE_WORKSPACE_ID")
        )
    except Exception:
        return False


def _refresh_symbol_rows_from_local_outline(results: List[Dict[str, Any]]) -> None:
    """Repair stale backend symbol line numbers from the local workspace.

    Hybrid/cloud symbol indexes can lag after a repo move or reindex gap. The
    MCP process has the user's real checkout, so it can cheaply verify exact
    symbol locations before returning rows to an AI editor. This updates only
    line/signature metadata and leaves ranking/source semantics intact.
    """
    outline_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        file_path = str(row.get("file_path") or row.get("file") or "")
        symbol_name = str(row.get("symbol_name") or row.get("name") or "")
        if not file_path or not symbol_name or _path_looks_unsafe(file_path):
            continue
        if file_path not in outline_cache:
            outline_cache[file_path] = _build_local_outline_payload(file_path)
        outline = outline_cache.get(file_path) or {}
        symbols = outline.get("symbols") or []
        fresh = next(
            (
                s for s in symbols
                if isinstance(s, dict) and s.get("name") == symbol_name
            ),
            None,
        )
        if not fresh:
            continue
        start = fresh.get("line_start")
        end = fresh.get("line_end")
        if not isinstance(start, int) or start <= 0:
            lines = fresh.get("lines") or []
            start = lines[0] if lines and isinstance(lines[0], int) else None
        if not isinstance(end, int) or end <= 0:
            lines = fresh.get("lines") or []
            end = lines[1] if len(lines) > 1 and isinstance(lines[1], int) else start
        if not isinstance(start, int) or start <= 0:
            continue
        old_start = row.get("line_start") or row.get("line_number")
        row["line_start"] = start
        if row.get("line_number") is not None:
            row["line_number"] = start
        if isinstance(end, int) and end > 0:
            row["line_end"] = end
        signature = fresh.get("signature")
        if signature:
            row["signature"] = signature
        row["line_source"] = "local_ast"
        if old_start and old_start != start:
            row["line_refresh"] = {
                "from": old_start,
                "to": start,
                "source": "local_ast",
            }
            why = list(row.get("why_matched") or [])
            if "local_ast:fresh_line" not in why:
                why.append("local_ast:fresh_line")
            row["why_matched"] = why


async def _run_text(
    make_request, query: str, file_pattern: Optional[str], max_results: int,
    flat: bool = False,
    meta_out: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Line-level grep across the workspace.

    When ``flat=True`` the backend disables adjacent-line merging so
    every matched line shows up as its own result row.
    """
    params = {
        "query": query,
        "file_pattern": file_pattern or _DEFAULT_TEXT_GLOBS,
        "max_results": max_results,
        "context_lines": 2,
        "merge_adjacent": (not flat),
    }
    raw = await make_request("POST", "/search/text", params=params)
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    if meta_out is not None and isinstance(data, dict):
        for key in (
            "provider",
            "provider_chain",
            "exact_index_used",
            "exact_line_fts_available",
            "line_fts_available",
            "line_fts_reason",
            "fallback_used",
            "fallback_reason",
            "warnings",
            "empty_reason",
        ):
            if key in data:
                meta_out[key] = data.get(key)
    results = list(data.get("results", []))
    total = int(data.get("total_results", len(results)) or len(results))
    local_hits, local_meta = _lookup_local_exact_text(
        query=query,
        file_pattern=file_pattern or _DEFAULT_TEXT_GLOBS,
        max_results=max_results,
    )
    if local_hits:
        merged: List[Dict[str, Any]] = []
        seen: set[Tuple[str, int, str]] = set()
        for row in [*local_hits, *results]:
            key = (
                str(row.get("file_path") or row.get("file") or ""),
                int(row.get("line_number") or row.get("line_start") or 0),
                str(row.get("line_content") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= max_results:
                break
        results = merged
        total = max(total, len(results))
        if meta_out is not None:
            chain = list(meta_out.get("provider_chain") or [])
            if "local_exact_index" not in chain:
                chain.insert(0, "local_exact_index")
            meta_out["provider"] = "local_exact_index"
            meta_out["provider_chain"] = chain
            meta_out["exact_index_used"] = True
            meta_out["line_fts_available"] = bool(
                local_meta.get("line_fts_available")
            )
            if local_meta.get("line_fts_reason"):
                meta_out["line_fts_reason"] = local_meta["line_fts_reason"]
            if local_meta.get("fallback_used"):
                meta_out["fallback_used"] = True
                meta_out["fallback_reason"] = local_meta.get("fallback_reason")
            warnings = list(meta_out.get("warnings") or [])
            for warning in local_meta.get("warnings") or []:
                if warning not in warnings:
                    warnings.append(warning)
            if warnings:
                meta_out["warnings"] = warnings
    return results, total


async def _run_hybrid(
    make_request, query: str, file_pattern: Optional[str], max_results: int, rerank: bool
) -> Tuple[List[Dict[str, Any]], int]:
    """Run symbol + semantic searches in parallel, fuse with RRF."""
    import asyncio

    # Over-fetch each side so RRF has material to work with.
    overfetch = max(max_results * 3, 15)
    sym_task = _run_symbol(make_request, query, file_pattern, overfetch)
    sem_task = _run_semantic(make_request, query, file_pattern, overfetch, rerank)
    sym_pair, sem_pair = await asyncio.gather(sym_task, sem_task)

    sym_results, _ = sym_pair
    sem_results, _ = sem_pair

    fused = _rrf_fuse([sym_results, sem_results], labels=["symbol", "semantic"])

    # Drop the long tail of barely-related items. With k=60 and two
    # source lists the maximum RRF score is ~0.033 and the noisy
    # singletons land near 0.0033; cutting at 0.005 keeps roughly the
    # top 40-50 items per side without truncating real matches.
    fused = [f for f in fused if float(f.get("relevance_score", 0)) >= 0.005]

    return fused[:max_results], len(fused)


# RRF fused score below this is treated as noise. Exposed at module scope
# so unit tests / callers can override.
_RRF_NOISE_FLOOR = 0.005


def _rrf_fuse(
    lists: List[List[Dict[str, Any]]], labels: List[str], k: int = 60
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple result lists.

    Each item is keyed by ``(file_path, line_start_or_line_number,
    symbol_name)`` so the same hit found by both sources merges.
    """
    scores: Dict[Tuple[str, Any, str], float] = {}
    items: Dict[Tuple[str, Any, str], Dict[str, Any]] = {}
    sources: Dict[Tuple[str, Any, str], List[str]] = {}

    for lst, label in zip(lists, labels, strict=False):
        for rank, item in enumerate(lst):
            key = (
                item.get("file_path", ""),
                item.get("line_start") or item.get("line_number") or 0,
                item.get("symbol_name", ""),
            )
            if key not in scores:
                scores[key] = 0.0
                items[key] = dict(item)
                sources[key] = []
            scores[key] += 1.0 / (k + rank + 1)
            if label not in sources[key]:
                sources[key].append(label)

    fused = []
    for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        item = items[key]
        item["relevance_score"] = score
        why = list(item.get("why_matched", []) or [])
        for label in sources[key]:
            tag = f"hybrid:{label}"
            if tag not in why:
                why.append(tag)
        item["why_matched"] = why
        fused.append(item)
    return fused


async def _run_references(
    make_request, query: str, max_results: int
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    """Find every definition + usage of a symbol with LSP-grade semantics.

    Pipeline:

    1. **LSP** — ``workspace/symbol`` + ``textDocument/references``.
       Yields the highest-confidence ``source=lsp`` rows with
       ``kind`` distinguishing ``definition`` and ``call``.
    2. **AST exact** — when LSP isn't running, we ask the AST symbol
       index for an *exact-name* match so the anchor (a single
       definition) is unambiguous.  ``source=ast_symbol``,
       ``confidence=medium``.
    3. **Word-boundary text grep** for cross-file callsites — only
       fired *after* an anchor was found in step 2, so callers see
       both the definition AND the usages on machines without LSP.
       ``source=text_grep``, ``confidence=low``, ``kind=call``.

    Behaviour change vs. the previous implementation:

    * **No more fuzzy fallback** for ``mode=references``.  A
      reference query for ``foo`` should NEVER return ``foobar``,
      ``foozz`` etc. — that produces the "same-name confusion"
      anti-pattern the audit flagged.
    * **Ambiguous definitions are tagged** so the caller can warn the
      user (think two unrelated ``set`` methods in different files).
    * **Each row carries** ``source`` + ``confidence`` + ``kind``.

    Returns:
        (results, total, meta) where ``meta`` is a structured probe
        record so the caller can be honest about what was tried::

            {
              "lsp_attempted":     bool,    # did we call /lsp/*?
              "lsp_available":     bool,    # backend reported LSP up
              "lsp_returned_refs": bool,    # did LSP yield any rows?
              "fallback_used":     "lsp" | "ast+text_grep" | "none",
              "fallback_reason":   str,     # why we fell back
            }
    """
    meta: Dict[str, Any] = {
        "lsp_attempted": False,
        "lsp_available": False,
        "lsp_returned_refs": False,
        "fallback_used": "none",
        "fallback_reason": "",
    }
    if not query or not query.strip():
        meta["fallback_reason"] = "empty query"
        return [], 0, meta

    # ----- Step 1: LSP -----------------------------------------------------
    lsp_refs: List[Dict[str, Any]] = []
    lsp_available = True
    anchor_file = ""
    anchor_line = 0
    meta["lsp_attempted"] = True
    try:
        raw = await make_request(
            "GET", "/lsp/workspace-symbols", params={"query": query}
        )
        data = raw.get("result", raw) if isinstance(raw, dict) else {}
        if isinstance(data, dict) and data.get("error"):
            lsp_available = False
            meta["fallback_reason"] = (
                f"lsp/workspace-symbols error: {data.get('error')}"
            )
            locations: List[Dict[str, Any]] = []
        else:
            locations = data.get("symbols", []) or data.get("locations", []) or []
    except Exception as exc:
        logger.debug("LSP workspace-symbols failed for %r: %s", query, exc)
        lsp_available = False
        meta["fallback_reason"] = f"lsp/workspace-symbols raised: {exc}"
        locations = []
    meta["lsp_available"] = lsp_available

    if lsp_available and locations:
        anchor = locations[0]
        anchor_file = (
            anchor.get("file_path")
            or anchor.get("file")
            or anchor.get("uri", "").replace("file://", "")
        )
        loc = anchor.get("location") or anchor
        rng = loc.get("range", {}).get("start", {}) if isinstance(loc, dict) else {}
        anchor_line = rng.get("line", 0)
        anchor_col = rng.get("character", 0)
    elif lsp_available:
        # ``workspace/symbol`` returned empty — pyright might just not
        # have indexed the file yet (it lazy-loads). Use AST symbol
        # search to anchor on a real declaration so we can still try
        # ``textDocument/references`` (which does ``didOpen`` first).
        try:
            sym_results, _ = await _run_symbol(make_request, query, None, 1)
        except Exception:
            sym_results = []
        if sym_results:
            first = sym_results[0]
            ast_anchor_file = first.get("file_path", "")
            ast_anchor_line = max(
                0, (first.get("line_start") or first.get("line_number") or 1) - 1
            )
            if ast_anchor_file:
                anchor_file = ast_anchor_file
                anchor_line = ast_anchor_line
                # AST gives us the line where ``def foo(...)`` starts,
                # but pyright wants the cursor to be ON the identifier.
                # Read the signature (when available) and locate the name
                # offset; fall back to col 0 if we can't tell.
                sig = first.get("signature") or ""
                if query in sig:
                    anchor_col = sig.find(query)
                else:
                    anchor_col = 0

        try:
            raw = await make_request(
                "POST",
                "/lsp/references",
                params={
                    "file": anchor_file,
                    "line": anchor_line,
                    "col": anchor_col,
                    "include_declaration": True,
                },
            )
            rdata = raw.get("result", raw) if isinstance(raw, dict) else {}
            if isinstance(rdata, dict) and rdata.get("error"):
                lsp_available = False
                meta["lsp_available"] = False
                meta["fallback_reason"] = (
                    f"lsp/references error: {rdata.get('error')}"
                )
            else:
                lsp_refs = rdata.get("locations") or rdata.get("references") or []
        except Exception as exc:
            logger.debug("LSP references failed for %r: %s", query, exc)
            lsp_available = False
            meta["lsp_available"] = False
            meta["fallback_reason"] = f"lsp/references raised: {exc}"

    if lsp_refs:
        meta["lsp_returned_refs"] = True
        meta["fallback_used"] = "lsp"
        results: List[Dict[str, Any]] = []
        for ref in lsp_refs[:max_results]:
            file = (
                ref.get("file_path")
                or ref.get("file")
                or ref.get("uri", "").replace("file://", "")
            )
            rng = ref.get("range", {}).get("start", {}) if isinstance(ref, dict) else {}
            line = rng.get("line", 0) + 1
            is_decl = (
                file == anchor_file and rng.get("line", -1) == anchor_line
            )
            results.append(
                {
                    "file_path": file,
                    "line_number": line,
                    "symbol_name": query,
                    "match_type": "definition" if is_decl else "call",
                    "relevance_score": 1.0,
                    "why_matched": [
                        "lsp:references",
                        "source:lsp",
                        "confidence:high",
                    ],
                    "source": "lsp",
                    "confidence": "high",
                    "kind": "definition" if is_decl else "call",
                }
            )
        return results, len(lsp_refs), meta

    # ----- Step 2: AST exact match (find definition anchor) ---------------
    # If we got here without an LSP-sourced result, record why we fell back.
    if meta["lsp_attempted"] and not meta["fallback_reason"]:
        meta["fallback_reason"] = (
            "lsp returned 0 references for the query"
            if meta["lsp_available"]
            else "lsp not available in this backend"
        )
    try:
        sym_results, _ = await _run_symbol(make_request, query, None, max_results)
    except Exception:
        sym_results = []

    # Keep ONLY exact-name matches. Fuzzy / contains / prefix matches are
    # the cause of the same-name confusion bug — a reference query for
    # ``foo`` must never surface ``foobar``.
    exact_defs = [
        r for r in sym_results
        if (r.get("symbol_name") or "") == query
    ]

    if not exact_defs:
        # No definition we can be confident about → return nothing.
        # The caller will see ``total=0`` and can fall back to a different
        # mode (semantic / text) if they actually want fuzzy matches.
        meta["fallback_used"] = "none"
        return [], 0, meta

    # ----- Step 3: text grep for callsites ---------------------------------
    grep_results: List[Dict[str, Any]] = []
    if _IDENT_RE.fullmatch(query):
        try:
            text_query = rf"\b{re.escape(query)}\b"
            grep_params = {
                "query": text_query,
                "use_regex": True,
                "case_sensitive": True,
                "max_results": max(max_results * 4, 40),
                "context_lines": 1,
                "merge_adjacent": False,
                "file_pattern": _DEFAULT_TEXT_GLOBS,
            }
            raw = await make_request("POST", "/search/text", params=grep_params)
            gdata = raw.get("result", raw) if isinstance(raw, dict) else {}
            grep_results = list(gdata.get("results", []) or [])
        except Exception as exc:
            logger.debug("Text-grep callsite scan failed for %r: %s", query, exc)
            grep_results = []

    # Build a (file, line) set for the AST defs so we don't re-emit the
    # definition line again as a "call".
    def_keys = {
        (
            (d.get("file_path") or "").replace("\\", "/"),
            d.get("line_start") or d.get("line_number") or 0,
        )
        for d in exact_defs
    }

    # Heuristic to label the LHS of a grep hit's line text.
    def _kind_for_line(line_text: str) -> str:
        s = (line_text or "").lstrip()
        if not s:
            return "call"
        if s.startswith(("def ", "async def ", "class ", "function ", "function* ")):
            return "declaration"
        if s.startswith(("import ", "from ", "export ", "export default ")):
            return "import"
        if "require(" in s or "require (" in s:
            return "import"
        return "call"

    final: List[Dict[str, Any]] = []

    # 3a) Definitions first.
    ambiguous = len(exact_defs) > 1
    for d in exact_defs:
        fp = (d.get("file_path") or "").replace("\\", "/")
        ln = d.get("line_start") or d.get("line_number") or 0
        final.append(
            {
                "file_path": fp,
                "line_number": ln,
                "line_start": d.get("line_start"),
                "line_end": d.get("line_end"),
                "symbol_name": d.get("symbol_name") or query,
                "signature": d.get("signature", ""),
                "match_type": "definition",
                "relevance_score": 1.0,
                "why_matched": [
                    "source:ast_symbol",
                    "confidence:medium",
                    "ambiguous" if ambiguous else "exact",
                ],
                "source": "ast_symbol",
                "confidence": "medium",
                "kind": "definition",
            }
        )

    # 3b) Callsites — anything in grep_results that ISN'T already a def.
    for r in grep_results:
        fp = (r.get("file_path") or "").replace("\\", "/")
        ln = r.get("line_number") or r.get("line_start") or 0
        if (fp, ln) in def_keys:
            continue
        line_text = r.get("line_content", "") or ""
        kind = _kind_for_line(line_text)
        final.append(
            {
                "file_path": fp,
                "line_number": ln,
                "line_content": line_text,
                "context_before": r.get("context_before") or [],
                "context_after": r.get("context_after") or [],
                "symbol_name": query,
                "match_type": kind,
                "relevance_score": 0.6,
                "why_matched": [
                    "source:text_grep",
                    "confidence:low",
                    f"kind:{kind}",
                ],
                "source": "text_grep",
                "confidence": "low",
                "kind": kind,
            }
        )

    return final[:max_results], len(final), {
        **meta,
        "fallback_used": "ast+text_grep",
    }


def _rerank_by_proximity(
    results: List[Dict[str, Any]], around_file: str
) -> List[Dict[str, Any]]:
    """Bump results that sit close to ``around_file`` in the file tree.

    Same-file > same-directory > parent-directory > rest.
    """
    anchor = PurePosixPath(around_file.replace("\\", "/"))

    def proximity_score(path: str) -> int:
        if not path:
            return 0
        p = PurePosixPath(path.replace("\\", "/"))
        if p == anchor:
            return 100
        if p.parent == anchor.parent:
            return 50
        # one directory above
        if str(p.parent).startswith(str(anchor.parent.parent)):
            return 20
        return 0

    enriched = []
    for r in results:
        bump = proximity_score(r.get("file_path", ""))
        if bump:
            r["relevance_score"] = float(r.get("relevance_score", 0)) + bump / 100
            why = list(r.get("why_matched", []) or [])
            why.append(f"proximity:{bump}")
            r["why_matched"] = why
        enriched.append(r)

    enriched.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return enriched


def _no_results_message(query: str, resolved_mode: str, requested_mode: str) -> str:
    """Render an actionable hint when search came back empty."""
    suggestions = []
    if resolved_mode != "text":
        suggestions.append("• `mode=text`   — line-level grep on the literal string")
    if resolved_mode != "symbol":
        suggestions.append("• `mode=symbol` — fuzzy match on function/class names")
    if resolved_mode != "semantic":
        suggestions.append("• `mode=semantic` — natural-language → code")
    if resolved_mode != "hybrid":
        suggestions.append("• `mode=hybrid` — fuse symbol + semantic via RRF")

    # Lightweight query-rewrite suggestions.
    rewrites: List[str] = []
    q = query.strip()
    parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[0-9]+", q.replace("_", " "))
    if parts and " ".join(parts).lower() != q.lower():
        rewrites.append(f"`{' '.join(parts).lower()}`  (split identifier into words)")
    if "_" in q and " " not in q:
        rewrites.append(f"`{q.replace('_', ' ')}`  (treat underscores as spaces)")
    if len(q) > 30:
        rewrites.append(f"`{' '.join(q.split()[:3])}…`  (shorter, focus on core terms)")

    if requested_mode == "auto":
        header = f"🔍 No results for '{query}' (mode=auto→{resolved_mode})"
    else:
        header = f"🔍 No results for '{query}' (mode={resolved_mode})"

    lines = [header, ""]
    if suggestions:
        lines.append("Try a different mode:")
        lines.extend(suggestions)
        lines.append("")
    if rewrites:
        lines.append("Or rewrite the query:")
        for r in rewrites:
            lines.append(f"• {r}")
        lines.append("")
    lines.append("Common misses: case sensitivity, language filter (try `file_pattern='*'`).")
    return "\n".join(lines)


def _approx_token_count(text: str) -> int:
    """Rough estimator — every 4 chars is ~1 token. Good enough for budget gates."""
    return max(1, len(text) // 4)


def _render_results(
    *,
    query: str,
    requested_mode: str,
    resolved_mode: str,
    results: List[Dict[str, Any]],
    total: int,
    token_budget: int,
) -> str:
    """Render search results as plain text.

    When ``token_budget > 0`` we render in two passes: first a verbose
    pass with snippet + context, then if the total would blow the
    budget we fall back to a compact pass that drops snippets.
    """
    header_mode = (
        f"mode={requested_mode}" if requested_mode == resolved_mode
        else f"mode={requested_mode}→{resolved_mode}"
    )
    header = f"🔍 {total} result(s) for '{query}' ({header_mode})\n"

    verbose = [header]
    for i, r in enumerate(results, 1):
        verbose.extend(_render_one_result(i, r, with_snippet=True))

    full = "\n".join(verbose)

    if token_budget <= 0 or _approx_token_count(full) <= token_budget:
        return full

    # Compact pass — drop snippets / context.
    compact = [header]
    for i, r in enumerate(results, 1):
        compact.extend(_render_one_result(i, r, with_snippet=False))
    compact.append(f"\n(token-budget {token_budget} exceeded full render; snippets dropped)")
    return "\n".join(compact)


# ---------------------------------------------------------------------------
# Per-row source / confidence stamping
#
# References mode self-reports source + confidence (see _run_references).
# The other backends (symbol / semantic / text / hybrid) used to leave both
# fields blank, which made the public JSON schema look half-broken to AI
# editors. ``_infer_source_confidence`` mirrors the references convention
# for the rest of the modes: derive source from which backend produced the
# row, derive confidence from why_matched tags + score thresholds.
# ---------------------------------------------------------------------------

# Score thresholds — tuned to keep a fresh BM25 / embedding hit at "medium"
# by default. We only escalate to "high" when there's a decisive signal
# (reranker + strong score, multi-source hybrid hit, or symbol:exact).
_SEMANTIC_HIGH = 0.70
_SEMANTIC_MED = 0.40
_HYBRID_DECISIVE = 0.020  # fused RRF score floor for "medium"
_FUZZY_MEDIUM = 0.70


def _infer_source_confidence(
    row: Dict[str, Any], mode: str, *, rerank: bool
) -> Tuple[str, str]:
    """Infer (source, confidence) for a result row.

    Used for symbol / semantic / text / hybrid. References-mode rows are
    already self-tagged by :func:`_run_references` and skipped by the
    caller. The returned strings follow the public contract:

      source ∈ {symbol_index, symbol_index_fuzzy, vector_index,
                vector_index+reranker, text_index,
                hybrid:<labels>, lsp, ast_symbol, text_grep, mixed}
      confidence ∈ {high, medium, low}
    """
    why = [str(w).lower() for w in (row.get("why_matched") or [])]
    score = float(row.get("relevance_score", 0) or 0)

    if mode == "symbol":
        if "symbol:exact" in why:
            return "symbol_index", "high"
        if "symbol:prefix" in why or "symbol:contains" in why:
            return "symbol_index", "medium"
        if any("fuzzy" in w or "rapidfuzz" in w for w in why):
            return (
                "symbol_index_fuzzy",
                "medium" if score >= _FUZZY_MEDIUM else "low",
            )
        return "symbol_index", "medium" if score >= _FUZZY_MEDIUM else "low"

    if mode == "semantic":
        src = "vector_index+reranker" if rerank else "vector_index"
        if rerank and score >= _SEMANTIC_HIGH:
            return src, "high"
        if score >= _SEMANTIC_MED:
            return src, "medium"
        return src, "low"

    if mode == "text":
        # text_index = the indexed-grep backend; an exact line match is the
        # strongest signal it ever emits, so confidence=high there.
        if any(w.startswith("text:") for w in why):
            return "text_index", "high"
        return "text_index", "medium"

    if mode == "hybrid":
        labels = sorted({
            w.split(":", 1)[1]
            for w in why
            if w.startswith("hybrid:")
        })
        src = "hybrid:" + "+".join(labels) if labels else "hybrid"
        if len(labels) >= 2:
            # Found by both symbol AND semantic — strongest hybrid signal.
            return src, "high"
        if score >= _HYBRID_DECISIVE:
            return src, "medium"
        return src, "low"

    # Defensive: references / unknown — preserve whatever the row carries.
    return row.get("source") or "", row.get("confidence") or ""


def _to_structured(r: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a backend result row to the public JSON schema.

    Schema (stable for AI editor consumption):
      file:        workspace-relative path
      line:        1-indexed start line
      end_line:    1-indexed end line (when available, else == line)
      symbol:      symbol name (may be empty for text/grep hits)
      kind:        symbol_type / chunk_type / match_type
      score:       float relevance score
      why_matched: list of compact tags
      signature:   one-line declaration (when available)
      snippet:     dict with ``before`` / ``line`` / ``after`` for text mode,
                   or null when the row carries only a signature.
      source:      which backend produced this row
      confidence:  high / medium / low
    """
    file = r.get("file_path", "")
    line = (
        r.get("line_number")
        or r.get("line_start")
        or 0
    )
    end_line = r.get("line_end") or line
    sig = r.get("signature", "") or ""
    line_content = r.get("line_content", "")

    snippet: Optional[Dict[str, Any]] = None
    if line_content:
        snippet = {
            "before": list(r.get("context_before") or []),
            "line": line_content,
            "after": list(r.get("context_after") or []),
            "merged_lines": list(r.get("merged_lines") or []),
        }

    return {
        "file": file,
        "line": line,
        "end_line": end_line,
        "symbol": r.get("symbol_name") or "",
        "kind": r.get("kind") or r.get("symbol_type") or r.get("chunk_type") or r.get("match_type") or "",
        "score": float(r.get("relevance_score", 0) or 0),
        "why_matched": list(r.get("why_matched") or []),
        "signature": sig[:200],
        "snippet": snippet,
        "source": r.get("source") or "",
        "confidence": r.get("confidence") or "",
    }


def _render_one_result(idx: int, r: Dict[str, Any], *, with_snippet: bool) -> List[str]:
    """Format a single hit — works for symbol / semantic / text / reference."""
    file = r.get("file_path", "")
    line_no = (
        r.get("line_number")
        or r.get("line_start")
        or 0
    )
    name = r.get("symbol_name") or file or "?"
    kind = r.get("symbol_type") or r.get("chunk_type") or r.get("match_type") or ""
    score = r.get("relevance_score", 0)
    why = r.get("why_matched") or []
    sig = r.get("signature", "")
    line_content = r.get("line_content", "")
    ctx_before = r.get("context_before") or []
    ctx_after = r.get("context_after") or []

    out = [f"{idx}. {name}"]
    if file:
        out.append(f"   📄 {file}:{line_no}")
    if kind or why:
        tail = f"score={score:.2f}" if isinstance(score, (int, float)) else ""
        if why:
            tail += f"  why={','.join(why[:4])}"
        out.append(f"   🏷️ {kind}  {tail}".rstrip())

    if not with_snippet:
        out.append("")
        return out

    if line_content:
        # Text-mode hit: render ±N lines context.
        merged_extra = r.get("merged_lines") or []
        if merged_extra:
            out.append(
                f"   📜 snippet (+ {len(merged_extra)} adjacent match"
                f"{'es' if len(merged_extra) != 1 else ''}):"
            )
        else:
            out.append("   📜 snippet:")
        start_line = line_no - len(ctx_before)
        merged_set = set(merged_extra)
        for offset, raw_line in enumerate(ctx_before + [line_content] + ctx_after):
            n = start_line + offset
            if n == line_no or n in merged_set:
                marker = "►"
            else:
                marker = " "
            out.append(f"      {marker} {n:>4} | {raw_line}")
    elif sig:
        # Symbol/semantic hit: render the signature.
        out.append(f"   ✏️ {sig[:160]}")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# omni_read helpers
# ---------------------------------------------------------------------------
def _path_looks_unsafe(file: str) -> bool:
    """Return True when a requested path is outside workspace-relative form."""
    raw = file or ""
    normalised = raw.replace("\\", "/")
    parts = [p for p in normalised.split("/") if p]
    return (
        normalised.startswith("/")
        or re.match(r"^[A-Za-z]:[\\/]", raw) is not None
        or ".." in parts
    )


def _path_has_parent_reference(file: str) -> bool:
    normalised = (file or "").replace("\\", "/")
    return ".." in [p for p in normalised.split("/") if p]


def _is_path_guard_error(error: str) -> bool:
    """Classify read/diagnostics errors caused by workspace-boundary checks."""
    err_lower = (error or "").lower()
    return any(
        marker in err_lower
        for marker in (
            "access denied",
            "path escapes workspace",
            "outside workspace",
            "absolute path",
            "path traversal",
            "workspace-relative path",
        )
    )


def _safe_path_search_query(file: str) -> str:
    """Use only a basename for follow-up search hints after path rejection."""
    normalised = (file or "").replace("\\", "/").rstrip("/")
    basename = normalised.rsplit("/", 1)[-1] if normalised else ""
    return basename or "target file"


def _safe_rejected_file_label(file: str) -> str:
    """Avoid echoing unsafe user-submitted paths in top-level envelopes."""
    if not _path_looks_unsafe(file):
        return file
    basename = _safe_path_search_query(file)
    if basename and basename != "target file" and not _path_looks_unsafe(basename):
        return basename
    return "<rejected-path>"


def _path_guard_next_actions(file: str) -> List[str]:
    safe_query = _safe_path_search_query(file)
    safe_query_lit = json.dumps(safe_query, ensure_ascii=False)
    return [
        "Use a workspace-relative path inside the active workspace; "
        "do not retry '..' or absolute paths.",
        "omni_status() to inspect workspace_root and backend_workspace_root.",
        f"omni_search(query={safe_query_lit}, mode='text', format='json') "
        "to locate the intended file.",
    ]


def _emit_read_error(*, file: str, mode: str, error: str, fmt: str) -> str:
    """Render a structured error in the requested format.

    audit-bundle.r10 (read.error_next_actions): file-not-found errors now
    carry a recovery ``next_actions`` list so an AI editor knows what to
    try (path check / search / context). Other read errors get a generic
    next_actions hint pointing at outline/symbol/range.
    """
    safe_error = _sanitize_error_text(error)
    payload: Dict[str, Any] = {
        "ok": False,
        "file": file,
        "mode": mode,
        "error": safe_error,
    }
    err_lower = (safe_error or "").lower()
    if _is_path_guard_error(safe_error) or _path_looks_unsafe(file):
        payload["next_actions"] = _path_guard_next_actions(file)
    elif "file not found" in err_lower or "not found" in err_lower:
        payload["next_actions"] = [
            "Check the workspace-relative path and retry.",
            f"omni_search(query='{file}', mode='text', format='json') "
            "to search for a similar file.",
            "omni_context(task='find the relevant file', format='json') "
            "if you are unsure of the path.",
        ]
    else:
        payload["next_actions"] = [
            f"omni_read(file='{file}', mode='outline', format='json') "
            "to see the file structure.",
            "Re-check the read parameters (mode, symbol, start_line, query).",
        ]
    _stamp(payload, tool="omni_read")
    if (fmt or "json").lower() == "text":
        error = safe_error
        return f"❌ omni_read[{mode}] {file}: {error}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _read_illegal_mode_envelope(*, file: str, requested_mode: str, fmt: str) -> str:
    """Build the stamped illegal-mode envelope for omni_read.

    audit-bundle.r10 (read.valid_modes_envelope): mirrors the omni_search
    illegal-mode envelope so AI editors see a consistent shape across
    tools. JSON path returns ok=false + valid_modes + next_actions; text
    path keeps a human-readable line.
    """
    if (fmt or "json").lower() == "text":
        return (
            f"❌ Unknown read mode: '{requested_mode}'. "
            f"Valid modes: {', '.join(_READ_VALID_MODES)}"
        )
    payload: Dict[str, Any] = {
        "ok": False,
        "file": file,
        "requested_mode": requested_mode,
        "error": f"Unknown read mode: {requested_mode}.",
        "valid_modes": list(_READ_VALID_MODES),
        "next_actions": [
            "Retry with mode='outline' for a compact file overview.",
            "Use mode='symbol' with symbol='<name>' to read a specific symbol.",
            "Use mode='range' with start_line/end_line for a precise slice.",
        ],
    }
    _stamp(payload, tool="omni_read")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _truncate_with_lines(content: str, max_tokens: int) -> Tuple[str, bool, int]:
    """Truncate ``content`` so its token estimate fits ``max_tokens``.

    Cuts on line boundaries to avoid leaving the agent with a half-line.
    Returns ``(truncated_content, was_truncated, lines_kept)``.
    """
    if max_tokens <= 0:
        return content, False, content.count("\n") + 1
    if _approx_token_count(content) <= max_tokens:
        return content, False, content.count("\n") + 1
    # Approx: 4 chars per token → keep first 4*max_tokens chars on a line
    # boundary.
    approx_chars = max_tokens * 4
    cut = content[:approx_chars]
    last_nl = cut.rfind("\n")
    if last_nl > 0:
        cut = cut[:last_nl]
    return cut, True, cut.count("\n") + 1


def _build_read_payload(
    *,
    file: str,
    requested_mode: str,
    data: Dict[str, Any],
    start_line: Optional[int],
    end_line: Optional[int],
    symbol: Optional[str],
    query: Optional[str],
    max_tokens: int,
) -> Dict[str, Any]:
    """Normalise the backend ``/read`` response into a unified MCP payload.

    Always populates ``file``, ``mode``, ``language``, ``total_lines``,
    ``symbols``, ``content``, ``token_estimate``, ``truncated`` so the AI
    consumer doesn't have to handle six different shapes.
    """
    mode = requested_mode
    language = data.get("language", "") or ""
    total_lines = data.get("total_lines") or 0
    content: str = ""
    truncated = False
    truncate_note: Optional[str] = None

    payload: Dict[str, Any] = {
        "ok": True,
        "file": file,
        "mode": mode,
        "language": language,
        "total_lines": total_lines,
    }

    if mode in ("outline", "symbols"):
        symbols = data.get("symbols") or []
        payload["symbols"] = symbols
        payload["symbol_count"] = data.get("symbol_count", len(symbols))
        # Synthesise a compact text rendering so callers asking for a
        # token estimate still get a useful number.
        rendered = _format_outline_text(file, language, total_lines, symbols, mode)
        payload["content"] = rendered
        content = rendered

    elif mode == "imports":
        imports = data.get("imports") or []
        payload["imports"] = imports
        payload["import_count"] = data.get("import_count", len(imports))
        payload["ast_used"] = data.get("ast_used", False)
        content = data.get("content", "") or ""
        payload["content"] = content

    elif mode == "diagnostics":
        # NOTE: omni_read[diagnostics] now delegates to the same code
        # path as omni_diagnostics — this branch only runs as a defensive
        # fallback if the dispatcher is bypassed. The richer envelope
        # (counts/total_count/severity_filter/sources/tools_run/
        # tools_skipped/truncated) is added by the caller.
        diagnostics = data.get("diagnostics") or []
        payload["diagnostics"] = diagnostics
        # Keep the legacy diagnostic_count so older callers don't break,
        # but the canonical fields are counts + total_count.
        legacy_count = data.get("diagnostic_count", len(diagnostics))
        payload["diagnostic_count"] = legacy_count
        payload["counts"] = data.get("counts") or {
            "error": 0,
            "warning": 0,
            "info": 0,
            "total": legacy_count,
        }
        payload["total_count"] = data.get("total_count", legacy_count)
        payload["severity_filter"] = data.get("severity_filter", "all")
        payload["sources"] = data.get("sources", [])
        if "tools_run" in data:
            payload["tools_run"] = data.get("tools_run") or []
        if "tools_skipped" in data:
            payload["tools_skipped"] = data.get("tools_skipped") or []
        if "note" in data:
            payload["note"] = data["note"]
        # Cheap rendering for the token estimate.
        content = json.dumps(diagnostics, ensure_ascii=False)

    elif mode == "relevant_chunks":
        chunks = data.get("chunks") or []
        payload["query"] = data.get("query") or (query or "")
        payload["chunks"] = chunks
        payload["result_count"] = data.get("result_count", len(chunks))
        content = json.dumps(chunks, ensure_ascii=False)

    elif mode == "tests":
        payload["candidate_test_files"] = data.get("candidate_test_files", []) or []
        payload["graph_suggestions"] = data.get("graph_suggestions", []) or []
        payload["suggested_commands"] = data.get("suggested_commands", []) or []
        if "note" in data:
            payload["note"] = data["note"]
        content = json.dumps(payload["candidate_test_files"])

    else:
        # full / range / symbol → backend returns a content blob.
        raw_content = data.get("content", "") or ""
        # Range / symbol carry their resolved range — surface it.
        if "start_line" in data:
            payload["start_line"] = data.get("start_line")
        if "end_line" in data:
            payload["end_line"] = data.get("end_line")
        if "symbol_name" in data:
            payload["symbol"] = data.get("symbol_name")

        if mode == "full":
            cut, was_truncated, kept = _truncate_with_lines(raw_content, max_tokens)
            content = cut
            truncated = was_truncated
            payload["lines_returned"] = kept
            if was_truncated:
                truncate_note = (
                    f"Output truncated to ~{max_tokens} tokens. "
                    f"Use mode=range with start_line/end_line for a slice "
                    f"or mode=outline for a structural overview."
                )
        else:
            content = raw_content
            payload["lines_returned"] = (
                content.count("\n") + 1 if content else 0
            )
        payload["content"] = content

    payload["token_estimate"] = _approx_token_count(content) if content else 0
    payload["truncated"] = truncated
    if truncate_note:
        payload["truncation_hint"] = truncate_note

    # ------------------------------------------------------------------
    # Language fallback — guarantee a non-empty `language` for callers.
    # The backend fills this for outline / symbols / imports today, but
    # leaves it blank for symbol / range / full / diagnostics. Filling
    # from the file extension keeps the public schema consistent.
    # ------------------------------------------------------------------
    if not payload.get("language"):
        payload["language"] = _guess_language_from_path(file)

    # ------------------------------------------------------------------
    # next_actions — keep the agent moving regardless of which mode ran.
    # ------------------------------------------------------------------
    payload["next_actions"] = _next_actions_for_mode(
        mode=mode,
        symbol=payload.get("symbol") or symbol,
        file=file,
        truncated=truncated,
    )

    # ------------------------------------------------------------------
    # audit-bundle.r16 (P3-C): source / confidence stamping.
    #
    # omni_read previously omitted source/confidence on the grounds
    # that "the file content is the truth". That's correct for
    # raw-content modes (full/range/symbol/symbols/outline/imports
    # — the bytes are authoritative) but the contract should still
    # carry the same surface fields the rest of the toolkit emits,
    # so AI editors can reason uniformly. We stamp:
    #
    #   * source        — which subsystem produced the response:
    #                     ``ast`` (outline / symbols / imports / symbol),
    #                     ``raw_file`` (full / range — straight bytes),
    #                     ``vector`` (relevant_chunks),
    #                     ``guard+lsp`` (diagnostics — same as
    #                       omni_diagnostics for parity),
    #                     ``graph`` (tests — call-graph suggested tests)
    #   * confidence    — high when the source is authoritative
    #                     (raw bytes / AST exact match / linter rules
    #                     / call-graph definitive), medium when it's
    #                     a best-effort retrieval (vector chunks),
    #                     low only when results are empty.
    # ------------------------------------------------------------------
    _read_source_map = {
        "outline": "ast",
        "symbols": "ast",
        "symbol": "ast",
        "imports": "ast",
        "full": "raw_file",
        "range": "raw_file",
        "relevant_chunks": "vector",
        "diagnostics": "guard+lsp",
        "tests": "graph",
    }
    _read_source = _read_source_map.get(mode, "raw_file")
    if _read_source == "vector":
        # Vector retrieval is approximate by construction.
        chunks = payload.get("chunks") or []
        _read_conf = "medium" if chunks else "low"
    elif _read_source == "graph":
        # Graph-suggested tests: confidence reflects whether the graph
        # actually produced anything.
        _read_conf = "high" if payload.get("graph_suggestions") else "medium"
    else:
        # AST + raw_file + guard+lsp are authoritative when they yield
        # any content / symbols / diagnostics; otherwise medium.
        _has_payload = bool(
            payload.get("symbols")
            or payload.get("imports")
            or payload.get("content")
            or payload.get("diagnostics")
        )
        _read_conf = "high" if _has_payload else "medium"
    payload["source"] = _read_source
    payload["confidence"] = _read_conf

    # Version stamp — last so the registered handler itself carries the
    # current contract_version in every successful payload.
    _stamp(payload, tool="omni_read")

    return payload


# ---------------------------------------------------------------------------
# Mode → next_actions table.
#
# Every omni_read response now ships a short list of recommended next
# moves so the AI editor knows where to go without re-reading the docs.
# ---------------------------------------------------------------------------

# File extension → language. Mirrors the backend detector but works on
# pure path heuristics so it never blocks on the backend filling the
# field. Anything unknown stays empty.
_EXT_TO_LANGUAGE = {
    "py": "python", "pyi": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin", "kts": "kotlin",
    "rb": "ruby",
    "php": "php",
    "cs": "csharp",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
    "c": "c", "h": "c",
    "swift": "swift",
    "scala": "scala",
    "md": "markdown", "markdown": "markdown",
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
    "toml": "toml",
    "sh": "shell", "bash": "shell", "zsh": "shell",
    "html": "html", "htm": "html",
    "css": "css", "scss": "css",
    "sql": "sql",
}


def _guess_language_from_path(file: str) -> str:
    """Best-effort language inference from the file extension."""
    if not file:
        return ""
    name = file.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].lower()
    return _EXT_TO_LANGUAGE.get(ext, "")


def _next_actions_for_mode(
    *,
    mode: str,
    symbol: Optional[str],
    file: str,
    truncated: bool,
) -> List[str]:
    """Return the recommended next moves for ``mode``.

    Kept centralised so adding a new mode in the future means filling in
    one row here rather than hunting through the renderer.
    """
    sym = symbol or "<name>"

    if mode == "outline":
        return [
            "mode=symbol&symbol=<name> to read one symbol",
            "mode=relevant_chunks&query=<text> to RAG inside this file",
        ]
    if mode == "symbols":
        return [
            "mode=symbol&symbol=<name> to read a symbol body",
            "mode=outline for signatures + first docstring line",
        ]
    if mode == "symbol":
        return [
            f"omni_search(query='{sym}', mode='references') to find every callsite",
            f"omni_impact(symbol='{sym}') to check the blast radius",
            f"omni_diagnostics(file='{file}') before editing this symbol",
            "omni_patch action=preview before writing changes",
        ]
    if mode == "imports":
        return [
            "mode=outline to see every defined symbol",
            "omni_search(mode='references') to trace where these imports are used",
        ]
    if mode == "diagnostics":
        return [
            f"omni_diagnostics(file='{file}', severity='error') for the canonical view",
            "mode=range&start_line=&end_line= to read the offending lines",
        ]
    if mode == "range":
        return [
            "mode=outline for a structural overview of the whole file",
            "mode=symbol&symbol=<name> to jump to a specific definition",
        ]
    if mode == "full":
        if truncated:
            return [
                "mode=range&start_line=&end_line= to fetch a slice",
                "mode=outline to get the structure",
                "Pass a larger max_tokens only if you really need the full file",
            ]
        return [
            "mode=outline next time to save tokens on large files",
            f"mode=symbol&symbol=<name> for a single definition in {file}",
            "omni_patch action=preview before writing changes",
        ]
    if mode == "relevant_chunks":
        return [
            "mode=symbol&symbol=<name> to read the most-relevant symbol body",
            "mode=outline for the file's full structure",
        ]
    if mode == "tests":
        return [
            "Run the suggested_commands to see which tests still pass",
            "omni_impact(symbol='<name>') to add static-call-graph candidates",
        ]
    return []


def _build_local_read_payload(
    *,
    file: str,
    mode: str,
    symbol: Optional[str],
    start_line: Optional[int],
    end_line: Optional[int],
    max_tokens: int,
) -> Optional[Dict[str, Any]]:
    """Read local-authority full/range/symbol content from the MCP workspace.

    Hybrid status has long declared ``omni_read`` local-authority, but the
    handler still proxied to the configured backend. In hybrid mode that backend
    is the cloud mirror, whose filesystem may not contain synced snapshot files.
    This helper makes the local-authority path real for raw byte reads and
    symbol-body reads.
    """
    if mode not in {"full", "range", "symbol", "outline", "symbols", "imports"}:
        return None
    if mode == "symbol" and not symbol:
        return None
    try:
        path = _resolve_workspace_path(file)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = raw.splitlines()
    total_lines = len(lines)

    if mode in {"outline", "symbols"}:
        outline = _build_local_outline_payload(file)
        if not outline:
            return None
        payload = _build_read_payload(
            file=file,
            requested_mode=mode,
            data=outline,
            start_line=None,
            end_line=None,
            symbol=None,
            query=None,
            max_tokens=max_tokens,
        )
        payload["source"] = "local_ast"
        payload["confidence"] = "high" if outline.get("symbols") else "medium"
        payload["local_first"] = True
        payload["local_authority"] = True
        return payload

    if mode == "imports":
        imports = []
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "package ")):
                imports.append({"line": line_no, "text": stripped})
        payload = _build_read_payload(
            file=file,
            requested_mode="imports",
            data={
                "imports": imports,
                "import_count": len(imports),
                "content": "\n".join(i["text"] for i in imports),
                "total_lines": total_lines,
                "language": _guess_language_from_path(file),
                "ast_used": False,
            },
            start_line=None,
            end_line=None,
            symbol=None,
            query=None,
            max_tokens=max_tokens,
        )
        payload["source"] = "local_file"
        payload["confidence"] = "medium"
        payload["local_first"] = True
        payload["local_authority"] = True
        return payload

    if mode == "symbol":
        outline = _build_local_outline_payload(file)
        symbols = outline.get("symbols", []) if outline else []
        match = next(
            (
                row for row in symbols
                if isinstance(row, dict) and row.get("name") == symbol
            ),
            None,
        )
        if not match:
            return None
        start = int(match.get("line_start") or (match.get("lines") or [1])[0])
        end = int(match.get("line_end") or (match.get("lines") or [start, start])[-1])
    else:
        start = start_line or 1
        end = end_line if end_line is not None else (
            total_lines if mode == "full" else start + 50
        )

    if start < 1 or end < start:
        return None
    selected = lines[start - 1:min(end, total_lines)] if start <= total_lines else []
    content = "\n".join(
        f"{line_no} | {line}"
        for line_no, line in enumerate(selected, start=start)
    )
    data = {
        "content": content,
        "total_lines": total_lines,
        "start_line": start,
        "end_line": min(end, total_lines) if total_lines else 0,
        "language": _guess_language_from_path(file),
    }
    if mode == "symbol":
        data["symbol_name"] = symbol
    payload = _build_read_payload(
        file=file,
        requested_mode=mode,
        data=data,
        start_line=start,
        end_line=end,
        symbol=symbol if mode == "symbol" else None,
        query=None,
        max_tokens=max_tokens,
    )
    payload["source"] = "local_ast" if mode == "symbol" else "local_file"
    payload["confidence"] = "high"
    payload["local_first"] = True
    payload["local_authority"] = True
    if mode == "symbol":
        language = _guess_language_from_path(file)
        complete = not (
            language in {"scala", "java"} and int(data["start_line"]) == int(data["end_line"])
        )
        payload["symbol_body_complete"] = complete
        if not complete:
            warnings = list(payload.get("warnings") or [])
            warnings.append("parser_partial_symbol_body")
            payload["warnings"] = warnings
            payload["confidence"] = "medium"
            next_actions = list(payload.get("next_actions") or [])
            next_actions.insert(
                0,
                (
                    "omni_read(file='%s', mode='range', start_line=%s, "
                    "end_line=%s, format='json') for a larger manual slice."
                )
                % (file, data["start_line"], int(data["start_line"]) + 80)
            )
            payload["next_actions"] = next_actions
    return payload


def _build_fast_file_symbol_context_payload(
    *,
    file: str,
    symbol: str,
    task: Optional[str],
    token_budget: int,
    max_files: int,
) -> Optional[Dict[str, Any]]:
    """Fast deterministic context for explicit file+symbol anchors.

    Large-repo audits showed that a file+symbol context request can spend
    tens of seconds in diagnostics/graph/memory paths even though the caller
    already supplied a precise local anchor.  This helper returns the
    deterministic core immediately and marks advanced sections as unavailable
    instead of waiting for optional cloud analysis.
    """
    symbol_payload = _build_local_read_payload(
        file=file,
        mode="symbol",
        symbol=symbol,
        start_line=None,
        end_line=None,
        max_tokens=max(800, min(token_budget or 2000, 2500)),
    )
    if not symbol_payload:
        return None
    outline = _build_local_outline_payload(file) or {}
    symbols = outline.get("symbols") or []
    match = next(
        (
            row for row in symbols
            if isinstance(row, dict) and row.get("name") == symbol
        ),
        {},
    )
    start_line = (
        symbol_payload.get("start_line")
        or match.get("line_start")
        or ((match.get("lines") or [0])[0] if isinstance(match, dict) else 0)
        or 0
    )
    end_line = (
        symbol_payload.get("end_line")
        or match.get("line_end")
        or ((match.get("lines") or [start_line, start_line])[-1] if isinstance(match, dict) else start_line)
        or start_line
    )
    signature = (
        match.get("signature")
        if isinstance(match, dict)
        else None
    ) or f"{symbol}"
    content = str(symbol_payload.get("content") or "")
    context: Dict[str, Any] = {
        "primary_symbols": [
            {
                "name": symbol,
                "kind": match.get("kind") or match.get("type") or "definition",
                "file": file,
                "lines": [start_line, end_line],
                "signature": str(signature)[:160],
                "source": symbol_payload.get("source") or "local_ast",
                "confidence": symbol_payload.get("confidence") or "high",
            }
        ],
        "related_files": [],
        "diagnostics": [],
        "memories": [],
        "recent_changes": [],
        "references": [],
        "definition": {
            "available": True,
            "source": symbol_payload.get("source") or "local_ast",
            "name": symbol,
            "file": file,
            "line": start_line,
            "signature": str(signature)[:160],
        },
        "local_neighborhood": {
            "available": True,
            "source": symbol_payload.get("source") or "local_ast",
            "file": file,
            "start_line": start_line,
            "end_line": end_line,
            "content": content,
            "symbol_body_complete": symbol_payload.get("symbol_body_complete"),
        },
        "semantic": {
            "available": False,
            "source": "semantic",
            "reason": "skipped in deterministic file+symbol fast path",
        },
        "graph": {
            "available": False,
            "source": "graph",
            "reason": "graph_index_unavailable; deterministic context returned",
        },
    }
    if outline.get("symbols"):
        context["outline"] = {
            "available": True,
            "source": outline.get("source") or "local_ast",
            "symbol_count": len(outline.get("symbols") or []),
        }
    capabilities_used = ["read.symbol", "read.outline"]
    capabilities_missing = ["impact.graph", "search.semantic"]
    if task:
        capabilities_missing.append("context.semantic_task_expansion")
    payload: Dict[str, Any] = {
        "ok": True,
        "task": task,
        "file": file,
        "symbol": symbol,
        "symbol_resolution": "found",
        "confidence": (
            "medium"
            if symbol_payload.get("symbol_body_complete") is False
            else "high"
        ),
        "token_budget": token_budget,
        "budget": token_budget,
        "budget_utilization": 0.0,
        "context": context,
        "context_builder": "deterministic_fast",
        "degraded": True,
        "capabilities_used": sorted(set(capabilities_used)),
        "capabilities_missing": sorted(set(capabilities_missing)),
        "diagnostics_status": {
            "ran": False,
            "source": "fast_path",
            "reason": "skipped to keep explicit file+symbol context deterministic and fast",
        },
        "memory_status": {
            "ran": False,
            "source": "fast_path",
            "reason": "skipped to keep explicit file+symbol context deterministic and fast",
        },
        "why_selected": [
            f"file+symbol fast path: {symbol} resolved locally in {file}:{start_line}",
            "advanced semantic/graph sections were skipped and marked degraded",
        ],
        "truncation_reasons": [],
        "truncated": bool(symbol_payload.get("truncated")),
        "freshness": "local_exact",
        "source": symbol_payload.get("source") or "local_ast",
        "next_actions": _next_actions_for_context(
            has_file=True,
            has_symbol=True,
            has_task=bool(task),
            symbol=symbol,
            file=file,
            primary_file=None,
        ),
    }
    payload["token_estimate"] = _approx_token_count(
        json.dumps(payload, ensure_ascii=False, default=str)
    )
    if token_budget > 0:
        payload["budget_utilization"] = round(
            min(payload["token_estimate"] / token_budget, 1.0),
            3,
        )
    return payload


def _build_local_outline_payload(file: str) -> Optional[Dict[str, Any]]:
    """Build a small outline directly from the local MCP workspace.

    Hybrid context gathering uses cloud analysis for expensive search/impact,
    but an explicit ``file=`` anchor must still respect the local checkout as
    the source of truth. The cloud mirror may be content-addressed or mounted
    outside the backend ``WORKING_DIR``, so a backend ``/read?mode=outline``
    can legitimately miss a file that exists locally and has already synced.
    """
    try:
        path = _resolve_workspace_path(file)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = raw.splitlines()
    symbols: List[Dict[str, Any]] = []
    stack: List[Tuple[int, str]] = []
    pattern = re.compile(
        r"^(?P<indent>\s*)(?P<kind>async\s+def|def|class)\s+"
        r"(?P<name>[A-Za-z_]\w*)"
    )
    pattern = re.compile(
        r"^(?P<indent>\s*)"
        r"(?:(?:public|private|protected|static|final|abstract|sealed|case|open|"
        r"override|implicit|lazy)\s+)*"
        r"(?P<kind>class|trait|object|interface|enum|async\s+def|def|function|func)\s+"
        r"(?P<name>[A-Za-z_]\w*)"
    )
    for line_no, line in enumerate(lines, 1):
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match.group("indent").replace("\t", "    "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        raw_kind = match.group("kind")
        kind = (
            "class"
            if raw_kind in {"class", "trait", "object", "interface", "enum"}
            else "function"
        )
        name = match.group("name")
        signature = line.strip()[:200]
        parent = stack[-1][1] if stack else None
        symbols.append({
            "name": name,
            "kind": kind,
            "type": kind,
            "signature": signature,
            "doc": "",
            "lines": [line_no, line_no],
            "line_start": line_no,
            "line_end": line_no,
            "parent": parent,
            "source": "local_file",
        })
        stack.append((indent, name))

    if _guess_language_from_path(file) == "python":
        try:
            import ast

            line_ends: Dict[int, int] = {}
            for node in ast.walk(ast.parse(raw)):
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    end_line = getattr(node, "end_lineno", None)
                    if isinstance(end_line, int):
                        line_ends[int(node.lineno)] = end_line
            for symbol_row in symbols:
                start = symbol_row.get("line_start")
                end = line_ends.get(start) if isinstance(start, int) else None
                if isinstance(start, int) and isinstance(end, int) and end >= start:
                    symbol_row["line_end"] = end
                    symbol_row["lines"] = [start, end]
        except Exception:
            pass

    return {
        "ok": True,
        "file": file,
        "mode": "outline",
        "total_lines": len(lines),
        "language": _guess_language_from_path(file),
        "symbols": symbols,
        "symbol_count": len(symbols),
        "source": "local_file",
        "confidence": "high",
        "local_authority": True,
    }


# ---------------------------------------------------------------------------
# omni_context helpers
# ---------------------------------------------------------------------------

# Stop-words that contribute nothing to a code search and would dilute
# the lexical-boost signal in task-mode context gathering.
_TASK_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "to", "in", "on", "for", "is", "be",
        "and", "or", "as", "by", "at", "with", "from", "into", "via",
        "this", "that", "these", "those", "how", "what", "when", "where",
        "why", "do", "does", "did", "use", "uses", "using", "make",
        "fix", "fixes", "modify", "change", "update", "add", "remove",
        "task", "code", "search", "search_mode",  # reserved meta-words
    }
)

_SNAKE_CASE_RE = re.compile(r"\b_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL_CASE_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*[a-z][A-Z][a-zA-Z0-9]*\b")
_DOTTED_IDENT_RE = re.compile(r"\b[A-Za-z_][\w.]*\.[A-Za-z_]\w*\b")


def _extract_lexical_terms(text: str) -> List[str]:
    """Pull out the kinds of tokens that point at code from a task string.

    Returns a deduplicated list, prioritising:
      1. snake_case identifiers (``_detect_mode``)
      2. dotted identifiers (``ProviderRegistry.test_provider``)
      3. CamelCase class-like names
      4. multi-word lowercase noun phrases minus stopwords (length >= 4)
    """
    if not text:
        return []
    found: List[str] = []
    seen: set = set()

    def _add(tok: str) -> None:
        if not tok:
            return
        key = tok.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(tok)

    for m in _SNAKE_CASE_RE.findall(text):
        _add(m)
    for m in _DOTTED_IDENT_RE.findall(text):
        _add(m)
    for m in _CAMEL_CASE_RE.findall(text):
        _add(m)
    # Plain words: keep ones that aren't stopwords + length >= 4.
    for w in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_]{3,}\b", text):
        if w.lower() in _TASK_STOPWORDS:
            continue
        if w in seen or w.lower() in seen:
            continue
        _add(w)
    return found


def _next_actions_for_context(
    *,
    has_file: bool,
    has_symbol: bool,
    has_task: bool,
    symbol: Optional[str],
    file: Optional[str],
    primary_file: Optional[str],
) -> List[str]:
    """Build mode-aware next_actions for omni_context responses.

    Symbol mode is the noisiest one — there are five obvious follow-ups
    (read, impact, references, diagnostics, patch preview); we list all
    five so the agent can pick. File and task modes get a smaller set.
    """
    actions: List[str] = []
    if has_symbol:
        sym = symbol or "<name>"
        actions.append(f"omni_read(file=..., mode='symbol', symbol='{sym}')")
        actions.append(f"omni_impact(symbol='{sym}')")
        actions.append(f"omni_search(query='{sym}', mode='references')")
        if file or primary_file:
            actions.append(
                f"omni_diagnostics(file='{file or primary_file}')"
            )
        actions.append("omni_patch(action='preview', file=..., content=...)")
    elif has_file:
        f = file or "<file>"
        actions.append(f"omni_read(file='{f}', mode='outline')")
        actions.append(f"omni_diagnostics(file='{f}')")
        actions.append(
            f"omni_search(query='<symbol>', mode='references') "
            f"to trace usages defined in {f}"
        )
    elif has_task:
        actions.append(
            "omni_search(query=<task keywords>, mode='hybrid') "
            "for keyword + semantic recall"
        )
        actions.append(
            "omni_read(file=<top related>, mode='outline') "
            "to drill into the most relevant file"
        )
        actions.append(
            "omni_context(symbol=<lexical hit>) once you have a symbol candidate"
        )
        actions.append(
            "omni_memory(action='advisory', task=<task>) "
            "for prior project lessons"
        )
    return actions


# ---------------------------------------------------------------------------
# omni_memory helpers
# ---------------------------------------------------------------------------

# omni_memory's allowed actions (single source of truth).
_MEMORY_ALLOWED_ACTIONS: Tuple[str, ...] = (
    "search", "store", "context", "advisory",
)


def _extract_memory_id(data: Dict[str, Any]) -> Optional[int]:
    """Pull the integer memory id out of a backend response.

    The memory backend has historically used several field names;
    accept all of them so an MCP-layer field rename in the future
    doesn't silently start returning ``memory_id=null`` again.
    """
    if not isinstance(data, dict):
        return None
    candidates = [
        data.get("memory_id"),
        data.get("id"),
        (data.get("memory") or {}).get("id") if isinstance(data.get("memory"), dict) else None,
        (data.get("item") or {}).get("id") if isinstance(data.get("item"), dict) else None,
        (data.get("result") or {}).get("id") if isinstance(data.get("result"), dict) else None,
    ]
    for cid in candidates:
        if cid is None:
            continue
        try:
            return int(cid)
        except (ValueError, TypeError):
            continue
    return None


def _is_dedup_response(
    data: Dict[str, Any], *, dedup_window_seconds: float = 5.0,
) -> Tuple[bool, Optional[str]]:
    """Heuristic: did the backend reuse an existing memory row?

    The memory_manager dedups by content fingerprint and returns the
    *original* row (timestamp + id). If the timestamp is older than
    ``dedup_window_seconds`` from now we treat that as a dedup hit and
    surface ``duplicate=true`` to the caller. Returns
    ``(is_duplicate, deduplication_reason)``.
    """
    import datetime as _dt

    ts = data.get("timestamp")
    if not isinstance(ts, str):
        return False, None
    try:
        # ISO-8601 without TZ → assume UTC for comparison sanity.
        parsed = _dt.datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False, None
    now = _dt.datetime.now(_dt.timezone.utc)
    age = (now - parsed).total_seconds()
    if age > dedup_window_seconds:
        return True, (
            f"timestamp_age:{age:.1f}s (>{dedup_window_seconds:.0f}s "
            f"window → existing row reused)"
        )
    return False, None


def _normalise_memory_row(
    raw: Dict[str, Any], *, score: Optional[float] = None,
    match_reason: Optional[str] = None,
    match_fields: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Convert a backend ``memory`` row to the public schema.

    Always emits ``memory_id`` (with ``id`` alias for back-compat),
    plus the canonical fields documented in memory.v2. ``score`` is
    rounded to 4 decimals for stable JSON diffs.
    """
    mem_raw = raw.get("memory")
    mem: Dict[str, Any] = mem_raw if isinstance(mem_raw, dict) else raw
    mid = _extract_memory_id(mem) or _extract_memory_id(raw)
    related_raw = mem.get("related_files") or []
    if isinstance(related_raw, str):
        related_values = [related_raw]
    elif isinstance(related_raw, (list, tuple, set)):
        related_values = list(related_raw)
    else:
        related_values = []
    out: Dict[str, Any] = {
        "memory_id": mid,
        "id": mid,  # alias
        "category": mem.get("category"),
        "content": _sanitize_public_path_text(mem.get("content") or ""),
        "importance": mem.get("importance"),
        "tags": mem.get("tags") or [],
        "timestamp": mem.get("timestamp"),
        "related_files": [
            ref for ref in (
                _sanitize_public_path_ref(path)
                for path in related_values
            )
            if ref
        ],
    }
    eff_score = score if score is not None else raw.get("relevance_score")
    if eff_score is not None:
        try:
            out["score"] = round(float(eff_score), 4)
        except (TypeError, ValueError):
            pass
    if match_reason or raw.get("match_reason"):
        out["match_reason"] = _sanitize_public_path_text(
            str(match_reason or raw.get("match_reason") or "")
        )
    if match_fields is not None:
        out["match_fields"] = _sanitize_memory_match_fields(match_fields)
    elif raw.get("match_fields"):
        out["match_fields"] = _sanitize_memory_match_fields(raw["match_fields"])
    if "score" in out and "confidence" not in out:
        s = out["score"]
        if s >= 0.7:
            out["confidence"] = "high"
        elif s >= 0.4:
            out["confidence"] = "medium"
        else:
            out["confidence"] = "low"
    return out


def _synthesise_advisory(
    *,
    symbol: Optional[str],
    file: Optional[str],
    task: Optional[str],
    memories: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a structured action-oriented advisory from recalled memories.

    Pure local synthesis (no LLM call): we group memories by category
    and weave them into a short summary + ``action_items`` + ``risks``
    list. The text is also flattened into ``advisory_text`` for callers
    that want a single string — emoji-free in JSON mode (callers
    rendering for humans add their own emoji).
    """
    target = symbol or file or task or "the upcoming change"
    mistakes = [m for m in memories if (m.get("category") or "") == "mistake"]
    solutions = [m for m in memories if (m.get("category") or "") == "solution"]
    learnings = [m for m in memories if (m.get("category") or "") == "learning"]
    architecture = [
        m for m in memories
        if (m.get("category") or "") in ("architecture", "integration")
    ]

    # Headline summary — names the target + top categories present.
    if memories:
        cat_summary = ", ".join(
            f"{len(c)} {label}"
            for c, label in (
                (mistakes, "mistake"),
                (solutions, "solution"),
                (learnings, "learning"),
                (architecture, "architecture"),
            ) if c
        )
        summary = (
            f"Recall for {target}: {cat_summary}. "
            f"Review action_items below before editing."
        )
    else:
        summary = (
            f"No prior memories matched {target}. "
            f"Proceed with caution and consider storing a new lesson "
            f"after this edit."
        )

    # Action items pull primarily from mistakes (the "don't do X" lessons)
    # and high-importance solutions; each becomes one item.
    def _first_sentence(text: str) -> str:
        text = (text or "").strip()
        for sep in (". ", "; ", "\n"):
            if sep in text:
                return text.split(sep, 1)[0].rstrip(".")
        return text[:200].rstrip(".")

    action_items: List[str] = []
    for m in mistakes[:5]:
        action_items.append(
            f"[mistake] {_first_sentence(m.get('content', ''))}."
        )
    for m in solutions[:3]:
        if (m.get("importance") or 0) >= 4:
            action_items.append(
                f"[solution] Reuse: {_first_sentence(m.get('content', ''))}."
            )

    # Risks: high-importance mistakes get a louder "regression risk" note.
    risks: List[str] = []
    for m in mistakes:
        if (m.get("importance") or 0) >= 4:
            risks.append(
                f"High-importance mistake recalled: "
                f"{_first_sentence(m.get('content', ''))}."
            )

    referenced = [
        {
            "memory_id": m.get("memory_id") or m.get("id"),
            "category": m.get("category"),
            "importance": m.get("importance"),
            "score": m.get("score"),
            "confidence": m.get("confidence"),
            "match_reason": m.get("match_reason"),
        }
        for m in memories
        if (m.get("memory_id") or m.get("id")) is not None
    ]

    # Plaintext flattening, no emoji.
    text_parts: List[str] = [summary]
    if action_items:
        text_parts.append("Action items:")
        for i, item in enumerate(action_items, 1):
            text_parts.append(f"{i}. {item}")
    if risks:
        text_parts.append("Risks:")
        for i, risk in enumerate(risks, 1):
            text_parts.append(f"{i}. {risk}")
    advisory_text = "\n".join(text_parts)

    # Confidence: high if we found a high-importance mistake exact-matching
    # the symbol/file/task; medium with any matches; low otherwise.
    if any((m.get("importance") or 0) >= 4 for m in mistakes):
        confidence = "high"
    elif memories:
        confidence = "medium"
    else:
        confidence = "low"

    why_recalled: List[str] = []
    if symbol:
        why_recalled.append(f"symbol:{symbol}")
    if file:
        why_recalled.append(f"file:{file}")
    if task:
        why_recalled.append(f"task:{task[:60]}")

    return {
        "summary": summary,
        "action_items": action_items,
        "risks": risks,
        "referenced_memories": referenced,
        "advisory_text": advisory_text,
        "why_recalled": why_recalled,
        "confidence": confidence,
    }


def _next_actions_for_memory(
    *, action: str, has_results: bool, memory_id: Optional[int],
    duplicate: bool,
    symbol: Optional[str] = None,
    file: Optional[str] = None,
    task: Optional[str] = None,
) -> List[str]:
    """Mode-specific next_actions for omni_memory responses.

    audit-bundle.r18 (P2): when the caller provided ``symbol`` /
    ``file`` / ``task`` we now interpolate the actual values into the
    suggested commands, matching the omni_impact / omni_read / omni_context
    style. Pre-r18 advisory recommended ``omni_search(query=<symbol>, ...)``
    with a literal ``<symbol>`` placeholder that an AI editor had to
    substitute by hand. Empty arguments fall back to the original
    placeholder so the caller can still see the schema.
    """
    safe_symbol = _sanitize_public_path_text(symbol) if symbol else None
    safe_file = _sanitize_public_path_ref(file) if file else None
    safe_task = _sanitize_public_path_text(task) if task else None
    sym_lit = json.dumps(safe_symbol, ensure_ascii=False) if safe_symbol else "<symbol>"
    file_lit = json.dumps(safe_file, ensure_ascii=False) if safe_file else "..."
    task_lit = json.dumps(safe_task, ensure_ascii=False) if safe_task else "..."
    if action == "search":
        if has_results:
            return [
                f"omni_memory(action='advisory', symbol={sym_lit}, "
                f"task={task_lit}) to synthesise action items from these memories",
                f"omni_memory(action='context', file={file_lit}, "
                f"symbol={sym_lit}) for the wider startup context",
            ]
        return [
            "omni_memory(action='store', content=..., category=..., "
            "importance=..., tags=[...]) to record a new lesson",
            "Try a broader query or omni_memory(action='context') for "
            "background context",
        ]
    if action == "store":
        if duplicate:
            return [
                "Existing memory was bumped (counters + tags merged). "
                "Use omni_memory(action='search') to inspect it.",
            ]
        if memory_id:
            return [
                f"omni_memory(action='advisory', symbol={sym_lit}, "
                f"task={task_lit}) to verify memory_id={memory_id} "
                "surfaces next time",
                "omni_memory(action='search', query=...) to confirm "
                "the new lesson is indexed",
            ]
        return [
            "Memory store reported no id; rerun "
            "omni_memory(action='search') with the same content to confirm "
            "the row landed.",
        ]
    if action == "context":
        return [
            f"omni_memory(action='advisory', symbol={sym_lit}, "
            f"task={task_lit}) for action-oriented synthesis",
            "omni_memory(action='search', query=...) to drill into a "
            "specific lesson",
        ]
    if action == "advisory":
        if has_results:
            actions = []
            if safe_symbol:
                actions.append(
                    f"omni_search(query={sym_lit}, mode='references', "
                    f"format='json') to find every callsite affected"
                )
                actions.append(
                    f"omni_impact(symbol={sym_lit}, format='json') to "
                    "confirm the blast radius"
                )
            else:
                actions.append(
                    "omni_search(query=<symbol>, mode='references') to find "
                    "every callsite affected"
                )
                actions.append(
                    "omni_impact(symbol=<symbol>) to confirm the blast radius"
                )
            actions.append(
                f"omni_patch(action='preview', file={file_lit}, "
                "content=...) before writing the change"
            )
            return actions
        return [
            "No relevant memories — omni_memory(action='store', ...) "
            "after this edit so the next agent benefits.",
        ]
    return []


# ---------------------------------------------------------------------------
# omni_patch helpers (audit-bundle.r7 / patch.v2)
# ---------------------------------------------------------------------------

# Single source of truth for omni_patch's allowed actions. Re-exported
# in error envelopes so AI editors can branch on a structured field
# instead of parsing the error message string.
_PATCH_ALLOWED_ACTIONS: Tuple[str, ...] = (
    "preview", "validate", "apply", "rollback", "sessions",
)


def _get_workspace_root() -> Tuple[Path, str, List[str]]:
    """Return ``(root, source, warnings)`` for the canonical workspace root.

    audit-bundle.r11 (workspace.root_alignment): every path-sensitive
    helper in this module — :func:`_resolve_workspace_path`, omni_patch's
    file_exists/new_file markers, the omni_edit alias guard, and the
    sessions ``unsafe_legacy_session`` annotation — must agree with the
    *backend's* file-IO root. Before r11 we used :func:`pathlib.Path.cwd`,
    which silently disagrees when the MCP host is launched from a
    different cwd than the backend's ``Settings.WORKING_DIR``.

    Resolution order (most authoritative first):

    1. ``omnicode_core.workspace.get_workspace_registry().get_active_path()``
       — the user-configured active workspace bookmark (persisted JSON).
    2. ``omnicode.config.settings.get_settings().WORKING_DIR`` — the env
       var ``WORKING_DIR`` (loaded once at process start) or its
       ``os.getcwd()`` default.
    3. ``Path.cwd()`` fallback — emits ``workspace_root_fallback_to_cwd``
       in the returned warnings list so omni_status can surface it.

    Returns
    -------
    root : Path
        The resolved workspace root (already passed through ``.resolve()``).
    source : str
        ``"workspace_registry"`` | ``"settings_working_dir"`` | ``"cwd_fallback"``.
    warnings : List[str]
        Empty when an authoritative source was found. ``["workspace_root_fallback_to_cwd"]``
        when only the cwd fallback was usable. Surfaced by omni_status.
    """
    warnings: List[str] = []

    # Explicit MCP-local workspace. In hybrid mode this is the user's real
    # checkout passed by ``omnicode mcp --workspace ...``; it must win over a
    # registry entry for the same workspace_id that points at a cloud mirror.
    try:
        import os as _os
        explicit = (
            _os.environ.get("OMNICODE_WORKSPACE_ROOT")
            or _os.environ.get("OMNICODE_WORKSPACE")
        )
        if explicit:
            p = Path(explicit)
            if p.is_dir():
                return p.resolve(), "explicit_local_workspace", warnings
    except Exception:
        pass

    # 1. Workspace registry — user-configured active bookmark.
    try:
        from omnicode_core.workspace import get_workspace_registry
        active = get_workspace_registry().get_active_path()
        if active:
            p = Path(active)
            if p.is_dir():
                return p.resolve(), "workspace_registry", warnings
    except Exception:
        # Registry import / load can fail in stripped-down test environments;
        # fall through to the next source.
        pass

    # 2. Settings.WORKING_DIR — pydantic-settings reads env then cwd.
    try:
        from omnicode.config.settings import get_settings
        wd = get_settings().WORKING_DIR
        if wd:
            p = Path(wd)
            if p.is_dir():
                return p.resolve(), "settings_working_dir", warnings
    except Exception:
        pass

    # 3. Last-resort fallback. Mark it explicitly so omni_status can flag.
    warnings.append("workspace_root_fallback_to_cwd")
    return Path.cwd().resolve(), "cwd_fallback", warnings


def _resolve_workspace_path(
    file: str, workspace_root: Optional[Path] = None,
) -> Path:
    """Resolve a user-supplied path to an absolute path inside the workspace.

    Hardens omni_patch (and any future tool that writes to disk) against
    three classes of escape:

      * absolute paths (``/etc/passwd``, ``C:\\Windows\\...``)
      * ``..`` traversal in path components
      * symlinks pointing outside the workspace

    Raises ``ValueError`` with a structured message when any of those
    conditions trip, so the caller can surface a stamped ``ok=false``
    response. The resolved path is returned to the caller for any
    downstream filesystem operation that needs the absolute form.

    Empty / whitespace input also raises — patch tools must not silently
    accept "no file".
    """
    if not file or not str(file).strip():
        raise ValueError("file path cannot be empty")
    raw = Path(str(file))
    if raw.is_absolute():
        raise ValueError(
            f"absolute paths are not allowed: {file!r}. "
            "Use a workspace-relative path."
        )
    # Reject ``..`` in any path component before resolving — resolve()
    # would canonicalise it away and we'd lose the intent.
    if ".." in raw.parts:
        raise ValueError(
            f"path traversal is not allowed: {file!r}. "
            "Use a workspace-relative path without '..'."
        )
    root = (workspace_root or _get_workspace_root()[0]).resolve()
    resolved = (root / raw).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as err:
        raise ValueError(
            f"path escapes workspace: {file!r} -> {resolved}. "
            f"Files must stay under {root}."
        ) from err
    return resolved


def _normalise_diff_text(diff: str) -> Tuple[str, bool]:
    """Collapse CRLF and the post-line blank-row pollution that the
    backend's diff renderer leaves in patch previews.

    Returns ``(normalised_diff, was_normalised)``. When the input is
    already clean, ``was_normalised`` is False. Used by the patch
    preview path so the public diff string isn't twice the size it
    should be just because the backend stitched ``\\r\\n`` and a stray
    blank line after every hunk row.
    """
    if not diff:
        return "", False
    original = diff
    # Step 1: CRLF → LF.
    out = diff.replace("\r\n", "\n").replace("\r", "\n")
    # Step 2: collapse any run of >=2 blank lines that appears
    # *between* diff body lines into a single blank line. We never want
    # to strip the leading/trailing newline structure; just remove the
    # backend's known pattern of injecting an empty line after every
    # `+`/`-`/` ` body row.
    lines = out.split("\n")
    collapsed: List[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            # Already emitted one blank, drop this one.
            continue
        collapsed.append(line)
        prev_blank = is_blank
    out = "\n".join(collapsed)
    return out, (out != original)


def _patch_path_guard_error(
    *, action: str, file: str, exc: ValueError,
) -> Dict[str, Any]:
    """Build the structured ``ok=false`` envelope for a path-guard
    rejection. Centralised so every omni_patch action returns the same
    shape, and tests can pin one schema."""
    return {
        "ok": False,
        "action": action,
        "file": _safe_rejected_file_label(file),
        "error": f"path-guard: {_sanitize_error_text(str(exc))}",
        "allowed_actions": list(_PATCH_ALLOWED_ACTIONS),
        "allowed_paths_pattern": "<workspace>/<relative-path>",
        "next_actions": [
            "Re-call omni_patch with a workspace-relative path "
            "(no leading '/', no '..').",
            "omni_status() to inspect the workspace root.",
        ],
    }


def _alias_envelope(alias: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp a deprecated-alias JSON response with the common compat
    fields: ``deprecated`` + ``alias`` + ``replacement`` + ``use_instead``
    + ``handler_version`` + ``contract_version`` (alias.compat.v1).

    Centralised so all three aliases (omni_analyze / omni_edit /
    omni_intelligence) return the same envelope shape, and so the alias
    contract never leaks into the core ``expected_contract_versions``
    audit surface. Idempotent on the keys it sets.
    """
    replacement = _ALIAS_REPLACEMENTS.get(alias, "")
    examples = {
        "omni_edit": (
            "omni_patch(action='preview', file='tests/example.py', "
            "content='...', format='json')"
        ),
        "omni_analyze": (
            "omni_impact(symbol='my_function', format='json')"
        ),
        "omni_intelligence": (
            "omni_context(symbol='my_function', "
            "task='understand before editing', format='json')"
        ),
    }
    payload.setdefault("deprecated", True)
    payload.setdefault("alias", alias)
    if replacement:
        payload.setdefault("replacement", replacement)
    payload.setdefault("use_instead", examples.get(alias, ""))
    payload.setdefault("handler_version", _HANDLER_VERSION)
    payload.setdefault("contract_version", _ALIAS_COMPAT_CONTRACT)
    return payload


def _alias_path_guard_error(alias: str, file: str, exc: ValueError) -> Dict[str, Any]:
    """Build the structured ``ok=false`` path-guard envelope for a
    deprecated alias (currently only omni_edit writes to disk). Reuses
    the same ValueError raised by :func:`_resolve_workspace_path` so the
    alias layer enforces the *exact* same workspace boundary as
    omni_patch v2 — the alias can never be a bypass around the guard."""
    replacement = _ALIAS_REPLACEMENTS.get(alias, "omni_patch")
    return _alias_envelope(alias, {
        "ok": False,
        "error": f"path-guard: {_sanitize_error_text(str(exc))}",
        "allowed_paths_pattern": "<workspace>/<relative-path>",
        "next_actions": [
            f"Use {replacement}(action='preview', file='<relative-path>', "
            "content='...', format='json').",
            "Avoid absolute paths and '..' segments.",
        ],
    })


def _format_outline_text(
    file: str,
    language: str,
    total_lines: int,
    symbols: List[Dict[str, Any]],
    mode: str,
) -> str:
    """Compact text rendering of outline/symbols, used both for ``format=text``
    and for computing a representative token estimate."""
    out = [f"📄 {file} ({total_lines} lines, {language})"]
    for s in symbols:
        name = s.get("name", "?")
        kind = s.get("kind") or s.get("type") or "symbol"
        if "lines" in s:
            sl, el = s.get("lines", [0, 0])
        else:
            sl = s.get("line_start", 0)
            el = s.get("line_end", 0)
        parent = s.get("parent") or ""
        prefix = "  └─ " if parent else ""
        out.append(f"{prefix}{kind} {name}  [L{sl}-{el}]")
        if mode == "outline":
            sig = (s.get("signature") or "")[:150]
            doc = (s.get("doc") or s.get("docstring") or "")[:100]
            if sig:
                out.append(f"     {sig}")
            if doc:
                out.append(f"     📝 {doc}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# discover_tools — Tool Intent Registry
# ---------------------------------------------------------------------------
#
# Two-layer matcher used by ``discover_tools``:
#
# 1. **Tool catalogue** — name + one-line description + scenario string +
#    EN/ZH keyword lists + deprecation flag.  Keyword tokens score the
#    tool when they appear in the (tokenised) query.
#
# 2. **Intent registry** — small, language-agnostic intent records.  Each
#    intent has a stable ID, recommended tools, language-specific
#    pattern lists (``patterns.en`` / ``patterns.zh``), and a suggested
#    pipeline rendered after a top match.  Intents fire when at least
#    one of their patterns is contained in the lower-cased raw query
#    (so multi-word phrases survive tokenisation).
#
# The whole pipeline is **rule-based** and offline.  ``matcher`` is
# accepted as a parameter so a future LLM/embedding backend can plug in
# without changing the call surface, but the default stays "rule".

_TOOL_CATALOGUE: List[Dict[str, Any]] = [
    {
        "name": "omni_context",
        "desc": "Composer — outline + impact + memory + git in one call",
        "scenario": (
            "First call when starting a task; aggregates outline / impact / "
            "memory / git into one response with a token_budget."
        ),
        "keywords_en": [
            "context", "compose", "task", "understand", "before",
            "summary", "lay of the land", "starting", "entry",
            "investigate", "explore", "what does",
        ],
        "keywords_zh": [
            "上下文", "理解", "梳理", "概览", "先看",
            "了解", "调研", "起点",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_search",
        "desc": "Search code (auto/semantic/symbol/text/hybrid/references)",
        "scenario": (
            "Find something by name, by literal string, or by natural "
            "language; mode=references for cross-file usages."
        ),
        "keywords_en": [
            "search", "find", "locate", "grep", "lookup", "look up",
            "symbol", "function", "method", "class", "where",
            "references", "usages", "callers", "imports",
            "semantic", "hybrid", "rrf", "fuzzy", "text",
        ],
        "keywords_zh": [
            "搜索", "查找", "找到", "定位", "符号", "函数", "方法",
            "类", "引用", "使用", "调用方", "查找", "搜寻",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_read",
        "desc": (
            "Read files (outline/symbols/full/imports/diagnostics/range/"
            "relevant_chunks/symbol)"
        ),
        "scenario": (
            "Read one file with the right granularity; use mode=outline "
            "first, then mode=symbol or mode=range to drill in."
        ),
        "keywords_en": [
            "read", "view", "open", "show", "print", "outline",
            "structure", "signature", "signatures", "imports",
            "lines", "range", "snippet", "function body",
            "definition", "what does", "implementation",
        ],
        "keywords_zh": [
            "读取", "查看", "打开", "显示", "概要", "结构",
            "签名", "导入", "实现", "函数体",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_impact",
        "desc": "Blast radius — callers/callees/risk/suggested tests",
        "scenario": (
            "Before any non-trivial edit; reports risk level + callers + "
            "callees + recommended tests."
        ),
        "keywords_en": [
            "impact", "blast", "radius", "risk", "callers", "callees",
            "affected", "depend", "dependencies", "dependents",
            "before changing", "before modifying", "before editing",
            "safe to change", "ripple", "graph",
        ],
        "keywords_zh": [
            "影响", "影响范围", "风险", "调用方", "依赖",
            "评估", "改动前", "修改前", "回归", "波及",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_diagnostics",
        "desc": "Lint / type / static-analysis diagnostics for a file",
        "scenario": (
            "Get ruff + mypy + bandit (Py) or eslint + tsc (TS/JS) issues "
            "for a single file."
        ),
        "keywords_en": [
            "diagnostics", "lint", "linter", "type", "types",
            "typecheck", "mypy", "ruff", "eslint", "tsc",
            "errors", "warnings", "static", "analysis",
            "issues", "problems",
        ],
        "keywords_zh": [
            "检查", "诊断", "lint", "类型", "类型错误",
            "静态分析", "错误", "警告", "问题", "类型检查",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_memory",
        "desc": "Project memory (search/store/advisory)",
        "scenario": (
            "Recall prior solutions / mistakes / architecture decisions; "
            "advisory mode auto-recalls on file + symbol + task."
        ),
        "keywords_en": [
            "memory", "remember", "recall", "history", "learned",
            "previous", "past", "advisory", "lesson", "lessons",
            "store", "save", "note",
        ],
        "keywords_zh": [
            "记忆", "回忆", "历史", "经验", "教训",
            "记下", "保存", "建议", "记录",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_patch",
        "desc": "Safe edit (preview / validate / apply / rollback / sessions)",
        "scenario": (
            "Never write to disk directly; always go preview → validate → "
            "apply, keep session_id for rollback."
        ),
        "keywords_en": [
            "patch", "edit", "modify", "change", "write",
            "preview", "validate", "apply", "rollback", "rewrite",
            "fix", "refactor", "session", "diff", "revert",
            "undo", "safely", "safe edit",
        ],
        "keywords_zh": [
            "修改", "编辑", "改动", "重构", "修复",
            "预览", "验证", "应用", "回滚", "撤销",
            "安全修改", "改写", "落盘", "差异",
        ],
        "deprecated": False,
    },
    {
        "name": "omni_skill",
        "desc": "Discover packaged workflow recipes (impact-review, safe-refactor, …)",
        "scenario": (
            "Look up a multi-step recipe before improvising — recipes "
            "bundle the right tool sequence."
        ),
        "keywords_en": [
            "skill", "recipe", "workflow", "playbook", "guide",
            "how to", "best practice", "pipeline",
        ],
        "keywords_zh": [
            "流程", "配方", "工作流", "指南", "最佳实践",
            "套路", "模板",
        ],
        "deprecated": False,
    },
    {
        "name": "discover_tools",
        "desc": "Find what's available — keyword + intent based",
        "scenario": "When unsure which tool fits the task.",
        "keywords_en": ["discover", "tools", "what tools", "available"],
        "keywords_zh": ["发现", "工具", "有哪些"],
        "deprecated": False,
    },
    # Deprecated aliases — only matched when user names them explicitly.
    {
        "name": "omni_analyze",
        "desc": "[deprecated alias] Use omni_impact",
        "scenario": "Kept for old MCP configs; new clients should use omni_impact.",
        "keywords_en": ["omni_analyze"],
        "keywords_zh": [],
        "deprecated": True,
        "alias_for": "omni_impact",
    },
    {
        "name": "omni_edit",
        "desc": "[deprecated alias] Use omni_patch (or omni_edit ai_edit when LLM_ROUTER=true)",
        "scenario": "Kept for old MCP configs; new clients should use omni_patch.",
        "keywords_en": ["omni_edit"],
        "keywords_zh": [],
        "deprecated": True,
        "alias_for": "omni_patch",
    },
    {
        "name": "omni_intelligence",
        "desc": "[deprecated alias] Use omni_context",
        "scenario": "Kept for old MCP configs; new clients should use omni_context.",
        "keywords_en": ["omni_intelligence"],
        "keywords_zh": [],
        "deprecated": True,
        "alias_for": "omni_context",
    },
]


_INTENT_REGISTRY: List[Dict[str, Any]] = [
    {
        "id": "safe_patch_flow",
        "recommended_tools": ["omni_patch"],
        "priority": 10,
        "patterns_en": [
            "preview validate apply", "validate apply rollback",
            "safe edit", "safely modify", "safely edit",
            "preview the diff", "rollback", "undo",
        ],
        "patterns_zh": [
            "安全修改", "安全编辑", "预览", "验证", "回滚",
            "撤销", "落盘前", "改完回滚",
        ],
        "keywords_en": [
            "preview", "validate", "apply", "rollback", "diff", "revert", "undo",
        ],
        "keywords_zh": ["预览", "验证", "应用", "回滚", "撤销", "差异"],
        "why_label": "intent:safe_patch_flow",
        "suggested_pipeline_kind": "safe_patch",
    },
    {
        "id": "find_references",
        "recommended_tools": ["omni_search"],
        "priority": 9,
        "patterns_en": [
            "find references", "find all references", "find all usages",
            "find usages", "where is", "where are", "who calls",
            "callsites of", "all callers", "cross-file references",
        ],
        "patterns_zh": [
            "查找引用", "查找所有引用", "所有引用",
            "调用了", "在哪里使用", "在哪里被调用",
            "谁调用了", "跨文件引用", "调用点",
        ],
        "keywords_en": ["references", "usages", "callsites", "callsite"],
        "keywords_zh": ["引用", "调用方", "使用处", "调用点"],
        "why_label": "intent:find_references",
        "suggested_pipeline_kind": "understanding",
    },
    {
        "id": "risk_analysis",
        "recommended_tools": ["omni_impact"],
        "priority": 9,
        "patterns_en": [
            "blast radius", "impact analysis", "risk before",
            "before changing", "before modifying", "before editing",
            "what will break", "who depends on",
        ],
        "patterns_zh": [
            "影响范围", "影响分析", "风险评估", "改动前",
            "修改前", "改之前", "波及范围", "依赖分析",
            "改了会影响", "影响和风险",
        ],
        "keywords_en": [
            "risk", "impact", "blast", "radius", "callers", "callees",
            "affected", "ripple", "depend",
        ],
        "keywords_zh": ["影响", "风险", "依赖", "波及", "调用方"],
        "why_label": "intent:risk_analysis",
        "suggested_pipeline_kind": "understanding",
    },
    {
        "id": "understand_before_edit",
        "recommended_tools": ["omni_context", "omni_read", "omni_impact", "omni_search"],
        "priority": 7,
        "patterns_en": [
            "understand a function", "understand the function",
            "understand the code", "before editing", "before modifying",
            "first understand", "explore the code",
            "what does this function do", "investigate the file",
        ],
        "patterns_zh": [
            "理解这个函数", "先理解", "先了解", "理解代码",
            "修改前先", "在修改前", "修改函数前",
            "了解一下", "搞清楚", "弄清楚",
            "这个函数做什么", "这个方法做什么",
        ],
        "keywords_en": [
            "understand", "investigate", "explore", "context",
            "function", "method", "class", "symbol", "code", "file",
        ],
        "keywords_zh": [
            "理解", "了解", "调研", "上下文", "函数",
            "方法", "类", "符号",
        ],
        "why_label": "intent:understand_before_edit",
        "suggested_pipeline_kind": "understanding",
    },
    {
        "id": "diagnostics_check",
        "recommended_tools": ["omni_diagnostics"],
        "priority": 9,
        "patterns_en": [
            "lint errors", "type errors", "static analysis",
            "lint or type", "typecheck", "lint warnings",
            "any errors", "any warnings",
        ],
        "patterns_zh": [
            "lint", "类型错误", "静态分析", "类型检查",
            "lint错误", "lint 错误", "lint 或 类型",
            "lint或类型", "lint 或 类型错误",
            "有没有错误", "有没有警告", "有没有问题",
            "检查错误", "检查文件", "检查这个文件",
        ],
        "keywords_en": [
            "diagnostics", "lint", "linter", "type", "typecheck",
            "mypy", "ruff", "eslint", "tsc", "errors", "warnings", "issues",
        ],
        "keywords_zh": [
            "检查", "诊断", "类型", "错误", "警告", "问题",
            "lint", "类型检查",
        ],
        "why_label": "intent:diagnostics_check",
        "suggested_pipeline_kind": None,
    },
    {
        "id": "memory_advisory",
        "recommended_tools": ["omni_memory"],
        "priority": 7,
        "patterns_en": [
            "what did we learn", "previous solutions", "past mistakes",
            "remember", "advisory", "recall",
        ],
        "patterns_zh": [
            "记得", "回忆", "之前的", "以前的", "经验",
            "教训", "建议", "历史上",
        ],
        "keywords_en": [
            "memory", "recall", "remember", "lesson", "advisory",
            "history", "past", "previous",
        ],
        "keywords_zh": ["记忆", "回忆", "经验", "历史", "建议"],
        "why_label": "intent:memory_advisory",
        "suggested_pipeline_kind": None,
    },
    {
        "id": "workflow_recipe",
        "recommended_tools": ["omni_skill"],
        "priority": 6,
        "patterns_en": [
            "show me a recipe", "best practice for", "workflow for",
            "how should i", "playbook",
        ],
        "patterns_zh": [
            "最佳实践", "推荐流程", "怎么做", "套路", "工作流",
        ],
        "keywords_en": ["recipe", "workflow", "playbook", "skill"],
        "keywords_zh": ["流程", "套路", "工作流", "配方", "指南"],
        "why_label": "intent:workflow_recipe",
        "suggested_pipeline_kind": None,
    },
]


_DEFAULT_PIPELINE = [
    "1. omni_skill(action='list')             — see if a recipe exists",
    "2. omni_context(file=… or task=…)       — gather outline + impact + memory + git",
    "3. omni_impact(symbol=…)                — check blast radius before editing",
    "4. omni_diagnostics(file=…)             — see existing lint/type issues",
    "5. omni_search(mode='references', …)    — find every callsite of the symbol",
    "6. omni_patch(action='preview', …)      — render the diff",
    "7. omni_patch(action='validate', …)     — run static checks on the patch",
    "8. omni_patch(action='apply', …)        — write + create rollback hook",
    "9. omni_patch(action='rollback', session_id=…)  — undo on regret",
]

_SAFE_PATCH_PIPELINE = [
    "1. omni_patch(action='preview', file=…, content=…)",
    "2. omni_patch(action='validate', file=…, content=…)",
    "3. omni_patch(action='apply', file=…, content=…)",
    "4. omni_patch(action='rollback', session_id=…)  # if needed",
]

_UNDERSTANDING_PIPELINE = [
    "1. omni_context(task=… or file=…)",
    "2. omni_search(mode='references', query=…)",
    "3. omni_read(file=…, mode='symbol', symbol=…)",
    "4. omni_impact(symbol=…)",
]

# Stop-words for tokenisation. EN + a small set of CN stop characters /
# function words.
_DISCOVER_STOPWORDS = {
    "i", "me", "my", "we", "us", "you", "your", "to", "the", "a", "an",
    "and", "or", "of", "in", "on", "for", "is", "be", "with", "before",
    "after", "this", "that", "these", "those", "it", "its", "want",
    "need", "should", "would", "could", "can", "do", "does", "have",
    "has", "had", "but", "so", "as", "at", "by", "from", "into",
    "what", "which", "how",
    # Chinese function words (each entry will be tested whole, not as substring)
    "我", "想", "要", "在", "的", "了", "有", "和", "与",
    "请", "把", "让", "对", "这", "那", "它",
}


# Tokeniser supports BOTH Latin words (``[A-Za-z_]+``) AND CJK characters
# treated as individual tokens.  This lets the CN keyword index work
# without a real Chinese segmenter.
_DISCOVER_TOKEN_RE = re.compile(r"[A-Za-z_]+|[\u4e00-\u9fff]")


def _tokenise_query(query: str) -> List[str]:
    raw = _DISCOVER_TOKEN_RE.findall(query.lower())
    return [t for t in raw if t not in _DISCOVER_STOPWORDS and len(t) >= 1]


def _phrase_in_query(phrase: str, query_lower: str) -> bool:
    """Whether ``phrase`` appears verbatim in the lower-cased query.

    For CJK phrases we additionally try a whitespace-stripped form so
    casual typing like ``查找  这个 函数`` still matches ``查找这个函数``.
    """
    p = phrase.lower().strip()
    if not p:
        return False
    if p in query_lower:
        return True
    # Whitespace-insensitive fallback for CJK: strip ALL whitespace from
    # both sides and retry.
    if any("\u4e00" <= ch <= "\u9fff" for ch in p):
        compact_q = re.sub(r"\s+", "", query_lower)
        compact_p = re.sub(r"\s+", "", p)
        if compact_p in compact_q:
            return True
    return False


def _recommend_tools_payload(
    query: str,
    *,
    matcher: str = "rule",
    capability_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the structured ``discover_tools`` data for ``query``.

    Pure function. Used by both the text renderer (``_recommend_tools``)
    and the JSON path of the MCP tool. Output schema (stable for AI
    editor consumption)::

        {
          "ok":           True,
          "query":        str,
          "matcher":      str,
          "matcher_note": str,           # populated when matcher=="embedding"
          "mode":         "default" | "ranked" | "no_match",
          "default_tools":      List[{name, desc}],         # mode=default / no_match
          "default_pipeline":   List[str],                  # mode=default / no_match
          "results":            List[{name, desc, scenario, score, why_matched, deprecated}],
          "pipeline_kind":      "safe_patch" | "understanding" | "",
          "pipeline":           List[str],
        }
    """
    matcher_note = ""
    if matcher and matcher.lower() == "embedding":
        matcher_note = (
            "matcher='embedding' is reserved for a future backend; "
            "falling back to rule-based matching."
        )

    # Empty / whitespace-only query → default listing.
    if not (query and query.strip()):
        non_deprecated = [t for t in _TOOL_CATALOGUE if not t["deprecated"]]
        return _finalize_discover_payload({
            "ok": True,
            "query": query or "",
            "matcher": matcher or "rule",
            "matcher_note": matcher_note,
            "mode": "default",
            "default_tools": [
                {"name": t["name"], "desc": t["desc"]}
                for t in non_deprecated
            ],
            "default_pipeline": list(_DEFAULT_PIPELINE),
            "results": [],
            "pipeline_kind": "",
            "pipeline": [],
            # audit-bundle.r18 (P3): mirror the default workflow as
            # next_actions for cross-tool field-name uniformity.
            "next_actions": list(_DEFAULT_PIPELINE),
        }, capability_registry=capability_registry)

    query_lower = query.lower()
    tokens = _tokenise_query(query)
    token_set = set(tokens)

    explicit_alias: Optional[str] = None
    for t in _TOOL_CATALOGUE:
        if t["deprecated"] and t["name"].lower() in query_lower:
            explicit_alias = t["name"]
            break

    scores: Dict[str, int] = {}
    why: Dict[str, List[str]] = {}

    # ---- 1) Per-tool keyword overlap -----------------------------------
    for t in _TOOL_CATALOGUE:
        if t["deprecated"] and t["name"] != explicit_alias:
            continue
        en_hits = [k for k in t.get("keywords_en", []) if k in token_set]
        zh_hits = [k for k in t.get("keywords_zh", []) if k in query_lower]
        all_hits = en_hits + zh_hits
        if all_hits:
            scores[t["name"]] = scores.get(t["name"], 0) + 3 * len(all_hits)
            why.setdefault(t["name"], []).extend(
                f"keyword:{h}" for h in all_hits[:3]
            )

    # ---- 2) Intent registry --------------------------------------------
    pipeline_for_top: Dict[str, str] = {}
    for intent in sorted(_INTENT_REGISTRY, key=lambda i: -i["priority"]):
        pat_hits = [
            p for p in intent.get("patterns_en", []) + intent.get("patterns_zh", [])
            if _phrase_in_query(p, query_lower)
        ]
        en_kw_hits = [k for k in intent.get("keywords_en", []) if k in token_set]
        zh_kw_hits = [k for k in intent.get("keywords_zh", []) if k in query_lower]
        kw_hits = en_kw_hits + zh_kw_hits
        if not pat_hits and not kw_hits:
            continue
        bonus = intent["priority"] if pat_hits else max(2, intent["priority"] - 4)
        for tgt in intent["recommended_tools"]:
            scores[tgt] = scores.get(tgt, 0) + bonus
            tag = intent["why_label"]
            why.setdefault(tgt, []).append(tag)
        kind = intent.get("suggested_pipeline_kind")
        if kind and intent["recommended_tools"]:
            pipeline_for_top.setdefault(intent["recommended_tools"][0], kind)

    # ---- 3) Explicit deprecated-alias special case ----------------------
    if explicit_alias:
        modern = next(
            (t.get("alias_for") for t in _TOOL_CATALOGUE
             if t["name"] == explicit_alias),
            None,
        )
        scores[explicit_alias] = scores.get(explicit_alias, 0) + 4
        why.setdefault(explicit_alias, []).append("named:deprecated_alias")
        if modern:
            scores[modern] = scores.get(modern, 0) + 6
            why.setdefault(modern, []).append(f"replacement_for:{explicit_alias}")

    # ---- 4) Zero match fallback ----------------------------------------
    if not scores:
        return _finalize_discover_payload({
            "ok": True,
            "query": query,
            "matcher": matcher or "rule",
            "matcher_note": matcher_note,
            "mode": "no_match",
            "default_tools": [
                {"name": t["name"], "desc": t["desc"]}
                for t in _TOOL_CATALOGUE if not t["deprecated"]
            ],
            "default_pipeline": list(_DEFAULT_PIPELINE),
            "results": [],
            "pipeline_kind": "",
            "pipeline": [],
            # audit-bundle.r18 (P3): mirror the default workflow as
            # next_actions for cross-tool field-name uniformity.
            "next_actions": list(_DEFAULT_PIPELINE),
        }, capability_registry=capability_registry)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    info_by_name = {t["name"]: t for t in _TOOL_CATALOGUE}
    results: List[Dict[str, Any]] = []
    for name, score in ranked[:6]:
        info = info_by_name[name]
        results.append({
            "name": name,
            "desc": info["desc"],
            "scenario": info["scenario"],
            "score": score,
            "why_matched": list(why.get(name, []))[:4],
            "deprecated": bool(info.get("deprecated")),
        })

    top_name = ranked[0][0]
    pipeline_kind = pipeline_for_top.get(top_name) or ""
    if not pipeline_kind:
        # Top-tool-shaped fallback so the JSON path still suggests something
        # actionable when no intent declared a pipeline.
        if top_name == "omni_patch":
            pipeline_kind = "safe_patch"
        elif top_name in ("omni_context", "omni_impact", "omni_read", "omni_search"):
            pipeline_kind = "understanding"

    pipeline_steps: List[str] = []
    if pipeline_kind == "safe_patch":
        pipeline_steps = list(_SAFE_PATCH_PIPELINE)
    elif pipeline_kind == "understanding":
        pipeline_steps = list(_UNDERSTANDING_PIPELINE)

    return _finalize_discover_payload({
        "ok": True,
        "query": query,
        "matcher": matcher or "rule",
        "matcher_note": matcher_note,
        "mode": "ranked",
        "default_tools": [],
        "default_pipeline": [],
        "results": results,
        "pipeline_kind": pipeline_kind,
        "pipeline": pipeline_steps,
        # audit-bundle.r18 (P3): mirror ``pipeline`` as
        # ``next_actions`` for cross-tool field-name uniformity. AI
        # editors that use the canonical ``next_actions`` key now get
        # the same workflow steps without having to special-case
        # discover_tools. ``pipeline`` is preserved for back-compat.
        "next_actions": pipeline_steps,
    }, capability_registry=capability_registry)


def _capability_state(
    capabilities: Dict[str, Any],
    name: str,
    default: str = "unavailable",
) -> str:
    row = capabilities.get(name) if isinstance(capabilities, dict) else None
    if isinstance(row, dict):
        return str(row.get("state") or default)
    return default


def _capability_ready(capabilities: Dict[str, Any], name: str) -> bool:
    return _capability_state(capabilities, name) == "ready"


def _runtime_capability_registry_snapshot(
    *,
    cloud_available: Optional[bool] = None,
    semantic_index_ready: bool = False,
    graph_index_ready: bool = False,
) -> Dict[str, Any]:
    """Build the same capability registry shape omni_status exposes."""
    local_index_ready = False
    line_fts_available = False
    embedding_available = False
    try:
        import os as _os

        ws_root, _source, _warnings = _get_workspace_root()
        workspace_id = (
            _os.environ.get("OMNICODE_WORKSPACE_ID")
            or ws_root.name
            or "workspace"
        )
        from omnicode_core.workspace.exact_index import SnapshotExactIndex

        status = SnapshotExactIndex().status(workspace_id=workspace_id)
        local_index_ready = bool(
            int(status.get("files") or 0) > 0
            and int(status.get("symbols") or 0) > 0
        )
        line_fts_available = bool(status.get("line_fts_available"))
    except Exception:
        local_index_ready = False
        line_fts_available = False

    try:
        from omnicode_core.embeddings.models import embedding_status

        embedding_available = bool(embedding_status().get("available"))
    except Exception:
        embedding_available = False

    if cloud_available is None:
        cloud_available = False

    try:
        from omnicode_core.capabilities.registry import build_runtime_capabilities

        return build_runtime_capabilities(
            cloud_available=bool(cloud_available),
            local_index_ready=local_index_ready,
            line_fts_available=line_fts_available,
            embedding_available=embedding_available,
            semantic_index_ready=semantic_index_ready,
            graph_index_ready=graph_index_ready,
        )
    except Exception as exc:
        return {"warning": f"{exc.__class__.__name__}: {exc}"}


def _capability_preflight_payload(
    capabilities: Dict[str, Any],
    *,
    required: List[str],
    fallbacks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    rows: Dict[str, Any] = {}
    missing: List[str] = []
    degraded: List[str] = []
    for name in list(required or []) + list(fallbacks or []):
        if name in rows:
            continue
        row = capabilities.get(name) if isinstance(capabilities, dict) else None
        state = str(row.get("state")) if isinstance(row, dict) else "unavailable"
        rows[name] = row if isinstance(row, dict) else {
            "state": state,
            "provider": "unknown",
            "reason": "capability not reported by registry",
        }
        if name in required and state in {"unavailable", "unsupported"}:
            missing.append(name)
        elif name in required and state in {"partial", "degraded"}:
            degraded.append(name)
    usable_fallbacks: List[str] = []
    for name in list(fallbacks or []):
        row = rows.get(name)
        state = str(row.get("state")) if isinstance(row, dict) else "unavailable"
        if state in {"ready", "partial", "degraded"}:
            usable_fallbacks.append(name)
    can_execute = not missing or bool(usable_fallbacks)
    if missing and usable_fallbacks:
        policy_mode = "fallback"
    elif missing:
        policy_mode = "block"
    elif degraded:
        policy_mode = "degraded"
    else:
        policy_mode = "normal"
    return {
        "ready": not missing,
        "can_execute": can_execute,
        "execution_policy": {
            "mode": policy_mode,
            "can_execute": can_execute,
            "blocking_missing": [] if can_execute else list(missing),
            "usable_fallbacks": usable_fallbacks,
            "degraded_required": list(degraded),
        },
        "required": list(required or []),
        "fallbacks": list(fallbacks or []),
        "states": rows,
        "missing": missing,
        "degraded": degraded,
    }


def _diagnostics_capability_for_file(
    file: Optional[str],
    language: Optional[str] = None,
) -> str:
    lang = (language or "").strip().lower()
    if not lang and file:
        try:
            from omnicode_core.capabilities.languages import language_for_path

            lang = language_for_path(file)
        except Exception:
            lang = ""
    if lang in {"python", "py"}:
        return "diagnostics.python"
    if lang in {"java"}:
        return "diagnostics.java"
    if lang in {"scala"}:
        return "diagnostics.scala"
    return f"diagnostics.{lang or 'unknown'}"


def _read_capability_for_mode(mode: str) -> tuple[List[str], List[str]]:
    mode_norm = (mode or "outline").lower().strip()
    if mode_norm == "full":
        return ["read.full"], []
    if mode_norm == "range":
        return ["read.range"], ["read.full"]
    if mode_norm in {"symbol", "symbols"}:
        return ["read.symbol"], ["read.range", "read.outline"]
    if mode_norm == "diagnostics":
        return ["read.outline"], ["diagnostics.python", "diagnostics.java", "diagnostics.scala"]
    if mode_norm == "relevant_chunks":
        return ["read.full"], ["search.semantic"]
    return ["read.outline"], ["read.range"]


def _capability_requirements_for_payload(
    *,
    tool: str,
    payload: Dict[str, Any],
) -> tuple[List[str], List[str]]:
    if tool == "omni_search":
        plan = payload.get("query_plan") if isinstance(payload, dict) else None
        if isinstance(plan, dict):
            return (
                list(plan.get("required_capabilities") or []),
                list(plan.get("fallback_capabilities") or []),
            )
        return ["search.symbol_exact"], ["search.text_exact"]
    if tool == "omni_read":
        required, fallbacks = _read_capability_for_mode(
            str(payload.get("mode") or "outline")
        )
        if str(payload.get("mode") or "").lower() == "diagnostics":
            fallbacks.append(
                _diagnostics_capability_for_file(
                    str(payload.get("file") or ""),
                    str(payload.get("language") or ""),
                )
            )
        return required, list(dict.fromkeys(fallbacks))
    if tool == "omni_impact":
        return ["impact.graph"], ["search.symbol_exact", "search.text_exact"]
    if tool == "omni_context":
        return ["context.deterministic"], [
            "search.symbol_exact",
            "search.text_exact",
            "search.semantic",
            "impact.graph",
        ]
    if tool == "omni_diagnostics":
        return [
            _diagnostics_capability_for_file(
                str(payload.get("file") or ""),
                str(payload.get("language") or ""),
            )
        ], []
    if tool == "omni_patch":
        action = str(payload.get("action") or "").lower()
        fallbacks: List[str] = []
        if action == "validate":
            fallbacks.append(
                _diagnostics_capability_for_file(
                    str(payload.get("file") or payload.get("file_path") or ""),
                    None,
                )
            )
        return ["patch.safe_edit"], fallbacks
    return [], []


def _attach_capability_preflight(
    payload: Dict[str, Any],
    *,
    tool: str,
) -> None:
    if "capability_preflight" in payload:
        return
    required, fallbacks = _capability_requirements_for_payload(
        tool=tool,
        payload=payload,
    )
    if not required and not fallbacks:
        return
    capabilities = _runtime_capability_registry_snapshot()
    payload["capability_preflight"] = _capability_preflight_payload(
        capabilities,
        required=required,
        fallbacks=fallbacks,
    )


def _finalize_discover_payload(
    payload: Dict[str, Any],
    *,
    capability_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    capabilities = (
        capability_registry
        if isinstance(capability_registry, dict)
        else _runtime_capability_registry_snapshot()
    )
    local_index_ready = _capability_ready(capabilities, "search.symbol_exact")
    semantic_ready = _capability_ready(capabilities, "search.semantic")
    graph_ready = _capability_ready(capabilities, "impact.graph")
    cloud_sync_ready = _capability_ready(capabilities, "sync.cloud")

    required_bootstrap = []
    if not local_index_ready:
        required_bootstrap.append({
            "tool": "omni_index",
            "args": {
                "action": "bootstrap",
                "scope": "workspace",
                "background": False,
                "format": "json",
            },
            "reason": "local exact index is not ready",
        })

    recommended_tools = ["omni_status", "omni_read", "omni_patch"]
    if not local_index_ready:
        recommended_tools.append("omni_index")
    recommended_tools.append("omni_search")
    if graph_ready:
        recommended_tools.append("omni_impact")
    if semantic_ready:
        recommended_tools.append("omni_context")
    payload["recommended_tools"] = list(dict.fromkeys(recommended_tools))
    payload["capability_registry"] = capabilities
    payload["required_bootstrap"] = required_bootstrap
    payload["safe_edit_workflow"] = [
        "omni_read(file=..., mode='outline', format='json')",
        "omni_patch(action='preview', file=..., content=..., format='json')",
        "omni_patch(action='validate', file=..., content=..., format='json')",
        "omni_patch(action='apply', file=..., content=..., format='json')",
        "omni_patch(action='rollback', session_id=..., format='json') if needed",
    ]
    degraded_tools: List[Dict[str, Any]] = []
    disabled_tools: List[Dict[str, Any]] = []
    if not graph_ready:
        degraded_tools.append({
            "tool": "omni_impact",
            "capability": "impact.graph",
            "reason": capabilities.get("impact.graph", {}).get(
                "reason",
                "graph index unavailable; use deterministic fallback only",
            ),
        })
    if not semantic_ready:
        degraded_tools.append({
            "tool": "omni_context",
            "capability": "search.semantic",
            "reason": capabilities.get("search.semantic", {}).get(
                "reason",
                "semantic index unavailable; deterministic context only",
            ),
        })
        disabled_tools.append({
            "capability": "search.semantic",
            "reason": capabilities.get("search.semantic", {}).get(
                "reason",
                "semantic index is unavailable",
            ),
        })
    if not cloud_sync_ready:
        disabled_tools.append({
            "capability": "sync.cloud",
            "reason": capabilities.get("sync.cloud", {}).get(
                "reason",
                "cloud sync unavailable",
            ),
        })
    if _capability_state(capabilities, "diagnostics.scala") == "unsupported":
        disabled_tools.append({
            "capability": "diagnostics.scala",
            "reason": capabilities.get("diagnostics.scala", {}).get(
                "reason",
                "Scala diagnostics unsupported",
            ),
        })
    payload["degraded_tools"] = degraded_tools
    payload["disabled_tools"] = disabled_tools
    payload["compatibility_aliases"] = [
        "omni_analyze",
        "omni_edit",
        "omni_intelligence",
    ]
    return payload


def _recommend_tools(query: str, *, matcher: str = "rule") -> str:
    """Return the rendered ``discover_tools`` response for ``query``.

    Pure function — easy to unit test.  The MCP tool wrapper just
    forwards to this helper.

    ``matcher='rule'`` (the default) is the only implemented backend
    today.  ``matcher='embedding'`` is a reserved name so a future
    semantic backend can plug in without changing the call surface;
    requesting it falls back to ``rule`` with a note in the response.
    """
    # Empty / whitespace-only query → unchanged default listing.
    if not (query and query.strip()):
        non_deprecated = [t for t in _TOOL_CATALOGUE if not t["deprecated"]]
        lines = ["📦 OmniCode tools:\n"]
        for t in non_deprecated:
            lines.append(f"  • {t['name']:<18} {t['desc']}")
        lines.append("")
        lines.append("💡 Recommended flow before any edit:")
        for step in _DEFAULT_PIPELINE:
            lines.append(f"   {step}")
        return "\n".join(lines)

    matcher_note = ""
    if matcher and matcher.lower() == "embedding":
        matcher_note = (
            "\n\n📝 Note: matcher='embedding' is reserved for a future "
            "backend; falling back to rule-based matching."
        )

    query_lower = query.lower()
    tokens = _tokenise_query(query)
    token_set = set(tokens)

    # Detect explicit deprecated-alias mention so we can surface the alias
    # AND its modern replacement.
    explicit_alias: Optional[str] = None
    for t in _TOOL_CATALOGUE:
        if t["deprecated"] and t["name"].lower() in query_lower:
            explicit_alias = t["name"]
            break

    scores: Dict[str, int] = {}
    why: Dict[str, List[str]] = {}

    # ---- 1) Per-tool keyword overlap -----------------------------------
    for t in _TOOL_CATALOGUE:
        if t["deprecated"] and t["name"] != explicit_alias:
            continue
        en_hits = [k for k in t.get("keywords_en", []) if k in token_set]
        zh_hits = [k for k in t.get("keywords_zh", []) if k in query_lower]
        all_hits = en_hits + zh_hits
        if all_hits:
            scores[t["name"]] = scores.get(t["name"], 0) + 3 * len(all_hits)
            why.setdefault(t["name"], []).extend(
                f"keyword:{h}" for h in all_hits[:3]
            )

    # ---- 2) Intent registry --------------------------------------------
    pipeline_for_top: Dict[str, str] = {}
    for intent in sorted(_INTENT_REGISTRY, key=lambda i: -i["priority"]):
        # Pattern hit on either language?
        pat_hits = [
            p for p in intent.get("patterns_en", []) + intent.get("patterns_zh", [])
            if _phrase_in_query(p, query_lower)
        ]
        # Keyword hit on either language? (broader fallback)
        en_kw_hits = [k for k in intent.get("keywords_en", []) if k in token_set]
        zh_kw_hits = [k for k in intent.get("keywords_zh", []) if k in query_lower]
        kw_hits = en_kw_hits + zh_kw_hits
        if not pat_hits and not kw_hits:
            continue
        bonus = intent["priority"] if pat_hits else max(2, intent["priority"] - 4)
        for tgt in intent["recommended_tools"]:
            scores[tgt] = scores.get(tgt, 0) + bonus
            tag = intent["why_label"]
            why.setdefault(tgt, []).append(tag)
        # Remember the first (highest-priority) intent's pipeline kind so
        # we can render it after the ranked list.
        kind = intent.get("suggested_pipeline_kind")
        if kind and intent["recommended_tools"]:
            pipeline_for_top.setdefault(intent["recommended_tools"][0], kind)

    # ---- 3) Explicit deprecated-alias special case ----------------------
    if explicit_alias:
        modern = next(
            (t.get("alias_for") for t in _TOOL_CATALOGUE
             if t["name"] == explicit_alias),
            None,
        )
        scores[explicit_alias] = scores.get(explicit_alias, 0) + 4
        why.setdefault(explicit_alias, []).append("named:deprecated_alias")
        if modern:
            scores[modern] = scores.get(modern, 0) + 6
            why.setdefault(modern, []).append(f"replacement_for:{explicit_alias}")

    # ---- 4) Zero match fallback ----------------------------------------
    if not scores:
        lines = [
            f"🔍 No direct keyword match for '{query}'.",
            "",
            "📦 Showing default tool listing — pick one of:",
            "",
        ]
        for t in _TOOL_CATALOGUE:
            if t["deprecated"]:
                continue
            lines.append(f"  • {t['name']:<18} {t['desc']}")
        lines.append("")
        lines.append("💡 Default workflow before any edit:")
        for step in _DEFAULT_PIPELINE:
            lines.append(f"   {step}")
        return "\n".join(lines) + matcher_note

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    info_by_name = {t["name"]: t for t in _TOOL_CATALOGUE}
    lines = [f"🔍 Tools matching '{query}' (ranked):\n"]
    for name, score in ranked[:6]:
        info = info_by_name[name]
        tag = " ⚠️ deprecated alias" if info.get("deprecated") else ""
        lines.append(f"  • {name}{tag}  (score={score})")
        lines.append(f"      {info['desc']}")
        lines.append(f"      ↳ {info['scenario']}")
        if why.get(name):
            shown = ", ".join(why[name][:4])
            lines.append(f"      why_matched: {shown}")
        lines.append("")

    # Append a tailored pipeline based on the top match.
    top_name = ranked[0][0]
    pipeline_kind = pipeline_for_top.get(top_name)
    if pipeline_kind == "safe_patch" or top_name == "omni_patch":
        lines.append("💡 Safe edit pipeline:")
        for step in _SAFE_PATCH_PIPELINE:
            lines.append(f"   {step}")
    elif pipeline_kind == "understanding" or top_name in (
        "omni_context", "omni_impact", "omni_read", "omni_search",
    ):
        lines.append("💡 Pre-edit understanding pipeline:")
        for step in _UNDERSTANDING_PIPELINE:
            lines.append(f"   {step}")

    return "\n".join(lines) + matcher_note


def _render_read_payload_text(payload: Dict[str, Any]) -> str:
    """Render a payload built by :func:`_build_read_payload` as human text."""
    file = payload.get("file", "?")
    mode = payload.get("mode", "?")
    lang = payload.get("language", "")
    total = payload.get("total_lines", "?")

    if not payload.get("ok", True):
        return f"❌ omni_read[{mode}] {file}: {payload.get('error', '')}"

    if mode in ("outline", "symbols"):
        return payload.get("content") or _format_outline_text(
            file, lang, total, payload.get("symbols") or [], mode,
        )

    header = f"📄 {file}  [mode={mode}, {total} lines, {lang}]"
    if payload.get("truncated"):
        header += "  ⚠️ truncated"
    body = payload.get("content", "") or ""
    if mode == "diagnostics":
        diags = payload.get("diagnostics") or []
        if not diags:
            return f"{header}\n✅ no diagnostics"
        body_lines = [header, ""]
        for d in diags[:30]:
            sev = (d.get("severity") or "").lower()
            emoji = {"error": "❌", "warning": "⚠️", "info": "ℹ️", "hint": "💡"}.get(
                sev, "•"
            )
            anchor = f"L{d.get('line')}" if d.get("line") else "?"
            body_lines.append(
                f"  {emoji} {anchor} [{d.get('tool')}/{d.get('code') or '-'}] "
                f"{d.get('message', '')}"
            )
        return "\n".join(body_lines)

    return f"{header}\n\n{body}"


def register_high_level_tools(mcp, make_request):
    """Register the 6+1 high-level tools on the given FastMCP instance.

    Args:
        mcp: FastMCP instance
        make_request: async function(method, endpoint, **kwargs) -> dict
    """

    async def _collect_local_diagnostics_payload(
        file: str,
        severity: str = "all",
        sources: str = "guard,lsp",
    ) -> Optional[Dict[str, Any]]:
        """Run local-first diagnostics from the MCP workspace root.

        In hybrid mode diagnostics must not depend on the cloud mirror being
        mounted at the backend WORKING_DIR. If the file exists in the local
        checkout, this helper returns the canonical diagnostics envelope and
        marks LSP as skipped unless a local LSP bridge is wired in later.
        """
        try:
            local_path = _resolve_workspace_path(file)
        except ValueError as exc:
            return {
                "ok": False,
                "file": file,
                "error": str(exc),
                "next_actions": _path_guard_next_actions(file),
                "source": "local_file",
                "local_first": True,
            }
        if not local_path.is_file():
            return None

        wanted = {s.strip() for s in (sources or "").split(",") if s.strip()}
        diagnostics: List[Dict[str, Any]] = []
        tools_run: List[str] = []
        tools_skipped: List[str] = []

        if "guard" in wanted:
            try:
                from omnicode.guard.analyzer import ProactiveGuard

                guard_result = await ProactiveGuard().check(str(local_path))
                tools_run.extend(list(getattr(guard_result, "tools_run", []) or []))
                tools_skipped.extend(
                    list(getattr(guard_result, "tools_skipped", []) or [])
                )
                for issue in getattr(guard_result, "issues", []) or []:
                    sev_obj = getattr(issue, "severity", "warning")
                    sev = getattr(sev_obj, "value", str(sev_obj)).lower()
                    tool_name = (getattr(issue, "tool", "") or "guard").lower()
                    if tool_name == "mypy" and sev == "info":
                        continue
                    diagnostics.append({
                        "source": getattr(issue, "tool", None) or "guard",
                        "severity": sev,
                        "line": getattr(issue, "line", None),
                        "column": getattr(issue, "column", None),
                        "rule": getattr(issue, "code", None) or "",
                        "message": getattr(issue, "message", "") or "",
                    })
                if not tools_run and not tools_skipped:
                    tools_run.append("guard")
            except Exception as exc:
                tools_skipped.append(f"guard:{exc.__class__.__name__}")

        if "lsp" in wanted:
            tools_skipped.append("lsp:local_mcp_lsp_unavailable")

        sev_filter = (severity or "all").lower().strip()
        if sev_filter not in ("all", ""):
            if sev_filter == "error":
                allowed = {"error"}
            elif sev_filter == "warning":
                allowed = {"warning", "warn"}
            else:
                allowed = {sev_filter}
            diagnostics = [
                d for d in diagnostics
                if (d.get("severity") or "").lower() in allowed
            ]

        sev_rank = {"error": 0, "warning": 1, "warn": 1, "info": 2, "hint": 3}
        diagnostics.sort(key=lambda d: (
            sev_rank.get((d.get("severity") or "").lower(), 4),
            d.get("line") or 0,
        ))

        counts = {
            "error": sum(
                1 for d in diagnostics
                if (d.get("severity") or "").lower() == "error"
            ),
            "warning": sum(
                1 for d in diagnostics
                if (d.get("severity") or "").lower() in ("warning", "warn")
            ),
            "info": sum(
                1 for d in diagnostics
                if (d.get("severity") or "").lower() in ("info", "hint")
            ),
            "total": len(diagnostics),
        }

        return {
            "ok": True,
            "file": file,
            "severity_filter": severity,
            "sources": sorted(wanted),
            "tools_run": tools_run,
            "tools_skipped": tools_skipped,
            "diagnostics": diagnostics[:25],
            "counts": counts,
            "truncated": len(diagnostics) > 25,
            "total_count": len(diagnostics),
            "source": "local_guard",
            "local_first": True,
            "local_authority": True,
        }

    def _current_executor_mode() -> str:
        """Return the MCP executor mode from the same env surface as runtime config."""
        import os as _os

        return (
            _os.environ.get("OMNICODE_EXECUTOR_MODE")
            or _os.environ.get("OMNICODE_EXECUTOR")
            or "local"
        ).strip().lower()

    async def _collect_diagnostics_payload(
        file: str,
        severity: str = "all",
        sources: str = "guard,lsp",
    ) -> Dict[str, Any]:
        """Run the configured diagnostics sources for ``file``.

        Returns the canonical diagnostics envelope shared by
        ``omni_diagnostics`` and ``omni_read(mode="diagnostics")``::

            {
              "ok":               bool,
              "file":             str,
              "severity_filter":  str,
              "sources":          List[str],   # the requested sources
              "tools_run":        List[str],   # the sources that produced output
              "tools_skipped":    List[str],   # sources that errored / were absent
              "diagnostics":      List[Dict],  # up to 25 issues
              "counts":           {error, warning, info, total},
              "truncated":        bool,
              "total_count":      int,
            }

        On a hard error (file missing, no usable sources) returns
        ``{"ok": False, "file": file, "error": "..."}`` so callers can
        bubble the failure to the user verbatim.
        """
        import asyncio

        wanted = {s.strip() for s in (sources or "").split(",") if s.strip()}

        if _path_has_parent_reference(file):
            return {
                "ok": False,
                "file": file,
                "error": (
                    f"path-guard: path traversal is not allowed: {file!r}. "
                    "Use a workspace-relative path without '..'."
                ),
                "next_actions": _path_guard_next_actions(file),
            }

        if not wanted.intersection({"guard", "lsp"}):
            return {
                "ok": False,
                "file": file,
                "error": f"Unknown sources '{sources}'. Use: guard, lsp",
            }

        try:
            if _current_executor_mode() == "hybrid":
                local_payload = await _collect_local_diagnostics_payload(
                    file=file,
                    severity=severity,
                    sources=sources,
                )
                if local_payload is not None:
                    return local_payload
        except Exception:
            pass

        tasks = []
        labels = []
        if "guard" in wanted:
            tasks.append(make_request(
                "POST", "/guard/check", params={"file_path": file},
            ))
            labels.append("guard")
        if "lsp" in wanted:
            tasks.append(make_request(
                "GET", f"/lsp/diagnostics/{file}",
            ))
            labels.append("lsp")

        if not tasks:
            return {
                "ok": False,
                "file": file,
                "error": f"Unknown sources '{sources}'. Use: guard, lsp",
            }

        raws = await asyncio.gather(*tasks, return_exceptions=True)

        all_issues: List[Dict[str, Any]] = []
        tools_run: List[str] = []
        tools_skipped: List[str] = []
        file_missing = False

        for label, raw in zip(labels, raws, strict=False):
            if isinstance(raw, Exception):
                tools_skipped.append(label)
                continue
            data = raw.get("result", raw) if isinstance(raw, dict) else {}
            if label == "guard":
                issues = data.get("issues", []) or []
                for it in issues:
                    tool_name = (it.get("tool") or "guard").lower()
                    sev_value = (it.get("severity") or "warning").lower()
                    msg = (it.get("message") or "").lower()
                    if "file not found" in msg or "no such file" in msg:
                        file_missing = True
                        continue
                    # mypy "Hint:" / "See https://..." info notes are
                    # advisory, not actionable for an AI editor.
                    if tool_name == "mypy" and sev_value == "info":
                        continue
                    all_issues.append({
                        "source": it.get("tool") or "guard",
                        "severity": it.get("severity") or "warning",
                        "line": it.get("line"),
                        "column": it.get("column"),
                        "rule": it.get("code") or "",
                        "message": it.get("message") or "",
                    })
                tools_run.append(label)
                if not issues and (data.get("errors") or "").strip():
                    # Legacy text fallback when backend didn't structure.
                    for txt in (data.get("errors") or "").splitlines():
                        if txt.strip():
                            all_issues.append({
                                "source": "guard",
                                "severity": "error",
                                "line": None,
                                "rule": "",
                                "message": txt,
                            })
            elif label == "lsp":
                diags = data.get("diagnostics", []) or []
                for d in diags:
                    rng = d.get("range", {}).get("start", {}) if isinstance(d, dict) else {}
                    all_issues.append({
                        "source": "lsp",
                        "severity": d.get("severity") or "warning",
                        "line": rng.get("line"),
                        "column": rng.get("character"),
                        "rule": d.get("code") or "",
                        "message": d.get("message") or "",
                    })
                tools_run.append(label)

        if file_missing and not all_issues:
            return {
                "ok": False,
                "file": file,
                "error": f"File not found: {file}",
            }

        # Filter by severity.
        sev = (severity or "all").lower().strip()
        if sev not in ("all", ""):
            if sev == "error":
                wanted_set = {"error"}
            elif sev == "warning":
                wanted_set = {"warning", "warn"}
            else:
                wanted_set = {sev}
            all_issues = [
                i for i in all_issues
                if (i.get("severity") or "").lower() in wanted_set
            ]

        # Sort: errors first, then by line.
        sev_rank = {"error": 0, "warning": 1, "warn": 1, "info": 2, "hint": 3}
        all_issues.sort(key=lambda i: (
            sev_rank.get((i.get("severity") or "").lower(), 4),
            i.get("line") or 0,
        ))

        counts = {
            "error": sum(
                1 for i in all_issues
                if (i.get("severity") or "").lower() == "error"
            ),
            "warning": sum(
                1 for i in all_issues
                if (i.get("severity") or "").lower() in ("warning", "warn")
            ),
            "info": sum(
                1 for i in all_issues
                if (i.get("severity") or "").lower() == "info"
            ),
            "total": len(all_issues),
        }

        shown = all_issues[:25]
        truncated = len(all_issues) > 25

        return {
            "ok": True,
            "file": file,
            "severity_filter": severity,
            "sources": list(wanted),
            # ``source`` (singular) added in audit-bundle.r14 for contract
            # parity with omni_search / omni_impact / omni_patch — the
            # plural ``sources`` is kept for back-compat. Same value.
            "source": list(wanted),
            "tools_run": tools_run,
            "tools_skipped": tools_skipped,
            "diagnostics": shown,
            "counts": counts,
            "truncated": truncated,
            "total_count": len(all_issues),
        }

    async def _get_backend_file_markers(file: str) -> Dict[str, Any]:
        """Return backend-authoritative file_exists / new_file markers.

        audit-bundle.r12 (patch.backend_file_markers): r10 introduced
        ``file_exists`` / ``new_file`` on omni_patch responses by stat()-ing
        a path resolved against ``_get_workspace_root()``. r11 live
        verification proved the MCP host's workspace root and the FastAPI
        backend's CWD can differ (host launched from one dir, backend
        started from another) — in that case the local stat lied:
        ``omni_read`` happily read a file the markers reported as missing.

        This helper asks the *backend* whether it can see the file. We
        prefer ``/read?mode=outline`` because it's cheap (no diff render,
        no validation) and shares the same path resolver the writeable
        endpoints (``/patch/preview``, ``/patch/apply``) use, so its
        answer matches what apply will do on disk.

        Returns the canonical marker envelope. ``file_marker_authoritative``
        is True only when the probe returned a definitive yes/no.
        """
        envelope: Dict[str, Any] = {
            "file_exists": None,
            "new_file": None,
            "file_marker_source": "unknown",
            "file_marker_authoritative": False,
            "backend_workspace_root": None,
            "resolved_file_path": None,
            "file_marker_warning": None,
        }
        if not file:
            envelope["file_marker_warning"] = "no file argument"
            return envelope
        try:
            raw = await make_request(
                "POST", "/read",
                params={"file_path": file, "mode": "outline",
                        "with_line_numbers": False},
            )
        except Exception as exc:
            envelope["file_marker_warning"] = (
                f"backend probe failed: {exc.__class__.__name__}"
            )
            return envelope
        data: Dict[str, Any] = {}
        if isinstance(raw, dict):
            data = raw.get("result", raw) or {}
        if not isinstance(data, dict):
            envelope["file_marker_warning"] = "backend returned non-dict result"
            return envelope

        # Option B passthrough — if a future backend cooperates and returns
        # explicit existence markers, prefer them over the success-flag
        # heuristic. Look for any of these keys on the response.
        explicit = None
        for key in ("file_exists", "exists", "file_present"):
            if key in data and isinstance(data[key], bool):
                explicit = data[key]
                envelope["file_marker_source"] = "backend_patch_response"
                break
        if explicit is not None:
            envelope["file_exists"] = explicit
            envelope["new_file"] = not explicit
            envelope["file_marker_authoritative"] = True
        else:
            # Option A — read-probe heuristic. The most robust signal is
            # "did the backend return readable content/structure for this
            # file?". If yes, it exists. If we got an explicit not-found
            # error message, it doesn't exist. Anything else → null.
            success = data.get("success")
            error_msg = (data.get("error") or "").lower()
            has_payload = bool(
                data.get("content")
                or data.get("symbols")
                or data.get("total_lines")
                or data.get("imports")
                or data.get("diagnostics")
            )
            not_found_signal = any(s in error_msg for s in (
                "not found", "no such file", "does not exist",
                "file not found",
            ))
            if has_payload:
                # Backend returned a payload describing the file → exists.
                # We accept this even when ``success`` is missing or
                # explicitly false (some backends return success=false on
                # outline when an optional symbol-index is unavailable
                # but still ship the content).
                envelope["file_exists"] = True
                envelope["new_file"] = False
                envelope["file_marker_authoritative"] = True
                envelope["file_marker_source"] = "backend_read_probe"
            elif not_found_signal:
                envelope["file_exists"] = False
                envelope["new_file"] = True
                envelope["file_marker_authoritative"] = True
                envelope["file_marker_source"] = "backend_read_probe"
            elif success is True:
                # success=True with no payload — file exists but is empty
                # or backend stripped the body. Still exists.
                envelope["file_exists"] = True
                envelope["new_file"] = False
                envelope["file_marker_authoritative"] = True
                envelope["file_marker_source"] = "backend_read_probe"
            else:
                envelope["file_marker_warning"] = (
                    f"backend read response was inconclusive "
                    f"(success={success!r}, error={error_msg or 'none'!r})"
                )

        # Best-effort backend-root passthrough — if the backend exposes
        # its resolved path or root anywhere on the response, surface it
        # so callers can spot a host/backend cwd mismatch.
        for key in (
            "resolved_file_path", "absolute_path", "abs_path", "file_path",
        ):
            v = data.get(key)
            if isinstance(v, str) and v:
                envelope["resolved_file_path"] = v
                break
        for key in (
            "workspace_root", "backend_workspace_root", "working_dir",
        ):
            v = data.get(key)
            if isinstance(v, str) and v:
                envelope["backend_workspace_root"] = v
                break

        return envelope

    async def _do_validate(target_file: str, target_content: str) -> Dict[str, Any]:
        """Run the backend validator and return a structured envelope.

        audit-bundle.r13: lifted from inside ``omni_patch`` to the
        register-scope so deprecated aliases (currently only
        ``omni_edit``) can reuse the exact same validate logic and
        contract that ``omni_patch`` enforces — no parallel implementation,
        no semantic drift.

        Always returns a dict with at least:
          ok, validation_passed, message, checks, counts,
          tools_run, tools_skipped, source.
        """
        try:
            raw = await make_request("POST", "/patch/validate", json={
                "file_path": target_file, "content": target_content,
            })
        except Exception as exc:
            return {
                "ok": False,
                "validation_passed": False,
                "message": f"validate call failed: {exc}",
                "checks": [],
                "counts": {"error": 0, "warning": 0, "info": 0, "total": 0},
                "tools_run": [],
                "tools_skipped": [],
                "source": "guard",
            }
        data = raw.get("result", raw) if isinstance(raw, dict) else {}
        ok = bool(data.get("success", False))
        msg = data.get("message", "")
        # The backend used to drop ``checks=[]`` even when issues
        # were present; pull issues directly so AI editors can act
        # on structured rows. Normalise to the same diagnostics row
        # shape that omni_diagnostics emits.
        raw_issues = (
            data.get("issues")
            or data.get("checks")
            or data.get("diagnostics")
            or []
        )
        checks: List[Dict[str, Any]] = []
        for it in raw_issues:
            if not isinstance(it, dict):
                checks.append({
                    "source": "guard",
                    "severity": "error",
                    "line": None,
                    "column": None,
                    "rule": "",
                    "message": str(it),
                })
                continue
            checks.append({
                "source": it.get("source") or it.get("tool") or "guard",
                "severity": (it.get("severity") or "warning").lower(),
                "line": it.get("line"),
                "column": it.get("column"),
                "rule": it.get("rule") or it.get("code") or "",
                "message": it.get("message") or "",
            })
        counts = {
            "error": sum(1 for c in checks if c.get("severity") == "error"),
            "warning": sum(
                1 for c in checks
                if c.get("severity") in ("warning", "warn")
            ),
            "info": sum(1 for c in checks if c.get("severity") == "info"),
            "total": len(checks),
        }
        tools_run = list(
            data.get("tools_run") or data.get("sources") or []
        )
        tools_skipped = list(data.get("tools_skipped") or [])
        validation_passed = ok and counts["error"] == 0
        return {
            "ok": ok,
            "validation_passed": validation_passed,
            "message": msg or (
                f"Validation {'passed' if validation_passed else 'failed'}: "
                f"{counts['error']} error(s), {counts['warning']} warning(s)"
            ),
            "checks": checks,
            "counts": counts,
            "tools_run": tools_run,
            "tools_skipped": tools_skipped,
            "source": "guard",
        }

    async def _collect_advisory_payload(
        *,
        symbol: Optional[str] = None,
        file: Optional[str] = None,
        task: Optional[str] = None,
        query: Optional[str] = None,
        max_memories: int = 8,
        per_seed_max_results: int = 5,
        min_score: float = 0.3,
    ) -> Dict[str, Any]:
        """Multi-seed memory advisory pipeline shared between
        ``omni_memory(action='advisory')`` and the memory section of
        ``omni_context``.

        This is the audit-bundle.r6 follow-up that closes the
        ``memory_status.memory_count == 0`` bug in omni_context: rather
        than calling the legacy ``/memory/advisory`` backend (which
        returns the v1-shape advisory blob without proper ids), both
        tools now drive the recall through ``/memory/search`` and the
        local ``_synthesise_advisory`` helper, so:

          * every recalled row carries a real ``memory_id``
          * ``memory_count`` is the true number of unique referenced rows
          * advisory text is emoji-free (in JSON mode)
          * action_items / risks / referenced_memories are structured

        Returns the same envelope shape that ``_synthesise_advisory``
        emits, plus the full normalised ``memories[]`` rows so callers
        can decide how much to include.
        """
        seen_ids: set = set()
        merged: List[Dict[str, Any]] = []
        safe_symbol = _sanitize_public_path_text(symbol) if symbol else None
        safe_file = _sanitize_public_path_ref(file) if file else None
        safe_task = _sanitize_public_path_text(task) if task else None
        safe_query = _sanitize_public_path_text(query) if query else None
        seed_pairs = [
            (raw, safe)
            for raw, safe in (
                (symbol, safe_symbol),
                (file, safe_file),
                (task, safe_task),
                (query, safe_query),
            )
            if raw
        ]
        for seed, safe_seed in seed_pairs:
            try:
                srsp = await make_request("POST", "/memory/search", json={
                    "query": seed,
                    "max_results": per_seed_max_results,
                    "min_score": min_score,
                })
            except Exception:
                continue
            sdata = srsp.get("result", srsp) if isinstance(srsp, dict) else {}
            for raw in (sdata.get("results") or []):
                norm = _normalise_memory_row(
                    raw,
                    match_reason=raw.get("match_reason")
                    or f"seed:{str(safe_seed or '')[:32]}",
                )
                mid = norm.get("memory_id")
                if mid is None or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                merged.append(norm)

        # Sort: high importance + high score first.
        def _rank(m: Dict[str, Any]) -> Tuple[int, float]:
            return (
                -(m.get("importance") or 0),
                -float(m.get("score") or 0.0),
            )
        merged.sort(key=_rank)
        merged = merged[:max_memories]

        synth = _synthesise_advisory(
            symbol=safe_symbol, file=safe_file, task=safe_task or safe_query,
            memories=merged,
        )
        return {
            "summary": synth["summary"],
            "action_items": synth["action_items"],
            "risks": synth["risks"],
            "referenced_memories": synth["referenced_memories"],
            "advisory_text": synth["advisory_text"],
            "why_recalled": synth["why_recalled"],
            "confidence": synth["confidence"],
            "memories": merged,
            "memory_count": len(synth["referenced_memories"]),
        }

    def _analysis_freshness_fields(
        state: Dict[str, Any],
        *,
        freshness_mode: str = "strict",
    ) -> Dict[str, Any]:
        semantic_stale = bool(state.get("semantic_stale", state.get("stale")))
        exact_fresh = bool(state.get("exact_fresh", False))
        visible_stale = (
            False
            if freshness_mode == "exact" and exact_fresh
            else state.get("stale")
        )
        return {
            "freshness": state.get("freshness"),
            "freshness_mode": freshness_mode,
            "stale": visible_stale,
            "semantic_stale": semantic_stale,
            "exact_fresh": exact_fresh,
            "exact_stale": state.get("exact_stale"),
            "cloud_available": state.get("cloud_available"),
            "cloud_unavailable": state.get("cloud_unavailable", False),
            "backend_unreachable": state.get("backend_unreachable", False),
            "workspace_id": state.get("workspace_id"),
            "local_revision": state.get("local_revision"),
            "accepted_revision": state.get("accepted_revision"),
            "indexed_revision": state.get("indexed_revision"),
            "exact_indexed_revision": state.get("exact_indexed_revision"),
            "required_revision": state.get("required_revision"),
            "manifest_present": state.get("manifest_present"),
        }

    def _backend_error_message(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        status_code: Optional[int] = None
        try:
            raw_status = data.get("status_code")
            if raw_status is not None:
                status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = None
        error_text = data.get("error") or data.get("message")
        detail = data.get("detail")
        if not error_text and detail:
            if isinstance(detail, list):
                parts: List[str] = []
                for item in detail[:3]:
                    if isinstance(item, dict):
                        loc = item.get("loc")
                        msg = item.get("msg") or item.get("message")
                        if loc and msg:
                            parts.append(f"{'.'.join(map(str, loc))}: {msg}")
                        elif msg:
                            parts.append(str(msg))
                        else:
                            parts.append(str(item))
                    else:
                        parts.append(str(item))
                error_text = "; ".join(parts)
            else:
                error_text = str(detail)
        error_type = str(data.get("error_type") or "").lower()
        if (
            data.get("ok") is False
            or data.get("success") is False
            or (status_code is not None and status_code >= 400)
        ):
            if error_text:
                return str(error_text)
            if status_code is not None:
                return f"HTTP {status_code}"
            return "backend returned an error envelope"
        if error_text or error_type in {
            "connectionerror", "timeouterror", "httperror", "unexpectederror",
        }:
            return str(error_text or data.get("error_type") or "backend unavailable")
        nested = data.get("result")
        if isinstance(nested, dict) and nested is not data:
            return _backend_error_message(nested)
        return None

    async def _analysis_freshness_state() -> Dict[str, Any]:
        import os as _os

        executor = (
            _os.environ.get("OMNICODE_EXECUTOR_MODE") or "local"
        ).strip().lower()
        if executor != "hybrid":
            return {
                "enabled": False,
                "freshness": "not_applicable",
                "stale": False,
            }

        workspace_id = _os.environ.get("OMNICODE_WORKSPACE_ID") or ""
        if not workspace_id:
            return {
                "enabled": True,
                "freshness": "unknown",
                "stale": None,
                "workspace_id": None,
                "local_revision": None,
                "accepted_revision": None,
                "indexed_revision": None,
                "required_revision": None,
                "manifest_present": False,
                "error": "workspace_id is not configured",
            }

        local_revision: Optional[int] = None
        accepted_revision = 0
        indexed_revision = 0
        exact_indexed_revision = 0
        pending_count = 0
        manifest_present = False
        manifest_warning: Optional[str] = None

        try:
            from omnicode_core.workspace.local import LocalWorkspace
            from omnicode_core.workspace.manifest import (
                LocalManifest,
                default_manifest_path,
            )

            local_ws = LocalWorkspace(
                root=_get_workspace_root()[0],
                workspace_id=workspace_id,
            )
            manifest_path = default_manifest_path(workspace_id)
            manifest_present = manifest_path.exists()
            if manifest_present:
                manifest = LocalManifest.load(workspace=local_ws)
                local_revision = int(manifest.local_revision)
                pending_entries = manifest.data.get("pending") or []
                pending_count = (
                    len(pending_entries)
                    if isinstance(pending_entries, list)
                    else 0
                )
                accepted_revision = max(
                    accepted_revision,
                    int(manifest.data.get("last_accepted_revision", 0)),
                )
                indexed_revision = max(
                    indexed_revision,
                    int(manifest.data.get("last_indexed_revision", 0)),
                )
        except Exception as exc:
            manifest_warning = f"{exc.__class__.__name__}: {exc}"

        status_warning: Optional[str] = None
        try:
            raw = await make_request(
                "GET",
                "/sync/status",
                params={"workspace_id": workspace_id},
            )
            data = raw.get("result", raw) if isinstance(raw, dict) else {}
            backend_error = _backend_error_message(data)
            if backend_error:
                status_warning = backend_error
            elif isinstance(data, dict) and data.get("ok", True) is not False:
                accepted_revision = max(
                    accepted_revision,
                    int(data.get("accepted_revision") or 0),
                )
                indexed_revision = max(
                    indexed_revision,
                    int(data.get("indexed_revision") or 0),
                )
                exact_indexed_revision = max(
                    exact_indexed_revision,
                    int(data.get("exact_indexed_revision") or 0),
                )
            else:
                status_warning = str(
                    (data or {}).get("error")
                    if isinstance(data, dict)
                    else "sync status unavailable"
                )
        except Exception as exc:
            status_warning = f"{exc.__class__.__name__}: {exc}"

        if status_warning:
            required_revision = (
                max(local_revision, accepted_revision)
                if local_revision is not None
                else None
            )
            return {
                "enabled": True,
                "freshness": "unavailable",
                "stale": None,
                "cloud_available": False,
                "cloud_unavailable": True,
                "backend_unreachable": True,
                "workspace_id": workspace_id,
                "local_revision": local_revision,
                "accepted_revision": accepted_revision,
                "indexed_revision": indexed_revision,
                "exact_indexed_revision": exact_indexed_revision,
                "required_revision": required_revision,
                "manifest_present": manifest_present,
                "manifest_warning": manifest_warning,
                "status_warning": status_warning,
                "error": "cloud backend is unavailable",
            }

        if local_revision is None:
            return {
                "enabled": True,
                "freshness": "unknown",
                "stale": None,
                "cloud_available": True,
                "cloud_unavailable": False,
                "backend_unreachable": False,
                "workspace_id": workspace_id,
                "local_revision": None,
                "accepted_revision": accepted_revision,
                "indexed_revision": indexed_revision,
                "exact_indexed_revision": exact_indexed_revision,
                "required_revision": None,
                "manifest_present": manifest_present,
                "manifest_warning": manifest_warning,
                "status_warning": status_warning,
                "error": "local revision is unknown",
            }

        required_revision = (
            accepted_revision
            if pending_count <= 0 and accepted_revision > 0
            else max(local_revision, accepted_revision)
        )
        pending_stale = pending_count > 0
        semantic_stale = pending_stale or indexed_revision < required_revision
        exact_fresh = exact_indexed_revision >= required_revision
        freshness = (
            "stale"
            if semantic_stale and not exact_fresh
            else "exact_fresh"
            if semantic_stale and exact_fresh
            else "fresh"
        )
        return {
            "enabled": True,
            "freshness": freshness,
            "stale": semantic_stale,
            "semantic_stale": semantic_stale,
            "exact_fresh": exact_fresh,
            "exact_stale": not exact_fresh,
            "cloud_available": True,
            "cloud_unavailable": False,
            "backend_unreachable": False,
            "workspace_id": workspace_id,
            "local_revision": local_revision,
            "accepted_revision": accepted_revision,
            "indexed_revision": indexed_revision,
            "exact_indexed_revision": exact_indexed_revision,
            "required_revision": required_revision,
            "pending_count": pending_count,
            "manifest_present": manifest_present,
            "manifest_warning": manifest_warning,
            "status_warning": status_warning,
        }

    async def _analysis_freshness_gate(
        *,
        tool: str,
        fmt: str,
        allow_exact: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        state = await _analysis_freshness_state()
        if not state.get("enabled"):
            return None, {}
        if state.get("freshness") == "fresh":
            return None, _analysis_freshness_fields(state)
        if allow_exact and state.get("exact_fresh"):
            return None, _analysis_freshness_fields(
                state,
                freshness_mode="exact",
            )
        if allow_exact:
            local_exact = _local_exact_index_status_payload()
            if bool(local_exact.get("ready")):
                fields = _analysis_freshness_fields(
                    state,
                    freshness_mode="local_exact",
                )
                fields.update({
                    "freshness": "local_exact",
                    "stale": False,
                    "local_exact_index_ready": True,
                    "local_exact_index": local_exact,
                })
                return None, fields

        is_json_fmt = (fmt or "json").lower() == "json"
        freshness = state.get("freshness") or "unknown"
        if freshness == "stale":
            error = "Cloud index is stale"
        elif freshness == "exact_fresh":
            error = "Cloud semantic index is stale"
        elif freshness == "unavailable":
            error = "Cloud backend is unavailable"
        else:
            error = "Cloud index freshness is unknown"
        payload = {
            "ok": False,
            "tool": tool,
            "error": error,
            **_analysis_freshness_fields(state),
            "freshness_unknown": freshness == "unknown",
            "next_actions": [
                (
                    "Restart or reconnect the cloud backend, then retry this tool."
                    if freshness == "unavailable"
                    else (
                        "Use mode='symbol' or mode='text' for exact-index lookup, "
                        "or wait for semantic indexing to finish."
                        if freshness == "exact_fresh"
                        else "Wait for sync/indexing to finish, then retry this tool."
                    )
                ),
                "omni_status() to inspect local/cloud revisions.",
                "GET /sync/status?workspace_id=<workspace_id> to inspect cloud sync state.",
            ],
        }
        if state.get("manifest_warning"):
            payload["manifest_warning"] = state["manifest_warning"]
        if state.get("status_warning"):
            payload["status_warning"] = state["status_warning"]
        _stamp(payload, tool=tool)
        if is_json_fmt:
            return json.dumps(payload, ensure_ascii=False, indent=2), {}
        return (
            f"ERROR {tool}: {error} "
            f"(local={state.get('local_revision')}, "
            f"accepted={state.get('accepted_revision')}, "
            f"indexed={state.get('indexed_revision')}, "
            f"required={state.get('required_revision')})"
        ), {}

    def _request_with_freshness_headers(freshness_meta: Dict[str, Any]):
        async def _wrapped(method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
            required = freshness_meta.get("required_revision")
            if required:
                headers = dict(kwargs.pop("headers", {}) or {})
                headers.setdefault("X-Omnicode-Min-Revision", str(required))
                kwargs["headers"] = headers
            result = await make_request(method, endpoint, **kwargs)
            return result if isinstance(result, dict) else {"result": result}

        return _wrapped

    @mcp.tool()
    async def omni_search(
        query: str,
        mode: str = "auto",
        file_pattern: Optional[str] = None,
        max_results: int = 10,
        rerank: bool = True,
        token_budget: int = 0,
        around_file: Optional[str] = None,
        flat: bool = False,
        format: str = "text",
    ) -> str:
        """Search the codebase with adaptive mode selection.

        Modes:
          - auto:       rule-based pick across {symbol, text, hybrid, semantic}
          - hybrid:     run symbol + semantic in parallel, fuse with RRF
          - semantic:   natural language → code (FAISS bi-encoder + optional rerank)
          - symbol:     fuzzy symbol-name matching across functions/classes/methods
          - text:       line-level grep (returns real line numbers + ±2 lines context)
          - references: find definitions and usages of a symbol. Uses LSP
                        when available; falls back to AST symbol index and
                        text grep when LSP is unavailable. Each result
                        includes ``source`` and ``confidence``.

        Other parameters:
          - file_pattern:  comma-separated globs ("*.py,*.md"); applies to text mode
                           (and is forwarded as a filter where supported).
          - rerank:        request cross-encoder rerank for semantic/hybrid (W2-9).
                           Effective only when ``OMNICODE_RERANKER=true`` in the
                           server env; otherwise the parameter is a no-op.
          - token_budget:  if > 0, trim the rendered output to roughly this many
                           tokens by dropping snippets/context first.
          - around_file:   bias results toward this file's neighbourhood (callgraph
                           hops + same directory). Other files still appear, just
                           lower in rank.
          - flat:          when true, disable adjacent-line merging in text mode.
                           Each matching line becomes its own result. Useful when
                           the AI wants to post-process every hit individually.
          - format:        ``"text"`` (default) renders the human-readable layout;
                           ``"json"`` returns a JSON array of
                           ``{file, line, symbol, kind, score, why_matched, snippet}``
                           rows so AI editors can parse the result programmatically.

        Returns plain text in a structured layout: per-result file + line, kind,
        score, why_matched tags, and (when budget allows) ±2 lines of code.

        On 0 hits the tool suggests alternative modes and broader queries.
        """
        try:
            fmt = (format or "text").lower()
            resolved_mode = _detect_mode(query) if mode == "auto" else mode
            search_plan = build_search_plan(
                query=query,
                requested_mode=mode,
                resolved_mode=resolved_mode,
                freshness_required=False,
            )

            # Validate the resolved mode up front so an illegal value gets the
            # same structured treatment in both formats. ``auto`` always
            # resolves to a member of this set, so we only need to guard the
            # caller-supplied modes.
            if resolved_mode not in _SEARCH_VALID_MODES:
                if fmt == "json":
                    payload = {
                        "ok": False,
                        "query": query,
                        "requested_mode": mode,
                        "error": f"Unknown search mode: {mode}.",
                        "valid_modes": list(_SEARCH_VALID_MODES),
                        "next_actions": [
                            "Retry with mode='auto' for adaptive routing.",
                            "Use mode='symbol' for code identifiers.",
                            "Use mode='references' for line-level references.",
                        ],
                    }
                    _stamp(payload, tool="omni_search")
                    return json.dumps(payload, ensure_ascii=False, default=str)
                return (
                    f"❌ Unknown search mode: {mode}.\n"
                    f"   Use one of: {', '.join(_SEARCH_VALID_MODES)}"
                )

            # Strip a single matching pair of outer quotes for backends that
            # treat ``"foo"`` as a literal — text/symbol/semantic all want the
            # bare phrase. We keep the original ``query`` for rendering so the
            # user still sees their literal in the header.
            effective_query = _strip_quotes(query.strip()).strip() if query else query

            freshness_block, freshness_meta = await _analysis_freshness_gate(
                tool="omni_search",
                fmt=fmt,
                allow_exact=resolved_mode in {"symbol", "text"},
            )
            if freshness_block is not None:
                return freshness_block
            analysis_request = _request_with_freshness_headers(freshness_meta)
            search_plan = build_search_plan(
                query=query,
                requested_mode=mode,
                resolved_mode=resolved_mode,
                freshness_required=bool(freshness_meta),
            )
            runtime_capabilities = _runtime_capability_registry_snapshot()
            preflight_fallbacks = list(search_plan.fallback_capabilities)
            if resolved_mode == "semantic":
                preflight_fallbacks = []
            capability_preflight = _capability_preflight_payload(
                runtime_capabilities,
                required=list(search_plan.required_capabilities),
                fallbacks=preflight_fallbacks,
            )
            if resolved_mode == "semantic":
                import os as _os

                backend_url = (
                    _os.environ.get("OMNICODE_REMOTE")
                    or _os.environ.get("OMNICODE_FASTAPI_BASE_URL")
                    or ""
                )
                semantic_state = str(
                    (
                        capability_preflight.get("states", {})
                        .get("search.semantic", {})
                        .get("state")
                    )
                    or "unavailable"
                )
                if not backend_url and not freshness_meta and semantic_state in {
                    "unavailable",
                    "unsupported",
                }:
                    payload = {
                        "ok": False,
                        "query": query,
                        "requested_mode": mode,
                        "resolved_mode": resolved_mode,
                        "error_code": "SEMANTIC_INDEX_NOT_READY",
                        "error": (
                            "Semantic search is unavailable in local mode: "
                            "the embedding model or semantic index is not ready."
                        ),
                        "provider": "semantic_vector",
                        "provider_unavailable": True,
                        "empty_reason": "provider_unavailable",
                        "query_plan": search_plan.to_dict(),
                        "capability_preflight": capability_preflight,
                        "capabilities_used": [],
                        "capabilities_missing": ["search.semantic"],
                        "fallback_used": False,
                        "warnings": [
                            "semantic search is optional and not part of the "
                            "default exact-search gate"
                        ],
                        "next_actions": [
                            "omni_index(action='bootstrap', scope='semantic', "
                            "background=True, format='json') to build semantic vectors.",
                            "omnicode models status to inspect the configured embedding model.",
                            "omni_search(query='<identifier or literal>', mode='auto', "
                            "format='json') to use deterministic symbol/text search.",
                        ],
                    }
                    _stamp(payload, tool="omni_search")
                    if fmt == "json":
                        return json.dumps(payload, ensure_ascii=False, default=str)
                    return (
                        "Semantic search unavailable: run omni_index(scope='semantic') "
                        "or use deterministic symbol/text search."
                    )

            # references-mode probe metadata; populated only when that
            # branch runs. Pre-declared so the JSON envelope assembly
            # below can reference it without a NameError.
            _references_meta: Dict[str, Any] = {}
            _text_meta: Dict[str, Any] = {}

            if resolved_mode == "hybrid":
                results, total = await _run_hybrid(
                    analysis_request, effective_query, file_pattern, max_results, rerank
                )
            elif resolved_mode == "semantic":
                results, total = await _run_semantic(
                    analysis_request, effective_query, file_pattern, max_results, rerank
                )
            elif resolved_mode == "symbol":
                results, total = await _run_symbol(
                    analysis_request, effective_query, file_pattern, max_results
                )
            elif resolved_mode == "text":
                results, total = await _run_text(
                    analysis_request, effective_query, file_pattern, max_results,
                    flat=flat,
                    meta_out=_text_meta,
                )
            elif resolved_mode == "references":
                results, total, _ref_meta = await _run_references(
                    analysis_request, effective_query, max_results
                )
                # Stash for the JSON envelope below.
                _references_meta = _ref_meta
            else:  # pragma: no cover - guarded by the up-front mode check
                results, total = [], 0

            # Bias results toward `around_file`'s neighbourhood when asked.
            if around_file and results:
                results = _rerank_by_proximity(results, around_file)

            if fmt == "json":
                # Build the standard envelope.
                structured = [_to_structured(r) for r in results[:max_results]]

                # Stamp source/confidence for non-references modes.
                # References rows are self-tagged inside _run_references,
                # so we leave them alone to preserve the LSP-vs-fallback
                # honesty contract.
                if resolved_mode != "references":
                    for orig, out in zip(results[:max_results], structured, strict=False):
                        # _to_structured already copies source/confidence
                        # if the backend filled them — only synthesize when
                        # they are blank, so a backend that does its own
                        # tagging (e.g. future LSP-aware text search) wins.
                        if not out.get("source") or not out.get("confidence"):
                            src, conf = _infer_source_confidence(
                                orig, resolved_mode, rerank=rerank
                            )
                            out["source"] = out.get("source") or src
                            out["confidence"] = out.get("confidence") or conf
                payload = {
                    "ok": True,
                    "query": query,
                    "requested_mode": mode,
                    "resolved_mode": resolved_mode,
                    "query_plan": search_plan.to_dict(
                        providers=(
                            _text_meta.get("provider_chain")
                            if resolved_mode == "text"
                            else None
                        )
                    ),
                    "capability_preflight": capability_preflight,
                    "total": total,
                    "count": min(len(results), max_results),
                    "results": structured,
                }
                if freshness_meta:
                    payload.update(freshness_meta)
                if resolved_mode == "text" and _text_meta:
                    payload["provider"] = _text_meta.get("provider")
                    payload["provider_chain"] = (
                        _text_meta.get("provider_chain") or []
                    )
                    payload["capabilities_used"] = [
                        str(p) for p in (_text_meta.get("provider_chain") or [])
                    ]
                    payload["capabilities_missing"] = []
                    if not _text_meta.get("line_fts_available"):
                        payload["capabilities_missing"].append(
                            "search.text_exact.line_fts"
                        )
                    payload["exact_index_used"] = bool(
                        _text_meta.get("exact_index_used")
                    )
                    payload["line_fts_available"] = bool(
                        _text_meta.get("line_fts_available")
                        or _text_meta.get("exact_line_fts_available")
                    )
                    if _text_meta.get("line_fts_reason"):
                        payload["line_fts_reason"] = _text_meta["line_fts_reason"]
                    payload["fallback_used"] = bool(
                        _text_meta.get("fallback_used")
                    )
                    if _text_meta.get("fallback_reason"):
                        payload["fallback_reason"] = _text_meta["fallback_reason"]
                    payload["warnings"] = list(_text_meta.get("warnings") or [])
                    if _text_meta.get("empty_reason"):
                        payload["empty_reason"] = _text_meta["empty_reason"]

                # For references mode, also emit the LSP-shaped contract:
                # ``definition`` + ``references[]`` + ``source`` +
                # ``confidence`` + ``ambiguous`` flag. Audit minimum spec.
                if resolved_mode == "references":
                    defs = [r for r in structured if r.get("kind") == "definition"]
                    usages = [r for r in structured if r.get("kind") != "definition"]
                    sources = {r.get("source") for r in structured if r.get("source")}
                    confidences: List[str] = [
                        str(r.get("confidence") or "")
                        for r in structured
                        if r.get("confidence")
                    ]
                    # high > medium > low; pick the best we have.
                    rank = {"high": 3, "medium": 2, "low": 1, "": 0}
                    overall_conf = max(
                        confidences, key=lambda c: rank.get(c, 0), default=""
                    )
                    payload["definition"] = defs[0] if defs else None
                    payload["definition_candidates"] = defs if len(defs) > 1 else []
                    payload["references"] = usages
                    payload["source"] = (
                        next(iter(sources)) if len(sources) == 1
                        else ("mixed" if sources else "")
                    )
                    payload["confidence"] = overall_conf
                    payload["ambiguous"] = len(defs) > 1
                    # ---- audit-bundle.r16 (P3-B): LSP probe transparency.
                    # Surface what was tried and why we fell back, so AI
                    # editors don't mistake "text_grep low" for "LSP not
                    # tried" — we *did* try LSP.
                    if _references_meta:
                        payload["lsp_attempted"] = bool(
                            _references_meta.get("lsp_attempted")
                        )
                        payload["lsp_available"] = bool(
                            _references_meta.get("lsp_available")
                        )
                        payload["lsp_returned_refs"] = bool(
                            _references_meta.get("lsp_returned_refs")
                        )
                        payload["fallback_used"] = (
                            _references_meta.get("fallback_used") or "none"
                        )
                        if _references_meta.get("fallback_reason"):
                            payload["fallback_reason"] = (
                                _references_meta["fallback_reason"]
                            )
                    if not structured:
                        payload["note"] = (
                            "No exact-match definition found. "
                            "For fuzzy matches, retry with mode=symbol."
                        )

                if not structured:
                    index_status = _local_exact_index_status_payload()
                    deterministic_mode = resolved_mode in {"symbol", "text"}
                    if (
                        deterministic_mode
                        and _local_index_required_for_search()
                        and not bool(index_status.get("ready"))
                    ):
                        payload["ok"] = False
                        payload["error_code"] = "INDEX_NOT_READY"
                        payload["error"] = (
                            "Local exact index is not ready; deterministic "
                            f"{resolved_mode} search cannot be trusted yet."
                        )
                        payload["empty_reason"] = "index_not_ready"
                        payload["local_index"] = index_status
                        payload["capabilities_missing"] = sorted(set(
                            list(payload.get("capabilities_missing") or [])
                            + [
                                "search.symbol_exact"
                                if resolved_mode == "symbol"
                                else "search.text_exact"
                            ]
                        ))
                        payload["next_actions"] = [
                            "omni_index(action='bootstrap', scope='workspace', "
                            "mode='fast', format='json') to build the local "
                            "files/lines/symbols index.",
                            "omni_read(file='<known file>', mode='outline', "
                            "format='json') if you already know the target file.",
                        ]
                    else:
                        payload.setdefault("empty_reason", "true_empty")

                # ---- audit-bundle.r18 (P2): next_actions for the
                # search success path. Pre-r18 the JSON envelope had no
                # ``next_actions`` — Round 9 found that the only tool
                # in the eight-tool surface without a follow-up nudge.
                # We branch on the resolved mode and the quality of the
                # top-scoring hit so the AI editor sees the most useful
                # next move first.
                top_row = structured[0] if structured else {}
                top_conf = (top_row.get("confidence") or "").lower()
                top_symbol = top_row.get("symbol") or ""
                top_file = top_row.get("file") or ""
                fuzzy_count = sum(
                    1 for r in structured
                    if "fuzzy" in (r.get("source") or "").lower()
                )
                next_actions: List[str] = []
                if resolved_mode == "references":
                    # references-mode owns its own next-step shape via
                    # the ``definition``+``references`` contract.
                    if structured:
                        next_actions.append(
                            "omni_read(file='%s', mode='symbol', symbol='%s', "
                            "format='json') to inspect the definition body."
                            % (top_file, top_symbol or query)
                        )
                        next_actions.append(
                            "omni_impact(symbol='%s', format='json') to check "
                            "the blast radius before editing."
                            % (top_symbol or query)
                        )
                    else:
                        next_actions.append(
                            "omni_search(query='%s', mode='symbol', "
                            "format='json') if you want a fuzzy lookup "
                            "instead." % query
                        )
                elif resolved_mode == "symbol":
                    if top_conf == "high" and top_symbol:
                        next_actions.append(
                            "omni_read(file='%s', mode='symbol', symbol='%s', "
                            "format='json') to read the definition body."
                            % (top_file, top_symbol)
                        )
                        next_actions.append(
                            "omni_impact(symbol='%s', format='json') for the "
                            "blast radius." % top_symbol
                        )
                        next_actions.append(
                            "omni_search(query='%s', mode='references', "
                            "format='json') for line-level callsites."
                            % top_symbol
                        )
                    elif fuzzy_count and structured:
                        # Mostly fuzzy hits → caller probably mis-typed
                        # the symbol. Suggest references / text fallback.
                        next_actions.append(
                            "Top hits are fuzzy matches; if you meant the "
                            "exact identifier '%s', try omni_search(query="
                            "'%s', mode='references', format='json') for an "
                            "exact-name resolver." % (query, query)
                        )
                        next_actions.append(
                            "omni_search(query='%s', mode='text', "
                            "format='json') for a free-text scan instead."
                            % query
                        )
                    else:
                        next_actions.append(
                            "omni_search(query='%s', mode='hybrid', "
                            "format='json') to widen recall." % query
                        )
                elif resolved_mode == "text":
                    if structured:
                        next_actions.append(
                            "omni_read(file='%s', mode='range', "
                            "start_line=%s, format='json') to read context "
                            "around the first hit."
                            % (top_file, top_row.get("line") or 1)
                        )
                        next_actions.append(
                            "omni_search(query='%s', mode='symbol', "
                            "format='json') if you want a symbol-anchored "
                            "result instead." % query
                        )
                    else:
                        next_actions.append(
                            "omni_search(query='%s', mode='semantic', "
                            "format='json') to broaden via embeddings."
                            % query
                        )
                elif resolved_mode == "semantic":
                    if structured:
                        next_actions.append(
                            "omni_read(file='%s', mode='symbol', "
                            "symbol='%s', format='json') to drill into the "
                            "top hit." % (top_file, top_symbol or query)
                        )
                        if top_symbol:
                            next_actions.append(
                                "omni_impact(symbol='%s', format='json') for "
                                "the blast radius." % top_symbol
                            )
                    next_actions.append(
                        "omni_search(query='%s', mode='hybrid', "
                        "format='json') to fuse symbol + semantic recall."
                        % query
                    )
                elif resolved_mode == "hybrid":
                    if structured:
                        next_actions.append(
                            "omni_read(file='%s', mode='symbol', "
                            "symbol='%s', format='json') to drill into the "
                            "top RRF-fused hit." % (top_file, top_symbol or query)
                        )
                # When the result list looks "fuzzy-only" regardless of
                # mode, surface a recovery hint.
                if structured and fuzzy_count == len(structured) and resolved_mode != "symbol":
                    next_actions.append(
                        "All hits are fuzzy; consider omni_search(query='%s', "
                        "mode='references', format='json') for an exact "
                        "lookup." % query
                    )
                # Always recommend the memory advisory for downstream
                # context, except when the response is empty.
                if structured and top_symbol and resolved_mode != "references":
                    next_actions.append(
                        "omni_memory(action='advisory', symbol='%s', "
                        "format='json') for prior lessons."
                        % top_symbol
                    )
                if next_actions:
                    existing_actions = payload.get("next_actions")
                    if isinstance(existing_actions, list):
                        merged_actions = list(existing_actions)
                        for action_item in next_actions:
                            if action_item not in merged_actions:
                                merged_actions.append(action_item)
                        payload["next_actions"] = merged_actions
                    else:
                        payload["next_actions"] = next_actions

                # ---- audit-bundle.r17 (P1): token budget honesty.
                # Pre-r17 the JSON path silently ignored ``token_budget``
                # — it only affected text rendering. AI editors had no
                # signal that a 20-result response was about to blow
                # their budget. Now every JSON envelope carries:
                #
                #   * token_estimate — _approx_token_count of the
                #                      currently-assembled payload.
                #   * truncated      — True iff token_budget > 0 and
                #                      we trimmed any results to fit
                #                      (NOT the post-trim estimate
                #                      vs budget — once we trim, the
                #                      response IS truncated even if
                #                      the new estimate fits).
                #
                # When truncation is needed AND ``token_budget`` is set,
                # we trim ``results`` (and the references-mode
                # ``references[]`` mirror) from the lowest-relevance
                # tail until we fit. The ``definition`` row is never
                # dropped — losing the anchor would leave the caller
                # without a usable result.
                est_text = json.dumps(payload, ensure_ascii=False, default=str)
                token_estimate = _approx_token_count(est_text)
                truncation_reasons: List[str] = []
                was_truncated = False
                if token_budget and token_estimate > token_budget:
                    # Trim from the tail of ``results`` (already sorted
                    # by relevance) until we fit or hit the floor (1).
                    raw_results = payload.get("results") or []
                    results_list: List[Any] = (
                        list(raw_results)
                        if isinstance(raw_results, list)
                        else []
                    )
                    original_count = len(results_list)
                    while (
                        token_estimate > token_budget
                        and len(results_list) > 1
                    ):
                        results_list.pop()
                        payload["results"] = results_list
                        # Keep the references mirror in sync if present.
                        if resolved_mode == "references":
                            raw_refs = payload.get("references") or []
                            refs: List[Any] = (
                                list(raw_refs)
                                if isinstance(raw_refs, list)
                                else []
                            )
                            if refs:
                                refs.pop()
                                payload["references"] = refs
                        # Re-estimate after each drop.
                        est_text = json.dumps(
                            payload, ensure_ascii=False, default=str,
                        )
                        token_estimate = _approx_token_count(est_text)
                    dropped = original_count - len(results_list)
                    if dropped > 0:
                        was_truncated = True
                        truncation_reasons.append(
                            f"results_capped:{len(results_list)} of "
                            f"{original_count} (token_budget={token_budget})"
                        )
                        payload["count"] = len(results_list)
                        # Honest count reflects what's actually returned;
                        # ``total`` keeps the backend total for context.
                        existing_actions = payload.get("next_actions", [])
                        payload["next_actions"] = [
                            f"Re-run with token_budget={token_budget * 2} "
                            "or max_results=<smaller> to avoid trimming.",
                        ] + (existing_actions if isinstance(existing_actions, list) else [])
                payload["token_estimate"] = token_estimate
                # ``truncated`` reflects whether we actually trimmed —
                # not the post-trim fit, which would lie about it.
                payload["truncated"] = bool(
                    was_truncated
                    or (token_budget and token_estimate > token_budget)
                )
                if token_budget:
                    payload["token_budget"] = token_budget
                if truncation_reasons:
                    payload["truncation_reasons"] = truncation_reasons

                _stamp(payload, tool="omni_search")
                return json.dumps(payload, ensure_ascii=False, default=str)

            if not results:
                return _no_results_message(query, resolved_mode, mode)

            return _render_results(
                query=query,
                requested_mode=mode,
                resolved_mode=resolved_mode,
                results=results[:max_results],
                total=total,
                token_budget=token_budget,
            )

        except Exception as e:
            if (format or "text").lower() == "json":
                message = _sanitize_error_text(str(e) or e.__class__.__name__)
                lowered = message.lower()
                cloud_unavailable = any(
                    marker in lowered
                    for marker in (
                        "timed out",
                        "connection refused",
                        "failed to establish",
                        "urlopen error",
                        "cloud unavailable",
                        "remote end closed",
                    )
                )
                resolved = _detect_mode(query) if mode == "auto" else mode
                payload = {
                    "ok": False,
                    "query": query,
                    "requested_mode": mode,
                    "resolved_mode": resolved,
                    "error_code": (
                        "CLOUD_UNAVAILABLE"
                        if cloud_unavailable
                        else "SEARCH_FAILED"
                    ),
                    "error": f"Search failed: {message}",
                    "freshness": (
                        "unavailable" if cloud_unavailable else "unknown"
                    ),
                    "empty_reason": "provider_unavailable",
                    "provider_unavailable": True,
                    "cloud_available": not cloud_unavailable,
                    "warnings": (
                        [
                            "Cloud search backend is unavailable; local read "
                            "and safe patch tools may still be usable."
                        ]
                        if cloud_unavailable
                        else []
                    ),
                    "next_actions": [
                        "omni_status(format='json') to inspect backend availability and capability status.",
                        "omni_read(file='<known file>', mode='outline', format='json') if you already know the target file.",
                    ],
                }
                try:
                    payload["query_plan"] = build_search_plan(
                        query=query,
                        requested_mode=mode,
                        resolved_mode=resolved,
                    ).to_dict()
                except Exception:
                    pass
                _stamp(payload, tool="omni_search")
                return json.dumps(payload, ensure_ascii=False, default=str)
            return f"Search failed: {e}"

    @mcp.tool()
    async def omni_read(
        file: str,
        mode: str = "outline",
        symbol: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        query: Optional[str] = None,
        format: str = "json",
        max_tokens: int = 8000,
    ) -> str:
        """Read a file with token-efficient mode selection.

        Modes:
          - outline:          signatures + first docstring line (~90% token savings)
          - symbols:          structured symbol list (name, kind, lines) — no code
          - full:             complete file content (auto-truncated above ``max_tokens``)
          - imports:          import / require / use lines (AST-driven where possible)
          - diagnostics:      lint / type / static-analysis issues for this file
          - range:            slice ``[start_line .. end_line]`` (start_line REQUIRED)
          - symbol:           read a specific symbol by name
          - relevant_chunks:  semantic top-K chunks of THIS file vs ``query`` —
                              "RAG inside one file" (``query`` REQUIRED)

        Args:
            file:        workspace-relative path to the file.
            mode:        one of the modes above. Default ``outline``.
            symbol:      symbol name (required for ``mode=symbol``).
            start_line:  1-indexed start line (required for ``mode=range``).
            end_line:    inclusive end line; defaults to ``start_line + 50``.
            query:       free-text query (required for ``mode=relevant_chunks``).
            format:      ``json`` (default, structured, agent-friendly) or
                         ``text`` (human-readable rendering).
            max_tokens:  soft budget for ``mode=full``. When exceeded the
                         response is truncated and ``truncated=true`` is set.

        Returns:
            JSON string with at least::

                {"file", "mode", "language", "total_lines", "symbols",
                 "content", "token_estimate", "truncated", ...}

            When ``format="text"`` the same data is rendered as a human-readable
            block.  Errors come back as ``{"error": "...", "mode": ...}``.
        """
        try:
            mode_norm = (mode or "outline").lower().strip()

            # ---- audit-bundle.r10: reject illegal modes up front with a
            # stamped envelope (read.valid_modes_envelope). Mirrors the
            # omni_search illegal-mode contract so AI editors see a
            # consistent shape.
            if mode_norm not in _READ_VALID_MODES:
                return _read_illegal_mode_envelope(
                    file=file, requested_mode=mode, fmt=format,
                )

            # ---- range guard: refuse to silently swallow missing start_line.
            if mode_norm == "range" and start_line is None:
                return _emit_read_error(
                    file=file,
                    mode="range",
                    error=(
                        "mode=range requires start_line (1-indexed). "
                        "Pass start_line and optionally end_line; "
                        "or use mode=full / mode=outline instead."
                    ),
                    fmt=format,
                )

            # ---- symbol mode: refuse without the symbol name.
            if mode_norm == "symbol" and not symbol:
                return _emit_read_error(
                    file=file,
                    mode="symbol",
                    error="mode=symbol requires the `symbol` argument.",
                    fmt=format,
                )

            # ---- relevant_chunks: refuse without the query.
            if mode_norm == "relevant_chunks" and not (query and query.strip()):
                return _emit_read_error(
                    file=file,
                    mode="relevant_chunks",
                    error=(
                        "mode=relevant_chunks requires `query` "
                        "(free-text search scoped to this file)."
                    ),
                    fmt=format,
                )

            params: Dict[str, Any] = {"file_path": file, "with_line_numbers": True}

            # ---- diagnostics: delegate to the shared service so the
            # envelope (counts/total_count/severity_filter/sources/
            # tools_run/tools_skipped/truncated) matches omni_diagnostics
            # exactly. omni_read keeps the read-tool conventions on top
            # (mode/language/total_lines/next_actions).
            if mode_norm == "diagnostics":
                diag_payload = await _collect_diagnostics_payload(
                    file=file, severity="all", sources="guard,lsp",
                )
                if not diag_payload.get("ok", False):
                    return _emit_read_error(
                        file=file,
                        mode="diagnostics",
                        error=str(diag_payload.get("error") or "diagnostics failed"),
                        fmt=format,
                    )

                # Build a synthetic backend response that _build_read_payload
                # can consume verbatim.
                synthetic = {
                    "language": "",
                    "total_lines": 0,
                    "diagnostics": diag_payload["diagnostics"],
                    "diagnostic_count": diag_payload["total_count"],
                    "counts": diag_payload["counts"],
                    "total_count": diag_payload["total_count"],
                    "severity_filter": diag_payload["severity_filter"],
                    "sources": diag_payload["sources"],
                    "tools_run": diag_payload["tools_run"],
                    "tools_skipped": diag_payload["tools_skipped"],
                }
                payload = _build_read_payload(
                    file=file,
                    requested_mode="diagnostics",
                    data=synthetic,
                    start_line=None,
                    end_line=None,
                    symbol=None,
                    query=None,
                    max_tokens=max_tokens,
                )
                # Honour the diagnostics-truncated flag from the shared
                # service rather than inheriting the read-tool's
                # token-budget truncation.
                payload["truncated"] = diag_payload.get("truncated", False)
                if (format or "json").lower() == "text":
                    return _render_read_payload_text(payload)
                return json.dumps(payload, ensure_ascii=False, indent=2)

            local_payload = _build_local_read_payload(
                file=file,
                mode=mode_norm,
                symbol=symbol,
                start_line=start_line,
                end_line=end_line,
                max_tokens=max_tokens,
            )
            if local_payload is not None:
                if (format or "json").lower() == "text":
                    return _render_read_payload_text(local_payload)
                return json.dumps(local_payload, ensure_ascii=False, indent=2)
            if mode_norm in {"full", "range", "symbol", "outline", "symbols", "imports"}:
                try:
                    import os as _os

                    executor_mode = (
                        _os.environ.get("OMNICODE_EXECUTOR_MODE")
                        or _os.environ.get("OMNICODE_EXECUTOR")
                        or "local"
                    ).strip().lower()
                    local_path = _resolve_workspace_path(file)
                except (ValueError, OSError):
                    executor_mode = ""
                    local_path = None
                if executor_mode == "hybrid" and local_path is not None and not local_path.is_file():
                    return _emit_read_error(
                        file=file,
                        mode=mode_norm,
                        error=f"File not found: {file}",
                        fmt=format,
                    )

            if mode_norm == "range":
                # ``start_line`` is guaranteed non-None at this point because
                # the guard above returns early when it's missing. Cast for
                # mypy: it sees Optional[int] from the function signature.
                assert start_line is not None
                params["start_line"] = start_line
                params["end_line"] = end_line if end_line is not None else (start_line + 50)
                params["mode"] = "full"
            elif mode_norm == "symbol":
                params["symbol_name"] = symbol
                params["mode"] = "full"
            else:
                params["mode"] = mode_norm
                if mode_norm == "relevant_chunks" and query:
                    params["query"] = query.strip()

            result = await make_request("POST", "/read", params=params)

            if "error" in result:
                return _emit_read_error(
                    file=file,
                    mode=mode_norm,
                    error=str(result["error"]),
                    fmt=format,
                )

            data = result.get("result", result) or {}

            # Backend can return ``success=false`` inside the result envelope
            # (e.g. file not found / start_line out of range).
            if isinstance(data, dict) and data.get("success") is False:
                return _emit_read_error(
                    file=file,
                    mode=mode_norm,
                    error=str(data.get("error") or "Unknown read error"),
                    fmt=format,
                )

            payload = _build_read_payload(
                file=file,
                requested_mode=mode_norm,
                data=data,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
                query=query,
                max_tokens=max_tokens,
            )

            if (format or "json").lower() == "text":
                return _render_read_payload_text(payload)
            return json.dumps(payload, ensure_ascii=False, indent=2)

        except Exception as e:
            return _emit_read_error(
                file=file, mode=mode, error=f"omni_read failed: {e}", fmt=format,
            )

    @mcp.tool()
    async def omni_impact(
        symbol: str,
        depth: int = 2,
        max_files: int = 200,
        format: str = "json",
    ) -> str:
        """Assess the blast radius of changing a symbol — required reading
        before any non-trivial edit.

        Returns:
          • risk level (low / medium / high / unknown) with the reasons,
          • direct callers and callees,
          • files affected,
          • recommended tests to run after the change.

        Note on semantics:
          ``callers`` and ``callees`` are graph-level function
          relationships derived from the static call graph. For
          line-level or textual references (including string-literal
          mentions and dynamic dispatch sites) use
          ``omni_search(mode='references')`` instead.

        Edge cases:
          * Empty / whitespace ``symbol`` → structured ``ok=false``
            error with a hint to call ``omni_search(mode='symbol')``
            first; no fake test list is returned.
          * Symbol not found in the call graph → ``risk='unknown'``,
            ``confidence='low'``, empty caller/callee lists, and a
            note explaining why. We never inherit ``risk='low'`` from
            "no test coverage" for a symbol that isn't in the graph.
          * ``max_files`` smaller than the backend minimum is clamped
            up for the request and the response is truncated back to
            the caller's value with ``truncated=true`` plus a note —
            the public surface never leaks an HTTP 422.

        Combines /graph/impact + /graph/risk + /graph/related-tests in
        parallel so the AI gets one consolidated payload.
        """
        # ---- Empty-symbol guard --------------------------------------
        # Reject empty / whitespace-only symbol with a structured error
        # rather than letting the backend swallow it silently and
        # returning a default test list that has nothing to do with the
        # caller's intent. The error envelope still ships the version
        # stamp via _stamp(...).
        if not symbol or not symbol.strip():
            err = {
                "ok": False,
                "error": "omni_impact requires a non-empty symbol name.",
                "symbol": symbol or "",
                "risk": "unknown",
                "risk_reasons": [],
                "callers": [],
                "caller_count": 0,
                "callees": [],
                "callee_count": 0,
                "files_count": 0,
                "files_involved": [],
                "suggested_tests": [],
                "suggested_commands": [],
                "source": "graph",
                "confidence": "low",
                "suggested_next_action": (
                    "Use omni_search(mode='symbol') first to find a symbol, "
                    "then pass its exact name to omni_impact."
                ),
                "symbol_resolution": "n/a",
                "next_actions": [
                    "omni_search(mode='symbol', query='<partial_name>', format='json')",
                    "omni_impact(symbol='<exact name>', format='json')",
                ],
            }
            _stamp(err, tool="omni_impact")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() != "text"
                else f"❌ {err['error']}"
            )

        fmt = (format or "json").lower()
        freshness_block, freshness_meta = await _analysis_freshness_gate(
            tool="omni_impact",
            fmt=fmt,
            allow_exact=True,
        )
        if freshness_block is not None:
            return freshness_block
        analysis_request = _request_with_freshness_headers(freshness_meta)

        try:
            import asyncio

            # Always send the backend a value above its rejection floor.
            # HTTP 422 used to leak through as ``note='HTTP 422'`` for
            # callers passing tiny max_files; clamp the *request* and
            # truncate the *response* so the public surface stays clean.
            #
            # Floor is set to the omni_impact default (200) rather than
            # the backend's hard rejection threshold so the graph has
            # enough headroom to return a meaningful blast radius for
            # high-fan-out symbols. Without this, max_files=5 against a
            # 96-file symbol would have the backend return zero files
            # (because graph traversal cuts at max_files), defeating the
            # MCP-layer truncation branch defined in the impact.v2
            # contract. Same constant on both sides so the contract is
            # implemented end-to-end.
            _DEFAULT_IMPACT_MAX_FILES = 200
            _MIN_BACKEND_MAX_FILES = _DEFAULT_IMPACT_MAX_FILES
            backend_max_files = max(max_files, _MIN_BACKEND_MAX_FILES)

            params = {"symbol": symbol, "depth": depth, "max_files": backend_max_files}
            risk_task = analysis_request("GET", "/graph/risk", params={
                "symbol": symbol, "max_files": backend_max_files,
            })
            impact_task = analysis_request("GET", "/graph/impact", params=params)
            tests_task = analysis_request("GET", "/graph/related-tests", params={
                "symbol": symbol, "max_files": backend_max_files,
            })
            gathered: List[Any] = list(await asyncio.gather(
                risk_task, impact_task, tests_task, return_exceptions=True,
            ))
            risk_raw, impact_raw, tests_raw = gathered

            def _safe(r: Any) -> Dict[str, Any]:
                if isinstance(r, Exception):
                    return {"error": str(r)}
                if isinstance(r, dict):
                    inner = r.get("result", r)
                    if isinstance(inner, dict):
                        return inner
                return {}

            risk: Dict[str, Any] = _safe(risk_raw)
            impact: Dict[str, Any] = _safe(impact_raw)
            tests: Dict[str, Any] = _safe(tests_raw)

            risk_level = risk.get("risk", "unknown")
            risk_reasons = risk.get("reasons", []) or []

            # Backend uses ``affected_symbols`` / ``dependent_symbols``
            # (note the trailing ``_symbols``); legacy keys ``affected`` /
            # ``dependents`` are checked for forward-compat.
            callees = (
                impact.get("affected_symbols")
                or impact.get("affected")
                or []
            )
            callers = (
                impact.get("dependent_symbols")
                or impact.get("dependents")
                or []
            )
            files_count = impact.get("files_count", 0) or 0
            files_involved = impact.get("files_involved", []) or []

            test_files = tests.get("test_files", []) or []
            suggested_cmds = tests.get("suggested_commands", []) or []

            # Heuristic confidence + risk override for missing symbols.
            #
            # When the call graph yielded nothing, we MUST NOT inherit
            # risk='low' from the /graph/risk endpoint (which interprets
            # "no test coverage" as "low risk"). A symbol that isn't in
            # the graph is genuinely unknown — pretending otherwise is
            # the dishonesty the audit flagged. ``confidence='low'``
            # plus ``risk='unknown'`` plus the note tells the agent to
            # confirm the symbol exists before trusting anything.
            impact_note: Optional[str] = None
            confidence_caveats: List[str] = []
            symbol_resolution = "found"
            symbol_fallback_hit: Optional[Dict[str, Any]] = None
            if files_count == 0 and not callers and not callees:
                confidence = "low"
                risk_level = "unknown"
                risk_reasons = []
                symbol_hits: List[Dict[str, Any]] = []
                try:
                    symbol_hits, _symbol_total = await _run_symbol(
                        analysis_request, symbol, None, 3
                    )
                except Exception as exc:
                    confidence_caveats.append(
                        "symbol fallback lookup failed: "
                        + _sanitize_error_text(str(exc))
                    )
                for hit in symbol_hits:
                    hit_name = (
                        hit.get("symbol_name")
                        or hit.get("name")
                        or hit.get("symbol")
                        or ""
                    )
                    if hit_name == symbol:
                        symbol_fallback_hit = hit
                        break
                if symbol_fallback_hit is not None:
                    symbol_resolution = "found"
                    graph_problem = (
                        "graph backend returned an error"
                        if "error" in impact
                        else "call graph has no callers/callees for this symbol"
                    )
                    impact_note = (
                        "Symbol exists in a deterministic symbol index, but "
                        f"{graph_problem}. risk='unknown' until references or "
                        "the symbol body are inspected."
                    )
                    confidence_caveats.append(
                        f"{graph_problem}; use omni_search(mode='references') "
                        "for line-level use."
                    )
                else:
                    symbol_resolution = "not_found"
                    if "error" in impact:
                        impact_note = (
                            "Symbol could not be confirmed because graph lookup "
                            f"failed: {_sanitize_error_text(str(impact.get('error')))}"
                        )
                    else:
                        impact_note = (
                            "Symbol not found in call graph or deterministic "
                            "symbol index. risk='unknown' until the symbol is "
                            "confirmed via omni_search(mode='symbol')."
                        )
            else:
                symbol_resolution = "found"
                # ---- audit-bundle.r16 (P3-A): honesty about transitive
                # blast radius and builtin-call noise.
                #
                # Old logic flipped at files_count >= 50, which produced
                # ``confidence=high`` for symbols whose graph was *least*
                # trustworthy: when the graph reports 95 files for a
                # function with 3 direct callers, it has dragged in
                # transitive callsites of helpers like ``q.lower()``,
                # ``q.split()`` etc. and inflated the blast radius far
                # beyond what an AI editor can verify. Honest contract:
                #
                #   * tight + clean graph (<25 files, no builtin callees)
                #     → ``high`` (we trust the graph)
                #   * medium graph or some builtin noise → ``medium``
                #   * wide / noisy graph (>=50 files, OR many builtins)
                #     → ``medium`` with a caveat string, NOT ``high``
                #
                # ``high`` should only be used when the graph result is
                # the right precision for an AI editor to act on without
                # double-checking via omni_search/omni_read.
                callee_names = list(callees) if isinstance(callees, list) else []
                builtin_callees = [
                    name for name in callee_names
                    if name in _PYTHON_BUILTIN_CALLEE_NAMES
                ]
                builtin_share = (
                    len(builtin_callees) / len(callee_names)
                    if callee_names else 0.0
                )
                noisy_callees = (
                    len(builtin_callees) >= 5
                    or (callee_names and builtin_share >= 0.4)
                )
                wide_graph = files_count >= 50
                if wide_graph:
                    confidence_caveats.append(
                        f"transitive blast radius: graph reports {files_count} "
                        f"files affected; many are reached only via shared "
                        "helpers and may not all be impacted by an edit."
                    )
                if noisy_callees:
                    sample = ", ".join(builtin_callees[:5])
                    confidence_caveats.append(
                        f"callees include builtins/method-style calls "
                        f"({sample}{'...' if len(builtin_callees) > 5 else ''}); "
                        "treat callees as approximate, not rename-grade."
                    )
                if wide_graph or noisy_callees:
                    confidence = "medium"
                elif files_count >= 25:
                    confidence = "medium"
                else:
                    confidence = "high"

            # Truncate ``files_involved`` to the caller's max_files. The
            # backend was always asked for >= _MIN_BACKEND_MAX_FILES so
            # files_count remains the true blast-radius size; this just
            # caps the *displayed* file list for tight token budgets.
            files_truncated = files_count > max_files
            displayed_files = files_involved[:max_files]
            if files_truncated:
                trunc_note = (
                    f"files_involved truncated to {max_files} of "
                    f"{files_count} (raise max_files to see more)."
                )
                impact_note = (
                    f"{impact_note}; {trunc_note}" if impact_note else trunc_note
                )

            payload = {
                "ok": True,
                "symbol": symbol,
                "risk": risk_level,
                "risk_reasons": risk_reasons,
                "callers": list(callers),
                "caller_count": len(callers),
                "callees": list(callees),
                "callee_count": len(callees),
                "files_count": files_count,
                "files_involved": displayed_files,
                "truncated": files_truncated,
                "suggested_tests": test_files,
                "suggested_commands": suggested_cmds,
                "source": (
                    "graph+symbol_fallback"
                    if symbol_fallback_hit is not None
                    else "graph"
                ),
                "confidence": confidence,
                # ---- audit-bundle.r15 (P3): symbol_resolution parity
                # with omni_intelligence / omni_context. AI editors can
                # now use a single field across the surface to detect
                # missing symbols.
                "symbol_resolution": symbol_resolution,
            }
            if symbol_fallback_hit is not None:
                payload["symbol_fallback"] = {
                    "name": (
                        symbol_fallback_hit.get("symbol_name")
                        or symbol_fallback_hit.get("name")
                        or symbol
                    ),
                    "file": (
                        symbol_fallback_hit.get("file_path")
                        or symbol_fallback_hit.get("file")
                    ),
                    "line": (
                        symbol_fallback_hit.get("line_start")
                        or symbol_fallback_hit.get("line_number")
                        or symbol_fallback_hit.get("line")
                    ),
                    "kind": symbol_fallback_hit.get("kind"),
                    "source": symbol_fallback_hit.get("source")
                    or "symbol_fallback",
                }
                payload["capabilities_used"] = ["search.symbol_exact"]
                payload["capabilities_missing"] = ["impact.graph"]
                payload["fallback"] = {
                    "reason": "graph_index_unavailable_or_empty",
                    "references": [],
                    "test_candidates": test_files[:5],
                }
            else:
                payload["capabilities_used"] = ["impact.graph"]
                payload["capabilities_missing"] = (
                    ["search.symbol_exact"] if symbol_resolution == "not_found" else []
                )
            # ---- audit-bundle.r16 (P3-A): expose caveats so AI editors
            # can see *why* the confidence band is what it is. Only set
            # the field when we actually have caveats to declare.
            if confidence_caveats:
                payload["confidence_caveats"] = confidence_caveats
            if impact_note:
                payload["note"] = impact_note

            # ---------- audit-bundle.r14 (P2): top-level next_actions ----
            # Every JSON branch now ships a ready-to-run follow-up so AI
            # editors don't have to mine ``suggested_commands`` or guess.
            next_actions: List[str] = []
            if risk_level == "unknown":
                if symbol_resolution == "found":
                    fallback_file = (
                        (symbol_fallback_hit or {}).get("file_path")
                        or (symbol_fallback_hit or {}).get("file")
                        or "<defining file>"
                    )
                    next_actions.append(
                        "omni_read(file='%s', mode='symbol', symbol='%s', "
                        "format='json') to inspect the symbol body."
                        % (fallback_file, symbol)
                    )
                    next_actions.append(
                        "omni_search(query='%s', mode='references', format='json') "
                        "for line-level callsites." % symbol
                    )
                else:
                    next_actions.append(
                        "omni_search(mode='symbol', query='%s', format='json') "
                        "to confirm the symbol exists." % symbol
                    )
                    next_actions.append(
                        "omni_read(file='<defining file>', mode='symbol', "
                        "symbol='%s', format='json') once the symbol is located."
                        % symbol
                    )
            else:
                if suggested_cmds:
                    next_actions.append(
                        "Run the suggested_commands to verify nothing regresses: "
                        + " ; ".join(suggested_cmds[:3])
                    )
                if test_files and not suggested_cmds:
                    next_actions.append(
                        "Run the suggested_tests targeted: pytest "
                        + " ".join(test_files[:3])
                    )
                next_actions.append(
                    "omni_search(query='%s', mode='references', format='json') "
                    "for line-level callsites." % symbol
                )
                next_actions.append(
                    "omni_memory(action='advisory', symbol='%s', format='json') "
                    "to recall prior lessons." % symbol
                )
            if files_truncated:
                next_actions.append(
                    "Re-run with max_files=%d to see the full file list."
                    % max(files_count, max_files * 2)
                )
            payload["next_actions"] = next_actions
            if freshness_meta:
                payload.update(freshness_meta)

            if fmt != "text":
                _stamp(payload, tool="omni_impact")
                return json.dumps(payload, ensure_ascii=False, indent=2)

            # ---------- text rendering (unchanged surface) ------------
            badge = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk_level, "⚪")
            lines = [f"💥 Impact: {symbol}\n"]
            lines.append(f"   {badge} Risk: {risk_level}  (confidence: {confidence})")
            for reason in risk_reasons[:6]:
                lines.append(f"      • {reason}")

            lines.append("")
            lines.append(f"   ⬇️  Callees affected:  {len(callees)}")
            for n in list(callees)[:8]:
                lines.append(f"      → {n}")
            lines.append(f"   ⬆️  Callers depending: {len(callers)}")
            for n in list(callers)[:8]:
                lines.append(f"      ← {n}")
            lines.append(f"   📁  Files in blast radius: {files_count}")
            if files_truncated:
                lines.append(
                    f"      (showing first {len(displayed_files)} of {files_count})"
                )

            if test_files:
                lines.append("")
                lines.append(f"   🧪 Suggested tests ({len(test_files)}):")
                for t in test_files[:6]:
                    lines.append(f"      • {t}")
                for cmd in suggested_cmds[:3]:
                    lines.append(f"      $ {cmd}")
            if impact_note:
                lines.append("")
                lines.append(f"   ⚠️  {impact_note}")
            return "\n".join(lines)

        except Exception as e:
            err = {
                "ok": False,
                "error": f"omni_impact failed: {e}",
                "next_actions": [
                    "omni_status(format='json') to confirm the runtime is healthy.",
                    "Retry omni_impact with format='json' once the underlying "
                    "issue is resolved.",
                ],
            }
            _stamp(err, tool="omni_impact")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() != "text"
                else f"❌ omni_impact failed: {e}"
            )

    @mcp.tool()
    async def omni_diagnostics(
        file: Optional[str] = None,
        severity: str = "all",
        sources: str = "guard,lsp",
        format: str = "json",
    ) -> str:
        """Get structured lint / type / static-analysis diagnostics.

        Parameters:
          - file:     workspace-relative path. If None, returns a summary
                      across the recently-edited files (TBD; for now
                      ``file`` is required).
          - severity: 'all' | 'error' | 'warning'. Default 'all'.
          - sources:  comma-separated list of 'guard,lsp'. Guard runs ruff
                      + mypy + bandit (Python) or eslint + tsc (JS/TS).
                      LSP returns the language-server diagnostics for the
                      file.
          - format:   ``json`` (default) for structured output or
                      ``text`` for a human-readable rendering.

        Returns a structured per-line list so the AI can decide what to
        fix without parsing tool stdout.
        """
        if not file:
            err = {
                "ok": False,
                "error": (
                    "omni_diagnostics requires a file path. "
                    "Workspace-wide aggregation is on the roadmap."
                ),
            }
            _stamp(err, tool="omni_diagnostics")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() != "text"
                else (
                    "❌ omni_diagnostics requires a file path.\n"
                    "   Workspace-wide aggregation is on the roadmap — "
                    "for now scope to a single file."
                )
            )
        try:
            from omnicode_core.capabilities.languages import (
                capabilities_for_path,
                language_for_path,
            )

            local_path = _resolve_workspace_path(file)
            caps = capabilities_for_path(file)
            language = language_for_path(file)
            if local_path.is_file() and caps.diagnostics == "unsupported":
                reason = f"{language}_diagnostics_unsupported"
                payload = {
                    "ok": True,
                    "file": file,
                    "diagnostics": [],
                    "diagnostics_status": "unsupported",
                    "language": language,
                    "severity_filter": severity,
                    "sources": sources,
                    "tools_run": [],
                    "tools_skipped": [reason],
                    "counts": {
                        "error": 0,
                        "warning": 0,
                        "info": 0,
                        "total": 0,
                    },
                    "total_count": 0,
                    "truncated": False,
                    "source": "language_capability_matrix",
                    "confidence": "high",
                    "warnings": [reason],
                    "next_actions": [
                        "Run the project-native compiler/test command for this language.",
                        f"omni_read(file='{file}', mode='outline', format='json') to inspect structure.",
                    ],
                }
                _stamp(payload, tool="omni_diagnostics")
                if (format or "json").lower() != "text":
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                return f"Diagnostics unsupported for {file}: {reason}"
        except ValueError:
            pass
        except Exception:
            pass
        try:
            payload = await _collect_diagnostics_payload(
                file=file, severity=severity, sources=sources,
            )

            if not payload.get("ok", False):
                err_msg = payload.get("error", "diagnostics failed")
                err_msg = _sanitize_error_text(str(err_msg))
                payload["error"] = err_msg
                # audit-bundle.r10 (read.error_next_actions): file-not-found
                # errors carry a recovery next_actions list so the agent
                # can act without parsing the message string.
                if "next_actions" not in payload:
                    err_lower = err_msg.lower()
                    if _is_path_guard_error(err_msg) or _path_looks_unsafe(file):
                        payload["next_actions"] = _path_guard_next_actions(file)
                    elif "file not found" in err_lower or "not found" in err_lower:
                        payload["next_actions"] = [
                            "Check the workspace-relative path and retry.",
                            f"omni_read(file='{file}', mode='outline', format='json') "
                            "after confirming the file exists.",
                            f"omni_search(query='{file}', mode='text', format='json') "
                            "to locate the file.",
                        ]
                    else:
                        payload["next_actions"] = [
                            f"omni_read(file='{file}', mode='outline', format='json') "
                            "to confirm the file is readable.",
                            "Re-check the diagnostics parameters "
                            "(severity, sources).",
                        ]
                _stamp(payload, tool="omni_diagnostics")
                if (format or "json").lower() != "text":
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                return f"❌ {err_msg}"

            # ---------- audit-bundle.r14 (P2): top-level next_actions
            # Different next steps depending on whether issues were
            # found and whether the result was truncated.
            #
            # audit-bundle.r18 (P3): when there are issues, also surface
            # an error-line locator so the AI editor can jump straight
            # to the first problematic line instead of just being told
            # "Fix the errors first". Pulled from the first error/
            # warning row's ``line`` field so it's always accurate.
            counts_blob = payload.get("counts") or {}
            error_count = counts_blob.get("error", 0)
            warning_count = counts_blob.get("warning", 0)
            total_count = payload.get("total_count", 0)
            diag_truncated = payload.get("truncated", False)
            diags_list = payload.get("diagnostics") or []
            first_error_line: Optional[int] = None
            for row in diags_list:
                if (row.get("severity") or "").lower() == "error":
                    first_error_line = row.get("line")
                    if first_error_line:
                        break
            if first_error_line is None:
                for row in diags_list:
                    line_no = row.get("line")
                    if line_no:
                        first_error_line = line_no
                        break
            next_actions: List[str] = []
            if error_count > 0:
                next_actions.append(
                    "Fix the listed errors first — they block omni_patch "
                    "validate/apply if you try to write to this file."
                )
                if first_error_line:
                    next_actions.append(
                        "omni_read(file='%s', mode='range', start_line=%d, "
                        "end_line=%d, format='json') to read the first "
                        "error in context."
                        % (file, max(1, first_error_line - 3),
                           first_error_line + 3)
                    )
            elif warning_count > 0:
                next_actions.append(
                    "Address warnings opportunistically — they don't block "
                    "omni_patch but are worth recording in omni_memory."
                )
                if first_error_line:
                    next_actions.append(
                        "omni_read(file='%s', mode='range', start_line=%d, "
                        "end_line=%d, format='json') to inspect the first "
                        "warning."
                        % (file, max(1, first_error_line - 3),
                           first_error_line + 3)
                    )
            elif total_count == 0:
                next_actions.append(
                    "File is clean. Proceed with omni_patch(action='preview') "
                    "if you intend to edit it."
                )
            if diag_truncated:
                next_actions.append(
                    "Result truncated to first 25 issues — re-run with "
                    "severity='error' to focus on blockers."
                )
            next_actions.append(
                "omni_read(file='%s', mode='outline', format='json') "
                "to map issues to symbols." % file
            )
            payload["next_actions"] = next_actions

            _stamp(payload, tool="omni_diagnostics")
            if (format or "json").lower() != "text":
                return json.dumps(payload, ensure_ascii=False, indent=2)

            shown = payload.get("diagnostics") or []
            total = payload.get("total_count", 0)
            truncated = payload.get("truncated", False)
            if not shown:
                return f"✅ {file} — no diagnostics ({severity}, sources={sources})"
            lines = [
                f"🩺 Diagnostics for {file} "
                f"({total} issue{'' if total == 1 else 's'})\n"
            ]
            for it in shown:
                emoji = {
                    "error": "❌", "warning": "⚠️", "warn": "⚠️",
                    "info": "ℹ️", "hint": "💡",
                }.get((it.get("severity") or "").lower(), "•")
                line_no = it.get("line")
                col = it.get("column")
                anchor = f"L{line_no}" if line_no else "?"
                if col is not None:
                    anchor += f":{col}"
                rule = f" [{it.get('rule')}]" if it.get("rule") else ""
                lines.append(
                    f"  {emoji} {anchor:<10} {it['source']}{rule}: {it['message']}"
                )
            if truncated:
                lines.append(
                    f"\n  ... ({total - 25} more issues)"
                )
            return "\n".join(lines)

        except Exception as e:
            e = Exception(_sanitize_error_text(str(e)))
            err = {"ok": False, "file": file, "error": f"omni_diagnostics failed: {e}"}
            _stamp(err, tool="omni_diagnostics")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() != "text"
                else f"❌ omni_diagnostics failed: {e}"
            )

    @mcp.tool()
    async def omni_patch(
        action: str = "preview",
        file: Optional[str] = None,
        content: Optional[str] = None,
        session_id: Optional[str] = None,
        format: str = "json",
        force: bool = False,
        force_reason: Optional[str] = None,
    ) -> str:
        """Safe edit operations — never let an LLM write to disk directly.

        Actions:
          - preview:   show a unified diff of what would change
          - validate:  run static checks on the proposed content
          - apply:     write to disk + create snapshot + record EditSession
                       (default: runs ``validate`` first; refuses on
                       errors unless ``force=True``)
          - rollback:  restore the file from a previous EditSession's
                       snapshot
          - sessions:  list recent EditSessions

        Path-guard contract (patch.workspace_path_guard):
          Every action that takes ``file`` rejects empty / absolute /
          ``..``-bearing / symlink-escape paths with a structured
          ``ok=false`` envelope including ``allowed_paths_pattern`` and
          ``next_actions``. Historical sessions whose ``file_path`` no
          longer resolves under the workspace are flagged as
          ``unsafe_legacy_session`` on rollback.

        Validate gate (patch.apply_validate_gate):
          ``apply`` runs the same backend ``/patch/validate`` that
          ``action='validate'`` calls. If it reports errors, apply
          short-circuits with ``ok=false``, ``validation_passed=false``
          and the structured ``checks[]`` list — nothing hits disk.
          Set ``force=True`` (with ``force_reason``) to override; the
          response then includes ``validation_bypassed=true`` and a
          warning in ``next_actions``.

        The recommended flow before any AI-driven edit is:
        preview → validate → apply, then keep the returned session_id
        in case you need to rollback.
        """
        is_json = (format or "json").lower() != "text"

        def _err(msg: str, **extra: Any) -> str:
            safe_msg = _sanitize_error_text(msg)
            payload: Dict[str, Any] = {
                "ok": False,
                "action": action,
                "error": safe_msg,
                "allowed_actions": list(_PATCH_ALLOWED_ACTIONS),
                **extra,
            }
            payload.setdefault("next_actions", [
                f"omni_patch(action='{a}', ...)"
                for a in _PATCH_ALLOWED_ACTIONS
            ])
            _stamp(payload, tool="omni_patch")
            msg = safe_msg
            return (
                json.dumps(payload, ensure_ascii=False, indent=2)
                if is_json
                else f"❌ {msg}"
            )

        def _hybrid_patch_local_authority() -> bool:
            try:
                import os as _os

                return (
                    (_os.environ.get("OMNICODE_EXECUTOR_MODE") or "local")
                    .strip()
                    .lower()
                    == "hybrid"
                )
            except Exception:
                return False

        def _local_patch_manager() -> Any:
            from omnicode_core.edit.patch import PatchManager

            workspace_root = _get_workspace_root()[0]
            return PatchManager(str(workspace_root))

        def _local_marker_envelope(
            target_file: str,
            target_path: Path,
        ) -> Dict[str, Any]:
            file_exists = target_path.is_file()
            return {
                "file_exists": file_exists,
                "new_file": not file_exists,
                "file_marker_source": "local_workspace_probe",
                "file_marker_authoritative": True,
                "backend_workspace_root": None,
                "resolved_file_path": target_file,
                "file_marker_warning": None,
            }

        def _patch_result_data(result: Any) -> Dict[str, Any]:
            return {
                "success": bool(getattr(result, "success", False)),
                "message": getattr(result, "message", "") or "",
                "diff": getattr(result, "diff", "") or "",
                "lines_added": getattr(result, "lines_added", 0) or 0,
                "lines_removed": getattr(result, "lines_removed", 0) or 0,
                "session_id": getattr(result, "session_id", None),
                "rollback_available": bool(
                    getattr(result, "rollback_available", False)
                ),
                "file_path": getattr(result, "file_path", None),
            }

        async def _do_local_validate(
            target_file: str,
            target_content: str,
        ) -> Dict[str, Any]:
            try:
                result = await _local_patch_manager().validate_patch(
                    target_file,
                    target_content,
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "validation_passed": False,
                    "message": f"local validate failed: {exc}",
                    "checks": [],
                    "counts": {"error": 0, "warning": 0, "info": 0, "total": 0},
                    "tools_run": ["local_patch_manager"],
                    "tools_skipped": [],
                    "source": "local",
                }

            raw_issues = getattr(result, "diagnostics", None) or []
            checks: List[Dict[str, Any]] = []
            for it in raw_issues:
                if not isinstance(it, dict):
                    checks.append({
                        "source": "local_guard",
                        "severity": "error",
                        "line": None,
                        "column": None,
                        "rule": "",
                        "message": str(it),
                    })
                    continue
                checks.append({
                    "source": it.get("source") or it.get("tool") or "local_guard",
                    "severity": (it.get("severity") or "warning").lower(),
                    "line": it.get("line"),
                    "column": it.get("column"),
                    "rule": it.get("rule") or it.get("code") or "",
                    "message": it.get("message") or "",
                })
            counts = {
                "error": sum(1 for c in checks if c.get("severity") == "error"),
                "warning": sum(
                    1 for c in checks
                    if c.get("severity") in ("warning", "warn")
                ),
                "info": sum(1 for c in checks if c.get("severity") == "info"),
                "total": len(checks),
            }
            validation_passed = bool(getattr(result, "success", False)) and (
                counts["error"] == 0
            )
            return {
                "ok": bool(getattr(result, "success", False)),
                "validation_passed": validation_passed,
                "message": getattr(result, "message", "") or (
                    f"Validation {'passed' if validation_passed else 'failed'}: "
                    f"{counts['error']} error(s), {counts['warning']} warning(s)"
                ),
                "checks": checks,
                "counts": counts,
                "tools_run": ["local_patch_manager"],
                "tools_skipped": [],
                "source": "local",
            }

        def _mark_local_sync_pending(target_file: Optional[str]) -> Dict[str, Any]:
            if not target_file:
                return {"sync_pending": False, "sync_pending_warning": "no file"}
            try:
                import os as _os

                workspace_root = _get_workspace_root()[0]
                from omnicode_core.workspace.local import LocalWorkspace
                from omnicode_core.workspace.manifest import LocalManifest
                from omnicode_core.workspace.sync_client import SyncClient
                from omnicode_core.workspace.sync_queue import SyncQueue

                workspace_id = (
                    _os.environ.get("OMNICODE_WORKSPACE_ID")
                    or workspace_root.name
                    or "workspace"
                )
                local_ws = LocalWorkspace(
                    root=workspace_root,
                    workspace_id=workspace_id,
                )
                manifest = LocalManifest.load(workspace=local_ws)
                change = manifest.mark_changed(target_file)
                if change is not None:
                    manifest.save()
                    meta: Dict[str, Any] = {
                        "sync_pending": True,
                        "sync_pending_path": change.path,
                        "sync_pending_op": change.op,
                        "local_revision": change.revision,
                    }
                    remote = (
                        _os.environ.get("OMNICODE_REMOTE")
                        or _os.environ.get("OMNICODE_FASTAPI_BASE_URL")
                        or _os.environ.get("OMNICODE_BACKEND_URL")
                        or ""
                    ).strip()
                    if not remote:
                        meta["sync_flush_skipped"] = True
                        meta["sync_flush_warning"] = "backend URL not configured"
                        return meta

                    queue = SyncQueue(manifest)
                    batch = queue.next_batch()
                    if batch is None:
                        meta["sync_flush_skipped"] = True
                        meta["sync_skipped_unchanged"] = True
                        return meta

                    client = SyncClient(
                        remote=remote,
                        workspace_id=workspace_id,
                        token=(
                            _os.environ.get("OMNICODE_BACKEND_TOKEN")
                            or _os.environ.get("OMNICODE_CLOUD_TOKEN")
                            or ""
                        ),
                        executor="hybrid",
                        client_id=batch.client_id,
                        timeout=10.0,
                    )
                    try:
                        result = client.push_batch(batch)
                    finally:
                        client.close()
                    if result.ok:
                        queue.mark_accepted(
                            batch,
                            accepted_revision=result.accepted_revision,
                            indexed_revision=result.indexed_revision,
                        )
                        manifest.save()
                        meta.update({
                            "sync_flushed": True,
                            "sync_flush_protocol": "/sync/batch",
                            "accepted_revision": result.accepted_revision,
                            "indexed_revision": result.indexed_revision,
                            "sync_paths": sorted(batch.paths),
                        })
                        return meta

                    meta.update({
                        "sync_flushed": False,
                        "sync_flush_protocol": "/sync/batch",
                        "sync_flush_error": _sanitize_error_text(
                            result.error or "sync failed"
                        ),
                        "sync_flush_status_code": result.status_code,
                    })
                    return meta
                manifest.save()
                return {
                    "sync_pending": False,
                    "sync_skipped_unchanged": True,
                    "local_revision": manifest.local_revision,
                }
            except Exception as exc:
                return {
                    "sync_pending": False,
                    "sync_pending_warning": (
                        f"{exc.__class__.__name__}: {exc}"
                    ),
                }

        def _language_validation_override(
            target_file: str,
        ) -> Optional[Dict[str, Any]]:
            try:
                from omnicode_core.capabilities.languages import (
                    capabilities_for_path,
                    language_for_path,
                )

                caps = capabilities_for_path(target_file)
                language = language_for_path(target_file)
                if caps.validate not in {"unsupported", "not_performed"}:
                    return None
                reason = (
                    f"{language}_validation_unsupported"
                    if language != "unknown"
                    else "validation_not_performed_for_unknown_language"
                )
                return {
                    "ok": True,
                    "validation_passed": None,
                    "validation": {
                        "status": "not_performed",
                        "passed": None,
                        "reason": reason,
                        "language": language,
                    },
                    "message": f"Validation not performed: {reason}",
                    "checks": [],
                    "counts": {
                        "error": 0,
                        "warning": 0,
                        "info": 0,
                        "total": 0,
                    },
                    "tools_run": [],
                    "tools_skipped": [reason],
                    "warnings": [reason],
                    "source": "language_capability_matrix",
                }
            except Exception:
                return None

        # ---- Internal helpers (closures over make_request / _err) -------
        # audit-bundle.r13: ``_do_validate`` lives at the
        # register-scope now (alongside ``_get_backend_file_markers`` and
        # the diagnostics/advisory helpers) so the deprecated ``omni_edit``
        # alias can reuse the exact same validate logic. The version
        # below is the same closure, no behaviour change.

        try:
            # ---- Action whitelist (early so unknown actions never
            #      reach a path-guard / make_request).
            if action not in _PATCH_ALLOWED_ACTIONS:
                return _err(
                    f"Unknown omni_patch action: {action}. "
                    f"Use: {', '.join(_PATCH_ALLOWED_ACTIONS)}",
                )
            local_patch_authority = _hybrid_patch_local_authority()

            # ---- Path guard runs for every file-bearing action.
            resolved_path: Optional[Path] = None
            if action in ("preview", "validate", "apply") and file is not None:
                try:
                    resolved_path = _resolve_workspace_path(file)
                except ValueError as exc:
                    guard_payload = _patch_path_guard_error(
                        action=action, file=file, exc=exc,
                    )
                    return _err(
                        guard_payload["error"],
                        **{
                            k: v for k, v in guard_payload.items()
                            if k not in ("ok", "action", "error")
                        },
                    )

            # ---- audit-bundle.r12 (patch.backend_file_markers): ask the
            # backend whether the file exists, instead of stat()-ing
            # against the MCP host's workspace root. r11 verification
            # showed those two roots can differ when the host CWD and
            # the backend CWD are different processes; the backend probe
            # always agrees with what apply / read will actually see on
            # disk. Markers are advisory — apply still creates files when
            # needed, the path guard above is the actual safety boundary.
            new_file: Optional[bool] = None
            marker_envelope: Optional[Dict[str, Any]] = None
            if resolved_path is not None and file is not None:
                if local_patch_authority:
                    marker_envelope = _local_marker_envelope(file, resolved_path)
                else:
                    marker_envelope = await _get_backend_file_markers(file)
                new_file = marker_envelope.get("new_file")

            def _marker_fields() -> Dict[str, Any]:
                """Return the dict of r12 marker fields for the current
                action's payload. Empty when no probe was attempted (e.g.
                rollback / sessions don't take a file argument)."""
                if marker_envelope is None:
                    return {}
                return {
                    "file_exists": marker_envelope.get("file_exists"),
                    "new_file": marker_envelope.get("new_file"),
                    "file_marker_source": marker_envelope.get(
                        "file_marker_source", "unknown"
                    ),
                    "file_marker_authoritative": marker_envelope.get(
                        "file_marker_authoritative", False
                    ),
                    "backend_workspace_root": marker_envelope.get(
                        "backend_workspace_root"
                    ),
                    "resolved_file_path": marker_envelope.get(
                        "resolved_file_path"
                    ),
                    "file_marker_warning": marker_envelope.get(
                        "file_marker_warning"
                    ),
                }

            # =========================================================
            #                          preview
            # =========================================================
            if action == "preview":
                if not file or content is None:
                    return _err("omni_patch preview needs both file and content.")
                if local_patch_authority:
                    data = _patch_result_data(
                        _local_patch_manager().preview_patch(file, content)
                    )
                else:
                    raw = await make_request("POST", "/patch/preview", json={
                        "file_path": file, "content": content,
                    })
                    data = raw.get("result", raw) if isinstance(raw, dict) else {}
                if not data.get("success", True):
                    # audit-bundle.r12 (patch.backend_file_markers): pass
                    # through the full marker envelope, including the
                    # source / authoritative / warning fields so the
                    # caller can tell whether the marker is advisory or
                    # backend-confirmed.
                    extra: Dict[str, Any] = dict(_marker_fields())
                    # audit-bundle.r19 (patch.preview_new_file_ok):
                    # when the probe-authoritative ``new_file`` marker
                    # tells us the file does not exist, the backend's
                    # ``Preview failed: File not found`` is the expected
                    # path for a *creation* preview. Synthesize a
                    # successful creation diff locally so AI editors
                    # following the safe-edit pipeline don't abort on
                    # ``ok=False``.
                    if (
                        new_file is True
                        and marker_envelope is not None
                        and marker_envelope.get("file_marker_authoritative")
                        is True
                    ):
                        content_lines = (content or "").splitlines()
                        # Header + every content line as an addition,
                        # following standard unified-diff shape so
                        # downstream tools (validators, viewers) can
                        # treat it identically to a backend-rendered
                        # diff. The line count below counts content
                        # lines only — the header is metadata, not a
                        # change.
                        synth_lines = [
                            "--- /dev/null",
                            f"+++ b/{file}",
                            f"@@ -0,0 +1,{len(content_lines)} @@",
                        ] + [f"+{line}" for line in content_lines]
                        synth_diff = "\n".join(synth_lines)
                        diff_truncated = len(synth_lines) > 80
                        if diff_truncated:
                            synth_diff = "\n".join(synth_lines[:80])
                        payload = {
                            "ok": True,
                            "action": "preview",
                            "file": file,
                            **_marker_fields(),
                            "lines_added": len(content_lines),
                            "lines_removed": 0,
                            "diff": synth_diff,
                            "diff_truncated": diff_truncated,
                            "diff_total_lines": len(synth_lines),
                            "newline_normalized": False,
                            "preview_synthesized": True,
                            "preview_synthesized_reason": (
                                "new_file=true; backend cannot diff a "
                                "nonexistent file, so the preview was "
                                "synthesized locally as a creation diff."
                            ),
                            "source": "local" if local_patch_authority else "backend",
                            "local_authority": local_patch_authority,
                            "next_actions": [
                                f"omni_patch(action='validate', file='{file}', "
                                f"content=...) to verify the new-file content.",
                                f"omni_patch(action='apply', file='{file}', "
                                f"content=...) to create the file.",
                            ],
                        }
                        _stamp(payload, tool="omni_patch")
                        if is_json:
                            return json.dumps(
                                payload, ensure_ascii=False, indent=2
                            )
                        tail = "" if not diff_truncated else (
                            f"\n... ({len(synth_lines) - 80} more diff lines)"
                        )
                        return (
                            f"📋 Preview (new file): {file}\n"
                            f"   +{len(content_lines)} / -0 lines "
                            f"(synthesized)\n\n"
                            f"{synth_diff}{tail}"
                        )
                    if new_file:
                        extra["next_actions"] = [
                            f"omni_patch(action='validate', file='{file}', "
                            f"content=...) to verify the new-file content.",
                            f"omni_patch(action='apply', file='{file}', "
                            f"content=...) to create the file.",
                        ]
                    return _err(
                        f"Preview failed: {data.get('message', 'unknown')}",
                        **extra,
                    )
                added = data.get("lines_added", 0)
                removed = data.get("lines_removed", 0)
                raw_diff = data.get("diff", "") or ""
                normalised, was_normalised = _normalise_diff_text(raw_diff)
                diff = normalised
                diff_lines = diff.splitlines()
                diff_truncated = len(diff_lines) > 80
                if diff_truncated:
                    diff = "\n".join(diff_lines[:80])
                payload = {
                    "ok": True,
                    "action": "preview",
                    "file": file,
                    **_marker_fields(),
                    "lines_added": added,
                    "lines_removed": removed,
                    "diff": diff,
                    "diff_truncated": diff_truncated,
                    "diff_total_lines": len(diff_lines),
                    "newline_normalized": was_normalised,
                    "source": "local" if local_patch_authority else "backend",
                    "local_authority": local_patch_authority,
                    "next_actions": [
                        f"omni_patch(action='validate', file='{file}', content=...)",
                        f"omni_patch(action='apply', file='{file}', content=...)",
                    ],
                }
                _stamp(payload, tool="omni_patch")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                tail = "" if not diff_truncated else (
                    f"\n... ({len(diff_lines) - 80} more diff lines)"
                )
                return (
                    f"📋 Preview: {file}\n"
                    f"   +{added} / -{removed} lines\n\n"
                    f"{diff}{tail}"
                )

            # =========================================================
            #                          validate
            # =========================================================
            if action == "validate":
                if not file or content is None:
                    return _err("omni_patch validate needs both file and content.")
                v = _language_validation_override(file) or (
                    await _do_local_validate(file, content)
                    if local_patch_authority
                    else await _do_validate(file, content)
                )
                validation_state = v.get("validation") or {
                    "status": (
                        "passed"
                        if v.get("validation_passed") is True
                        else "failed"
                    ),
                    "passed": v.get("validation_passed"),
                }
                payload = {
                    "ok": v["validation_passed"] is not False,
                    "action": "validate",
                    "file": file,
                    **_marker_fields(),
                    "validation_passed": v["validation_passed"],
                    "validation": validation_state,
                    "message": v["message"],
                    "checks": v["checks"],
                    "counts": v["counts"],
                    "tools_run": v["tools_run"],
                    "tools_skipped": v["tools_skipped"],
                    "warnings": list(v.get("warnings") or []),
                    "source": v["source"],
                    "local_authority": local_patch_authority,
                    "next_actions": (
                        [
                            f"omni_patch(action='apply', file='{file}', content=...) "
                            "to write with path/conflict guards only.",
                            "Run the project-native compiler/test command for this language.",
                        ]
                        if v["validation_passed"] is None
                        else
                        [
                            f"omni_patch(action='apply', file='{file}', content=...) "
                            f"to write the change",
                            f"omni_patch(action='preview', file='{file}', content=...) "
                            f"to inspect the diff once more",
                        ]
                        if v["validation_passed"]
                        else [
                            "Fix the listed errors and re-validate.",
                            f"omni_diagnostics(file='{file}') for the full lint surface.",
                            "omni_patch(action='apply', force=True, force_reason=...) "
                            "to override (NOT recommended).",
                        ]
                    ),
                }
                _stamp(payload, tool="omni_patch")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                lines = [
                    f"{'✅' if v['validation_passed'] else '❌'} Validate: {file}",
                ]
                if v["message"]:
                    lines.append(f"   {v['message']}")
                for c in v["checks"][:10]:
                    line_no = c.get("line")
                    anchor = f"L{line_no}" if line_no else "?"
                    lines.append(
                        f"   • [{c.get('severity', '?')}] {anchor} "
                        f"{c.get('source', '?')}/{c.get('rule', '-') or '-'}: "
                        f"{c.get('message', '')}"
                    )
                return "\n".join(lines)

            # =========================================================
            #                            apply
            # =========================================================
            if action == "apply":
                if not file or content is None:
                    return _err("omni_patch apply needs both file and content.")

                # Validate gate — runs by default, refuses on errors
                # unless the caller set force=True with a reason.
                v = _language_validation_override(file) or (
                    await _do_local_validate(file, content)
                    if local_patch_authority
                    else await _do_validate(file, content)
                )
                if v["validation_passed"] is False and not force:
                    err_payload = {
                        "ok": False,
                        "action": "apply",
                        "file": file,
                        **_marker_fields(),
                        "error": "apply blocked by validation failure",
                        "validation_passed": False,
                        "validation_bypassed": False,
                        "checks": v["checks"],
                        "counts": v["counts"],
                        "tools_run": v["tools_run"],
                        "tools_skipped": v["tools_skipped"],
                        "source": v["source"],
                        "local_authority": local_patch_authority,
                        "allowed_actions": list(_PATCH_ALLOWED_ACTIONS),
                        "next_actions": [
                            "Fix the listed errors and retry "
                            f"omni_patch(action='apply', file='{file}', content=...).",
                            f"omni_patch(action='preview', file='{file}', "
                            f"content=...) to re-inspect the diff.",
                            "Set force=True with force_reason='...' to override "
                            "(strongly discouraged).",
                        ],
                    }
                    _stamp(err_payload, tool="omni_patch")
                    if is_json:
                        return json.dumps(err_payload, ensure_ascii=False, indent=2)
                    return (
                        f"❌ Apply blocked: validation failed "
                        f"({v['counts']['error']} error(s), "
                        f"{v['counts']['warning']} warning(s)). "
                        f"Fix and retry."
                    )

                if local_patch_authority:
                    pm = _local_patch_manager()
                    result = pm.apply_patch(
                        file,
                        content,
                        source="omni_patch:hybrid_local",
                        metadata={
                            "executor": "hybrid",
                            "local_authority": True,
                            "validation_passed": v["validation_passed"],
                            "validation_status": (
                                (v.get("validation") or {}).get("status")
                            ),
                            "validation_bypassed": (
                                (v["validation_passed"] is False) and force
                            ),
                            "force_reason": force_reason if force else None,
                        },
                    )
                    data = _patch_result_data(result)
                    sid_for_hash = data.get("session_id")
                    if sid_for_hash:
                        session_row = pm.get_session(sid_for_hash) or {}
                        data["original_hash"] = session_row.get("original_hash")
                        data["new_hash"] = session_row.get("new_hash")
                else:
                    raw = await make_request("POST", "/patch/apply", json={
                        "file_path": file, "content": content,
                    })
                    data = raw.get("result", raw) if isinstance(raw, dict) else {}
                if not data.get("success", False):
                    return _err(f"Apply failed: {data.get('message', 'unknown')}")
                sid = data.get("session_id")
                added = data.get("lines_added", 0)
                removed = data.get("lines_removed", 0)
                rb = data.get("rollback_available", True)
                sync_meta = (
                    _mark_local_sync_pending(file)
                    if local_patch_authority
                    else {}
                )
                payload = {
                    "ok": True,
                    "action": "apply",
                    "file": file,
                    **_marker_fields(),
                    "lines_added": added,
                    "lines_removed": removed,
                    "session_id": sid,
                    "rollback_available": rb,
                    "validation_passed": v["validation_passed"],
                    "validation": v.get("validation") or {
                        "status": (
                            "passed"
                            if v.get("validation_passed") is True
                            else "failed"
                            if v.get("validation_passed") is False
                            else "not_performed"
                        ),
                        "passed": v.get("validation_passed"),
                    },
                    "validation_bypassed": (
                        v["validation_passed"] is False
                    ) and force,
                    "force_reason": force_reason if force else None,
                    "warnings": list(v.get("warnings") or []),
                    "original_hash": data.get("original_hash"),
                    "new_hash": data.get("new_hash"),
                    "source": "local" if local_patch_authority else "backend",
                    "local_authority": local_patch_authority,
                    "sync_pending": local_patch_authority,
                    "sync_trigger": (
                        "filesystem_mtime" if local_patch_authority else None
                    ),
                    **sync_meta,
                    "next_actions": (
                        [
                            f"omni_patch(action='rollback', session_id='{sid}') "
                            "if anything looks wrong",
                            f"omni_diagnostics(file='{file}') to verify the "
                            "post-apply state",
                            f"omni_read(file='{file}', mode='full') to see the "
                            "written content",
                        ]
                        if rb
                        else [
                            f"omni_diagnostics(file='{file}') to verify the "
                            "post-apply state",
                        ]
                    ),
                }
                if v["validation_passed"] is False and force:
                    payload["next_actions"].insert(0, (
                        "⚠️ Validation was bypassed via force=True. "
                        "Re-run omni_diagnostics + tests immediately."
                    ))
                _stamp(payload, tool="omni_patch")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                bypass_tag = (
                    "  ⚠️ validation bypassed"
                    if (v["validation_passed"] is False and force)
                    else ""
                )
                return (
                    f"✅ Applied: {file}{bypass_tag}\n"
                    f"   +{added} / -{removed} lines\n"
                    f"   session_id: {sid}\n"
                    f"   rollback_available: {rb}\n"
                    f"\n   To undo: omni_patch(action='rollback', session_id='{sid}')"
                )

            # =========================================================
            #                          rollback
            # =========================================================
            if action == "rollback":
                if not session_id:
                    return _err("omni_patch rollback needs session_id.")

                if local_patch_authority:
                    _EMPTY_CONTENT_SHA256_PREFIX = "e3b0c44298fc1c14"
                    pm = _local_patch_manager()
                    session_before = pm.get_session(session_id)
                    local_legacy_path = (
                        session_before.get("file_path")
                        if isinstance(session_before, dict)
                        else None
                    )
                    unsafe_legacy = False
                    session_was_new_file = False
                    if local_legacy_path:
                        try:
                            _resolve_workspace_path(str(local_legacy_path))
                        except ValueError:
                            unsafe_legacy = True
                        session_was_new_file = (
                            (session_before.get("original_hash") or "")
                            == _EMPTY_CONTENT_SHA256_PREFIX
                        ) if isinstance(session_before, dict) else False

                    if unsafe_legacy:
                        err_payload = {
                            "ok": False,
                            "action": "rollback",
                            "session_id": session_id,
                            "error": (
                                f"unsafe_legacy_session: session {session_id} "
                                f"targets {local_legacy_path!r}, which sits outside "
                                "the workspace. Refusing to rollback."
                            ),
                            "unsafe_legacy_session": True,
                            "legacy_file_path": local_legacy_path,
                            "source": "local",
                            "local_authority": True,
                            "allowed_actions": list(_PATCH_ALLOWED_ACTIONS),
                            "next_actions": [
                                "omni_patch(action='sessions') to inspect recent "
                                "sessions and pick a safe one.",
                            ],
                        }
                        _stamp(err_payload, tool="omni_patch")
                        if is_json:
                            return json.dumps(err_payload, ensure_ascii=False, indent=2)
                        return (
                            f"鉂?Rollback refused: session {session_id} targets "
                            f"path outside the workspace ({local_legacy_path!r})."
                        )

                    result = pm.rollback_patch(session_id)
                    data = _patch_result_data(result)
                    ok = bool(data.get("success", False))
                    msg = data.get("message", "")

                    new_file_unlinked = False
                    local_new_file_unlink_warning: Optional[str] = None
                    if ok and session_was_new_file and local_legacy_path:
                        try:
                            local_target = _resolve_workspace_path(
                                str(local_legacy_path)
                            )
                            if local_target.exists() and local_target.is_file():
                                try:
                                    size = local_target.stat().st_size
                                except OSError:
                                    size = -1
                                if size == 0:
                                    local_target.unlink(missing_ok=True)
                                    new_file_unlinked = True
                                else:
                                    local_new_file_unlink_warning = (
                                        f"new-file rollback skipped unlink: "
                                        f"file is {size} bytes, expected 0. "
                                        "Manual cleanup may be required."
                                    )
                        except Exception as exc:
                            local_new_file_unlink_warning = (
                                f"new-file rollback unlink failed: "
                                f"{exc.__class__.__name__}: {exc}"
                            )

                    sync_meta = (
                        _mark_local_sync_pending(str(local_legacy_path))
                        if ok and local_legacy_path
                        else {}
                    )
                    payload = {
                        "ok": ok,
                        "action": "rollback",
                        "session_id": session_id,
                        "file": local_legacy_path,
                        "message": msg,
                        "rolled_back": ok,
                        "previous_hash": (
                            session_before.get("new_hash")
                            if isinstance(session_before, dict)
                            else None
                        ),
                        "restored_hash": (
                            session_before.get("original_hash")
                            if isinstance(session_before, dict)
                            else None
                        ),
                        "new_file_unlinked": new_file_unlinked,
                        "new_file_unlink_warning": local_new_file_unlink_warning,
                        "source": "local",
                        "local_authority": True,
                        "sync_pending": ok,
                        "sync_trigger": "filesystem_mtime" if ok else None,
                        **sync_meta,
                        "next_actions": (
                            [
                                f"omni_diagnostics(file='{local_legacy_path}') to confirm "
                                "the rolled-back state is clean",
                                f"omni_read(file='{local_legacy_path}', mode='full') to see "
                                "the restored content",
                            ]
                            if ok and local_legacy_path
                            else [
                                "omni_patch(action='sessions') to inspect rollback state",
                            ]
                        ),
                    }
                    if not ok and msg:
                        payload["error"] = msg
                        low = msg.lower()
                        if "not found" in low or "no session" in low:
                            payload["next_actions"] = [
                                "omni_patch(action='sessions', format='json') to "
                                "list valid session_ids.",
                                "Confirm the session_id was copied from a recent "
                                "omni_patch(action='apply', ...) response.",
                            ] + payload["next_actions"]
                        elif "already" in low and "rolled" in low:
                            payload["next_actions"] = [
                                "Session already rolled back 鈥?no further action "
                                "needed. Verify with omni_read.",
                            ] + payload["next_actions"]
                    _stamp(payload, tool="omni_patch")
                    if is_json:
                        return json.dumps(payload, ensure_ascii=False, indent=2)
                    status = "OK" if ok else "ERROR"
                    return f"{status} Rollback: {msg}"

                # Pre-flight: look up the session, check the file path
                # is still inside the workspace. Legacy sessions with
                # paths like ``../../outside.py`` (recorded before the
                # path guard existed) must not be silently restored.
                # audit-bundle.r20: also remember whether the session
                # was a *new-file creation* (original_hash == empty
                # sentinel), so that after the backend rollback we can
                # remove the truncated 0-byte stub the backend leaves
                # behind. The pre-edit state was "file does not exist";
                # restoring to a 0-byte file would be a partial
                # restoration only.
                _EMPTY_CONTENT_SHA256_PREFIX = "e3b0c44298fc1c14"
                unsafe_legacy = False
                legacy_path: Optional[str] = None
                session_was_new_file = False
                try:
                    sess_raw = await make_request(
                        "GET", "/patch/sessions", params={"limit": 50},
                    )
                    sess_data = (
                        sess_raw.get("result", sess_raw)
                        if isinstance(sess_raw, dict) else {}
                    )
                    for s in (sess_data.get("sessions") or []):
                        if s.get("session_id") == session_id:
                            legacy_path = s.get("file_path") or ""
                            try:
                                _resolve_workspace_path(legacy_path)
                            except ValueError:
                                unsafe_legacy = True
                            # Detect new-file creation session: the
                            # original-content hash equals the
                            # well-known empty-bytes sha256 prefix.
                            session_was_new_file = (
                                (s.get("original_hash") or "")
                                == _EMPTY_CONTENT_SHA256_PREFIX
                            )
                            break
                except Exception:
                    legacy_path = None

                if unsafe_legacy:
                    err_payload = {
                        "ok": False,
                        "action": "rollback",
                        "session_id": session_id,
                        "error": (
                            f"unsafe_legacy_session: session {session_id} "
                            f"targets {legacy_path!r}, which sits outside the "
                            f"workspace. Refusing to rollback."
                        ),
                        "unsafe_legacy_session": True,
                        "legacy_file_path": legacy_path,
                        "allowed_actions": list(_PATCH_ALLOWED_ACTIONS),
                        "next_actions": [
                            "omni_patch(action='sessions') to inspect recent "
                            "sessions and pick a safe one.",
                        ],
                    }
                    _stamp(err_payload, tool="omni_patch")
                    if is_json:
                        return json.dumps(err_payload, ensure_ascii=False, indent=2)
                    return (
                        f"❌ Rollback refused: session {session_id} targets "
                        f"path outside the workspace ({legacy_path!r})."
                    )

                raw = await make_request(
                    "POST", "/patch/rollback",
                    params={"session_id": session_id},
                )
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = bool(data.get("success", False))
                msg = data.get("message", "")

                # ---- audit-bundle.r20 (P0-1): new-file rollback must
                # restore the pre-edit state, which is "file does not
                # exist". The backend rollback truncates the file to 0
                # bytes; we follow up by unlinking the file from disk
                # so a subsequent omni_read sees file-not-found.
                # Only runs when the rollback succeeded AND the session
                # is a new-file creation AND the path is workspace-safe.
                #
                # The path guard above (_resolve_workspace_path) gates
                # safety. For the actual unlink target, we resolve
                # against three candidate roots in order:
                #   1. host workspace_root
                #   2. Path.cwd()
                #   3. project root derived from this module's __file__
                # Reason: when the host runs from a different working
                # dir than the project (Kiro MCP host runs from
                # ``E:\Kiro`` while the project lives elsewhere — the
                # documented ``workspace.backend_root_visibility``
                # backlog item), the host workspace_root and cwd point
                # at the wrong place. Candidate 3 always finds the
                # actual project where the backend wrote the file.
                # Each candidate that's a different drive / outside the
                # workspace is rejected by the same path-traversal
                # guard before unlink, so this is safe.
                new_file_unlinked = False
                new_file_unlink_warning: Optional[str] = None
                if (
                    ok
                    and session_was_new_file
                    and not unsafe_legacy
                    and legacy_path
                ):
                    try:
                        candidates: List[Path] = []
                        # Candidate 1: host workspace_root (may differ
                        # from cwd in dev environments).
                        try:
                            candidates.append(
                                _resolve_workspace_path(legacy_path)
                            )
                        except ValueError:
                            pass
                        # Candidate 2: cwd.
                        cwd_root = Path.cwd().resolve()
                        cwd_candidate = (
                            cwd_root / legacy_path
                        ).resolve(strict=False)
                        try:
                            cwd_candidate.relative_to(cwd_root)
                            candidates.append(cwd_candidate)
                        except ValueError:
                            pass
                        # Candidate 3: project root inferred from this
                        # module's __file__ — robust to host/backend
                        # cwd mismatch. Walk up until we find the
                        # project marker (the ``omnicode_adapters``
                        # package containing this module).
                        try:
                            module_dir = Path(__file__).resolve().parent
                            # __file__ is .../omnicode_adapters/mcp_server/high_level_tools.py
                            # project root is two levels up.
                            project_root = module_dir.parent.parent
                            mod_candidate = (
                                project_root / legacy_path
                            ).resolve(strict=False)
                            try:
                                mod_candidate.relative_to(project_root)
                                if mod_candidate not in candidates:
                                    candidates.append(mod_candidate)
                            except ValueError:
                                pass
                        except Exception:
                            pass
                        # Pick the first candidate that actually exists.
                        target: Optional[Path] = None
                        for cand in candidates:
                            if cand.exists() and cand.is_file():
                                target = cand
                                break
                        if target is not None:
                            try:
                                size = target.stat().st_size
                            except OSError:
                                size = -1
                            # Only unlink when the post-rollback file is
                            # what we expect (0 bytes); a non-empty file
                            # at this point would indicate the user
                            # already wrote new content, in which case
                            # we leave it alone and surface a warning.
                            if size == 0:
                                target.unlink(missing_ok=True)
                                new_file_unlinked = True
                            else:
                                new_file_unlink_warning = (
                                    f"new-file rollback skipped unlink: "
                                    f"file is {size} bytes, expected 0. "
                                    "Manual cleanup may be required."
                                )
                        else:
                            new_file_unlink_warning = (
                                "new-file rollback could not locate the "
                                "file at any candidate root "
                                "(workspace_root, cwd, project_root); "
                                "the backend may have written elsewhere. "
                                "Manual cleanup may be required."
                            )
                    except Exception as exc:
                        new_file_unlink_warning = (
                            f"new-file rollback unlink failed: "
                            f"{exc.__class__.__name__}: {exc}"
                        )

                # ---- audit-bundle.r20 (P0-2): cache invalidation.
                # The backend ``/read`` endpoint may cache the file
                # state from an earlier omni_read call. After a
                # successful rollback (especially the unlink branch
                # above), prime the backend so the next omni_read sees
                # the current disk state. The file marker probe used by
                # omni_patch already does an outline read, which is the
                # cheapest way to refresh whatever cache the backend
                # carries. The result is intentionally discarded — this
                # is purely a side-effect call.
                if ok and legacy_path and not unsafe_legacy:
                    try:
                        await _get_backend_file_markers(legacy_path)
                    except Exception:
                        # Best-effort — never fail the rollback because
                        # the cache-warm probe errored.
                        pass

                payload = {
                    "ok": ok,
                    "action": "rollback",
                    "session_id": session_id,
                    "file": legacy_path,
                    "message": msg,
                    "rolled_back": ok,
                    "previous_hash": data.get("previous_hash") or data.get("new_hash"),
                    "restored_hash": (
                        data.get("restored_hash")
                        or data.get("restored_to_hash")
                        or data.get("original_hash")
                    ),
                    "new_file_unlinked": new_file_unlinked,
                    "new_file_unlink_warning": new_file_unlink_warning,
                    "next_actions": (
                        [
                            f"omni_diagnostics(file='{legacy_path}') to confirm "
                            "the rolled-back state is clean",
                            f"omni_read(file='{legacy_path}', mode='full') to see "
                            "the restored content",
                        ]
                        if ok and legacy_path
                        else [
                            "omni_patch(action='sessions') to inspect rollback state",
                        ]
                    ),
                }
                # ---- audit-bundle.r15 (P1): error-field alignment for
                # rollback failures (e.g. "Session not found"). Mirrors
                # the r14 fix on omni_edit so AI editors can rely on a
                # single ``error`` key across the surface; ``message`` is
                # kept untouched for back-compat.
                if not ok and msg:
                    payload["error"] = msg
                    low = msg.lower()
                    if "not found" in low or "no session" in low:
                        payload["next_actions"] = [
                            "omni_patch(action='sessions', format='json') to "
                            "list valid session_ids.",
                            "Confirm the session_id was copied from a recent "
                            "omni_patch(action='apply', ...) response.",
                        ] + payload["next_actions"]
                    elif "already" in low and "rolled" in low:
                        payload["next_actions"] = [
                            "Session already rolled back — no further action "
                            "needed. Verify with omni_read.",
                        ] + payload["next_actions"]
                _stamp(payload, tool="omni_patch")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                return f"{'✅' if ok else '❌'} Rollback: {msg}"

            # =========================================================
            #                          sessions
            # =========================================================
            if action == "sessions":
                _SESSIONS_LIMIT = 20
                if local_patch_authority:
                    local_sessions = _local_patch_manager().list_sessions(
                        _SESSIONS_LIMIT
                    )
                    data = {
                        "sessions": local_sessions,
                        "total_count": len(local_sessions),
                    }
                else:
                    raw = await make_request(
                        "GET", "/patch/sessions", params={"limit": _SESSIONS_LIMIT},
                    )
                    data = raw.get("result", raw) if isinstance(raw, dict) else {}
                sessions = data.get("sessions", []) or []
                # ---- audit-bundle.r14 (P2): truncation transparency.
                # Backend may keep growing the session log; we cap
                # display at _SESSIONS_LIMIT and emit ``truncated`` +
                # ``total_count`` so the AI editor can ask for more.
                backend_total = (
                    data.get("total_count")
                    or data.get("total")
                    or len(sessions)
                )
                truncated = bool(
                    backend_total and backend_total > _SESSIONS_LIMIT
                ) or len(sessions) > _SESSIONS_LIMIT
                # Annotate each session with whether it's still inside
                # the workspace — AI editors can use this to avoid
                # offering rollback on legacy unsafe sessions.
                annotated: List[Dict[str, Any]] = []
                for s in sessions[:_SESSIONS_LIMIT]:
                    s = dict(s)
                    fp = s.get("file_path") or ""
                    try:
                        _resolve_workspace_path(fp)
                        s["unsafe_legacy_session"] = False
                    except ValueError:
                        s["unsafe_legacy_session"] = True
                    annotated.append(s)
                next_actions = [
                    "omni_patch(action='rollback', session_id='<id>') "
                    "to undo a session (skip rows where "
                    "unsafe_legacy_session=true)",
                    "omni_diagnostics(file='<path>') to verify a file's "
                    "current state",
                    "omni_read(file='<path>', mode='full') to inspect file content",
                ]
                if truncated:
                    next_actions.insert(
                        0,
                        "Result truncated to %d of %s sessions — older "
                        "rows are not shown."
                        % (_SESSIONS_LIMIT, backend_total),
                    )
                payload = {
                    "ok": True,
                    "action": "sessions",
                    "count": len(annotated),
                    "limit": _SESSIONS_LIMIT,
                    "total_count": backend_total,
                    "truncated": truncated,
                    "sessions": annotated,
                    "source": "local" if local_patch_authority else "backend",
                    "local_authority": local_patch_authority,
                    "next_actions": next_actions,
                }
                _stamp(payload, tool="omni_patch")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                if not annotated:
                    return "📜 No recent EditSessions."
                tail = (
                    f"  (truncated, {backend_total} total)" if truncated else ""
                )
                lines = [
                    f"📜 Recent EditSessions ({len(annotated)}){tail}:\n"
                ]
                for s in annotated:
                    sid = s.get("session_id", "?")
                    fp = s.get("file_path", "?")
                    ts = s.get("timestamp", "?")
                    src = s.get("source", "?")
                    a = s.get("lines_added", 0)
                    r = s.get("lines_removed", 0)
                    flag = " ⚠️unsafe-legacy" if s.get("unsafe_legacy_session") else ""
                    lines.append(
                        f"  {sid}  {ts}  {fp}  +{a}/-{r}  ({src}){flag}"
                    )
                return "\n".join(lines)

            # Defensive — should never reach here because the action
            # whitelist runs first.
            return _err(
                f"Unknown omni_patch action: {action}. "
                f"Use: {', '.join(_PATCH_ALLOWED_ACTIONS)}"
            )

        except Exception as e:
            return _err(f"omni_patch failed: {e}")

    # -------------------------------------------------------------------
    # Backwards-compat alias — omni_analyze delegates to omni_impact for
    # the most common case ("impact" analysis). Kept so older MCP configs
    # don't break, but new clients should use omni_impact directly.
    # -------------------------------------------------------------------
    @mcp.tool()
    async def omni_analyze(
        symbol: str,
        analysis: str = "impact",
        depth: int = 2,
        path: Optional[str] = None,
        format: str = "text",
    ) -> str:
        """[deprecated alias] Use omni_impact for impact analysis.

        Analysis types:
          - impact (default): delegates to omni_impact
          - callers / callees / graph: low-level call-graph queries

        Kept for backwards compatibility with older MCP configs. Pass
        ``format='json'`` for the structured deprecated-alias envelope.
        """
        fmt = (format or "text").lower()

        def _alias_json(payload: Dict[str, Any]) -> str:
            return json.dumps(
                _alias_envelope("omni_analyze", payload),
                ensure_ascii=False, default=str,
            )

        # ---------------------------------------------------------------
        # audit-bundle.r13 (P1-B): empty / whitespace symbol must be a
        # structured ok=false envelope, not a synthetic risk=low stub.
        # Mirrors omni_impact's empty-symbol guard so the alias surface
        # cannot be used to fake an "everything's fine" report.
        # ---------------------------------------------------------------
        symbol_clean = (symbol or "").strip()
        if not symbol_clean:
            err = "omni_analyze requires a non-empty symbol name."
            if fmt == "json":
                return _alias_json({
                    "ok": False,
                    "analysis": analysis,
                    "symbol": symbol,
                    "error": err,
                    "next_actions": [
                        "omni_impact(symbol='<name>', format='json') — the "
                        "supported entry point for blast-radius analysis.",
                        "omni_search(mode='symbol', query='<partial_name>', "
                        "format='json') if you don't know the exact symbol.",
                    ],
                })
            return f"❌ {err}"

        try:
            if analysis in ("callers", "callees", "impact"):
                direction = "both" if analysis == "impact" else analysis
                payload = {
                    "symbol": symbol_clean,
                    "direction": direction,
                    "max_files": 200,
                }
                if path:
                    payload["path"] = path

                result = await make_request("POST", "/search/symbols/relations", json=payload)

                if "error" in result:
                    if fmt == "json":
                        return _alias_json({
                            "ok": False, "error": f"Analysis error: {result['error']}",
                        })
                    return f"❌ Analysis error: {result['error']}"

                data = result.get("result", result)
                callers = data.get("callers", {})
                callees = data.get("callees", {})
                caller_count = callers.get("count", 0) if callers else 0
                callee_count = callees.get("count", 0) if callees else 0
                # ---------------------------------------------------------------
                # audit-bundle.r13 (P1-A): missing-symbol parity with
                # omni_impact.  When the symbol has neither callers nor
                # callees in the graph, we cannot be sure the symbol
                # exists — emit risk=unknown (not the misleading
                # risk=low) plus confidence=low and a note pointing at
                # omni_search.
                #
                # Live-test fix: the backend returns ``total_edges`` as
                # the GLOBAL call-graph size (e.g. 10573), not the
                # symbol's edge count, so it cannot be used as a
                # missing-symbol signal. The only reliable indicator
                # from /search/symbols/relations is that BOTH
                # callers.count and callees.count are zero.
                # ---------------------------------------------------------------
                total_edges = data.get("total_edges")
                no_edges = caller_count == 0 and callee_count == 0
                if no_edges:
                    risk = "unknown"
                    confidence = "low"
                    note = (
                        f"Symbol '{symbol_clean}' was not found in the call "
                        "graph. Risk reported as 'unknown' to avoid a false "
                        "low-risk signal."
                    )
                else:
                    risk = (
                        "high" if caller_count > 10
                        else "medium" if caller_count > 3
                        else "low"
                    )
                    confidence = "high" if (caller_count or callee_count) else "medium"
                    note = None

                if fmt == "json":
                    json_payload: Dict[str, Any] = {
                        "ok": True,
                        "analysis": analysis,
                        "symbol": symbol_clean,
                        "risk": risk,
                        "confidence": confidence,
                        "callers": callers,
                        "callees": callees,
                        "total_edges": total_edges,
                        # audit-bundle.r14 (P2): stamp ``source`` on every
                        # branch of omni_analyze so callers, callees, and
                        # impact analyses agree on provenance — same key
                        # omni_impact uses.
                        "source": "graph",
                        "next_actions": [
                            "Prefer omni_impact(symbol='%s', format='json') — "
                            "it returns risk + suggested_tests + source/confidence."
                            % symbol_clean,
                        ],
                    }
                    if no_edges:
                        json_payload["note"] = note
                        json_payload["next_actions"].insert(
                            0,
                            "omni_search(mode='symbol', query='%s', "
                            "format='json') to confirm the symbol exists."
                            % symbol_clean,
                        )
                    return _alias_json(json_payload)

                lines = [f"🔍 Impact analysis: {symbol_clean}\n"]
                lines.append(f"  Total edges in scope: {data.get('total_edges', '?')}")

                if callers:
                    lines.append(f"\n  ⬆️ Callers ({callers.get('count', 0)}):")
                    for name in (callers.get("names") or [])[:15]:
                        lines.append(f"    ← {name}")

                if callees:
                    lines.append(f"\n  ⬇️ Callees ({callees.get('count', 0)}):")
                    for name in (callees.get("names") or [])[:15]:
                        lines.append(f"    → {name}")

                # Risk assessment
                lines.append(
                    f"\n  ⚠️ Risk: {risk} (confidence={confidence}, "
                    f"{caller_count} direct callers)"
                )
                if note:
                    lines.append(f"  ℹ️ {note}")
                lines.append(
                    "  (omni_analyze is a deprecated alias — prefer omni_impact)"
                )

                return "\n".join(lines)

            elif analysis == "graph":
                params: Dict[str, Any] = {"max_files": 50, "max_nodes": 30}
                if path:
                    params["path"] = path
                result = await make_request("GET", "/search/symbols/graph", params=params)
                if "error" in result:
                    if fmt == "json":
                        return _alias_json({
                            "ok": False, "error": f"Graph error: {result['error']}",
                        })
                    return f"❌ Graph error: {result['error']}"
                data = result.get("result", result)
                summary = data.get("summary", {})
                if fmt == "json":
                    return _alias_json({
                        "ok": True,
                        "analysis": "graph",
                        "path": path,
                        "summary": summary,
                        # audit-bundle.r14 (P2): stamp source/confidence
                        # parity with the callers/callees branch.
                        "source": "graph",
                        "confidence": "high" if summary.get("total_edges") else "low",
                        "next_actions": [
                            "Prefer omni_impact(symbol='<name>', format='json') "
                            "for symbol-scoped blast radius.",
                        ],
                    })
                return (
                    f"📊 Call graph{' for ' + path if path else ''}\n"
                    f"  Edges: {summary.get('total_edges', 0)}\n"
                    f"  Callers: {summary.get('total_callers', 0)}\n"
                    f"  Callees: {summary.get('total_callees', 0)}"
                )

            unknown = (
                f"Unknown analysis type: {analysis}. "
                "Use: impact, callers, callees, graph"
            )
            if fmt == "json":
                return _alias_json({"ok": False, "error": unknown})
            return f"❌ {unknown}"

        except Exception as e:
            if fmt == "json":
                return _alias_json({"ok": False, "error": f"Analysis failed: {e}"})
            return f"❌ Analysis failed: {e}"

    @mcp.tool()
    async def omni_memory(
        action: str = "search",
        query: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
        importance: int = 3,
        tags: Optional[Union[str, List[str]]] = None,
        file: Optional[str] = None,
        symbol: Optional[str] = None,
        task: Optional[str] = None,
        max_memories: int = 8,
        token_budget: int = 0,
        format: str = "json",
    ) -> str:
        """Interact with the project memory system.

        Actions:
          - search:    find relevant memories (requires ``query``)
          - store:     save a new memory (requires ``content`` + ``category``)
          - context:   startup context (recent progress, key learnings)
          - advisory:  multi-angle proactive recall for an edit task —
                       searches by file/symbol/task/error and dedupes.

        Categories: solution, learning, preference, mistake, architecture,
                   integration, debug, progress
        """
        is_json = (format or "json").lower() != "text"

        def _err(msg: str, **extra: Any) -> str:
            payload: Dict[str, Any] = {
                "ok": False,
                "action": action,
                "error": msg,
                "allowed_actions": list(_MEMORY_ALLOWED_ACTIONS),
                **extra,
            }
            payload.setdefault("next_actions", [
                f"omni_memory(action='{a}', ...)"
                for a in _MEMORY_ALLOWED_ACTIONS
            ])
            _stamp(payload, tool="omni_memory")
            return (
                json.dumps(payload, ensure_ascii=False, indent=2)
                if is_json
                else f"❌ {msg}"
            )

        try:
            # ---------- Validate action up front -------------------------
            if action not in _MEMORY_ALLOWED_ACTIONS:
                return _err(
                    f"Unknown action: {action}. "
                    f"Use: {', '.join(_MEMORY_ALLOWED_ACTIONS)}",
                )

            if tags is None:
                tag_list: List[str] = []
            elif isinstance(tags, str):
                tag_list = [
                    t.strip() for t in tags.split(",") if t.strip()
                ]
            elif isinstance(tags, list):
                tag_list = [
                    str(t).strip() for t in tags if str(t).strip()
                ]
            else:
                return _err("tags must be a string or a list of strings")

            # ---------- search -------------------------------------------
            if action == "search":
                if not query:
                    return _err("query is required for memory search")
                result = await make_request("POST", "/memory/search", json={
                    "query": query,
                    "category": category,
                    "max_results": 10,
                    "min_score": 0.3,
                })
                backend_error = _backend_error_message(result)
                if backend_error:
                    return _err(f"Memory search error: {backend_error}")
                data = result.get("result", result) or {}
                rows = data.get("results", []) or []
                normalised = [_normalise_memory_row(r) for r in rows]
                payload: Dict[str, Any] = {
                    "ok": True,
                    "action": "search",
                    "query": query,
                    "category_filter": category,
                    "count": len(normalised),
                    "results": normalised,
                    "memories": normalised,  # alias for omni_context parity
                    "next_actions": _next_actions_for_memory(
                        action="search",
                        has_results=bool(normalised),
                        memory_id=None,
                        duplicate=False,
                        symbol=symbol,
                        file=file,
                        task=task,
                    ),
                }
                # Audit guard: surface a warning if any row came back
                # without an id — the API contract is that backend
                # responses always include id; if they don't, we want
                # an explicit signal rather than silent null.
                missing_ids = [
                    i for i, r in enumerate(normalised)
                    if r.get("memory_id") is None
                ]
                if missing_ids:
                    payload["warnings"] = [
                        f"backend_missing_ids:{len(missing_ids)} of "
                        f"{len(normalised)} rows lack memory_id"
                    ]
                _stamp(payload, tool="omni_memory")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                if not normalised:
                    return f"🧠 No memories found for '{query}'"
                lines = [
                    f"🧠 {len(normalised)} memory(ies) for '{query}'\n"
                ]
                for r in normalised:
                    lines.append(
                        f"  [{r.get('category', '?')}] "
                        f"id={r.get('memory_id')} "
                        f"(score={r.get('score', 0):.2f})"
                    )
                    lines.append(f"  {(r.get('content') or '')[:200]}")
                    if r.get("match_reason"):
                        lines.append(f"  📍 {r['match_reason']}")
                    lines.append("")
                return "\n".join(lines)

            # ---------- store --------------------------------------------
            if action == "store":
                if not content or not (content.strip() if isinstance(content, str) else True):
                    return _err(
                        "content and category are required for memory store",
                        missing=["content"],
                    )
                if not category:
                    return _err(
                        "content and category are required for memory store",
                        missing=["category"],
                    )

                # Build related_files heuristically: explicit file + any
                # file mentioned via symbol metadata.
                related_files: List[str] = []
                if file:
                    related_files.append(file)

                store_body: Dict[str, Any] = {
                    "category": category,
                    "content": content,
                    "importance": importance,
                    "tags": tag_list,
                    "related_files": related_files,
                    "context": {},
                }
                # Echo symbol/task into context so they're searchable later.
                if symbol:
                    store_body["context"]["symbol"] = symbol
                if task:
                    store_body["context"]["task"] = task
                if file:
                    store_body["context"]["file"] = file

                result = await make_request(
                    "POST", "/memory/store", json=store_body,
                )
                backend_error = _backend_error_message(result)
                if backend_error:
                    return _err(f"Memory store error: {backend_error}")
                data = result.get("result", result) or {}
                memory_id = _extract_memory_id(data)

                # Detect dedup (backend reuses the existing row by
                # content_fingerprint) via timestamp age heuristic.
                is_dup, dedup_reason = _is_dedup_response(data)

                payload = {
                    "ok": True,
                    "action": "store",
                    "memory_id": memory_id,
                    "id": memory_id,           # alias for back-compat
                    "category": category,
                    "importance": importance,
                    "tags": tag_list,
                    "file": _sanitize_public_path_ref(file) if file else file,
                    "symbol": _sanitize_public_path_text(symbol) if symbol else symbol,
                    "task": _sanitize_public_path_text(task) if task else task,
                    "content": _sanitize_public_path_text(content),
                    "duplicate": is_dup,
                    "existing_memory_id": memory_id if is_dup else None,
                    "deduplication_reason": dedup_reason,
                }
                payload["next_actions"] = _next_actions_for_memory(
                    action="store",
                    has_results=False,
                    memory_id=memory_id,
                    duplicate=is_dup,
                    symbol=symbol,
                    file=file,
                    task=task,
                )
                # If we couldn't pull a memory_id out of any field, that's
                # an audit failure — tell the caller explicitly so they
                # don't silently accept a null id (the original P0 bug).
                if memory_id is None:
                    payload["warnings"] = [
                        "backend_missing_id: store endpoint returned no "
                        "id field; cannot reference this memory later. "
                        "Retry omni_memory(action='search', query=...) to "
                        "locate the row."
                    ]
                _stamp(payload, tool="omni_memory")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                tag = " (duplicate, counters bumped)" if is_dup else ""
                return (
                    f"✅ Memory stored{tag}: id={memory_id} "
                    f"category={category} importance={importance}"
                )

            # ---------- context ------------------------------------------
            if action == "context":
                result = await make_request("GET", "/memory/context")
                backend_error = _backend_error_message(result)
                if backend_error:
                    return _err(f"Memory context error: {backend_error}")
                data = result.get("result", result) or {}
                sanitized_buckets = {
                    "recent_progress": [
                        _normalise_memory_row({"memory": r if isinstance(r, dict) else {}})
                        for r in (data.get("recent_progress") or [])
                    ],
                    "key_learnings": [
                        _normalise_memory_row({"memory": r if isinstance(r, dict) else {}})
                        for r in (data.get("key_learnings") or [])
                    ],
                    "user_preferences": [
                        _normalise_memory_row({"memory": r if isinstance(r, dict) else {}})
                        for r in (data.get("user_preferences") or [])
                    ],
                    "important_warnings": [
                        _normalise_memory_row({"memory": r if isinstance(r, dict) else {}})
                        for r in (data.get("important_warnings") or [])
                    ],
                }
                # Build the unified ``memories[]`` alias by flattening
                # the four buckets and normalising every row.
                buckets = (
                    ("recent_progress", sanitized_buckets["recent_progress"]),
                    ("key_learnings", sanitized_buckets["key_learnings"]),
                    ("user_preferences", sanitized_buckets["user_preferences"]),
                    ("important_warnings", sanitized_buckets["important_warnings"]),
                )
                memories_alias: List[Dict[str, Any]] = []
                for bucket_name, rows in buckets:
                    for r in rows:
                        norm = dict(r)
                        norm["match_reason"] = f"context:{bucket_name}"
                        memories_alias.append(norm)

                payload = {
                    "ok": True,
                    "action": "context",
                    "recent_progress": sanitized_buckets["recent_progress"],
                    "key_learnings": sanitized_buckets["key_learnings"],
                    "user_preferences": sanitized_buckets["user_preferences"],
                    "important_warnings": sanitized_buckets["important_warnings"],
                    "current_focus": _sanitize_public_path_text(
                        str(data.get("current_focus") or "")
                    ) if isinstance(data.get("current_focus"), str)
                    else data.get("current_focus"),
                    "next_priorities": [
                        _sanitize_public_path_text(item)
                        if isinstance(item, str) else item
                        for item in (data.get("next_priorities") or [])
                    ],
                    "memories": memories_alias,
                    "memory_count": len(memories_alias),
                    "next_actions": _next_actions_for_memory(
                        action="context",
                        has_results=bool(memories_alias),
                        memory_id=None,
                        duplicate=False,
                        symbol=symbol,
                        file=file,
                        task=task,
                    ),
                }
                _stamp(payload, tool="omni_memory")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                return _format_json(payload)

            # ---------- advisory -----------------------------------------
            if action == "advisory":
                if not (file or symbol or task or query):
                    return _err(
                        "advisory needs at least one of "
                        "file, symbol, task or query",
                    )

                # Drive the advisory off the same /memory/search backend
                # via _collect_advisory_payload — the shared helper that
                # omni_context also calls. This guarantees both tools
                # report the same memory_id / memory_count / synthesis.
                #
                # audit-bundle.r17 (P3): pass through the new
                # ``max_memories`` cap so advisory responses can stay
                # within an AI editor's budget. Default (8) keeps the
                # legacy behaviour; lower values trim from the bottom
                # of the relevance-sorted list.
                advisory_data = await _collect_advisory_payload(
                    symbol=symbol, file=file,
                    task=task, query=query,
                    max_memories=max(1, max_memories),
                )
                merged = advisory_data["memories"]
                safe_file = _sanitize_public_path_ref(file) if file else file
                safe_symbol = (
                    _sanitize_public_path_text(symbol) if symbol else symbol
                )
                safe_task = (
                    _sanitize_public_path_text(task or query)
                    if (task or query) else (task or query)
                )

                payload = {
                    "ok": True,
                    "action": "advisory",
                    "file": safe_file,
                    "symbol": safe_symbol,
                    "task": safe_task,
                    "advisory": {
                        "summary": advisory_data["summary"],
                        "action_items": advisory_data["action_items"],
                        "risks": advisory_data["risks"],
                        "referenced_memories": advisory_data["referenced_memories"],
                    },
                    "advisory_text": advisory_data["advisory_text"],
                    "referenced_memories": advisory_data["referenced_memories"],
                    "memory_count": advisory_data["memory_count"],
                    "memories": merged,  # full normalised rows for callers
                    "why_recalled": advisory_data["why_recalled"],
                    "confidence": advisory_data["confidence"],
                    "next_actions": _next_actions_for_memory(
                        action="advisory",
                        has_results=bool(merged),
                        memory_id=None,
                        duplicate=False,
                        symbol=symbol,
                        file=file,
                        task=task or query,
                    ),
                }
                # ---- audit-bundle.r17 (P3): budget honesty for advisory.
                # Estimate the token weight of the assembled response and
                # surface ``token_estimate`` + ``truncated`` so AI
                # editors can budget for advisory just like they do for
                # omni_search / omni_read. When ``token_budget`` is set
                # and exceeded, drop ``memories`` rows from the tail
                # (lowest score) until we fit; the ``advisory`` /
                # ``referenced_memories`` blocks stay intact because
                # those are the synthesis layer, not the raw rows.
                est_text = json.dumps(payload, ensure_ascii=False, default=str)
                token_estimate = _approx_token_count(est_text)
                truncation_reasons: List[str] = []
                was_truncated = False
                if token_budget and token_estimate > token_budget:
                    raw_mem = payload.get("memories") or []
                    mem_list: List[Any] = (
                        list(raw_mem) if isinstance(raw_mem, list) else []
                    )
                    original_count = len(mem_list)
                    while token_estimate > token_budget and len(mem_list) > 1:
                        mem_list.pop()
                        payload["memories"] = mem_list
                        est_text = json.dumps(
                            payload, ensure_ascii=False, default=str,
                        )
                        token_estimate = _approx_token_count(est_text)
                    dropped = original_count - len(mem_list)
                    if dropped > 0:
                        was_truncated = True
                        truncation_reasons.append(
                            f"memories_capped:{len(mem_list)} of "
                            f"{original_count} (token_budget={token_budget})"
                        )
                payload["max_memories"] = max(1, max_memories)
                payload["token_estimate"] = token_estimate
                payload["truncated"] = bool(
                    was_truncated
                    or (token_budget and token_estimate > token_budget)
                )
                if token_budget:
                    payload["token_budget"] = token_budget
                if truncation_reasons:
                    payload["truncation_reasons"] = truncation_reasons
                _stamp(payload, tool="omni_memory")
                if is_json:
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                symbol = safe_symbol
                file = safe_file
                task = safe_task
                query = None
                # Text rendering keeps the emoji header for humans —
                # JSON path stays clean.
                if not merged:
                    return "🧠 No advisory available for the given inputs."
                lines = [
                    f"🧠 Advisory  symbol={symbol!r}  file={file!r}  "
                    f"task={(task or query)!r}",
                    f"   confidence={advisory_data['confidence']}  "
                    f"memories={len(merged)}",
                    "",
                    advisory_data["summary"],
                ]
                if advisory_data["action_items"]:
                    lines.append("")
                    lines.append("Action items:")
                    for i, item in enumerate(advisory_data["action_items"], 1):
                        lines.append(f"  {i}. {item}")
                if advisory_data["risks"]:
                    lines.append("")
                    lines.append("Risks:")
                    for i, risk in enumerate(advisory_data["risks"], 1):
                        lines.append(f"  {i}. {risk}")
                return "\n".join(lines)

            # Defensive — should never reach here because the action
            # validation up front catches unknown actions.
            return _err(
                f"Unknown action: {action}. "
                f"Use: {', '.join(_MEMORY_ALLOWED_ACTIONS)}",
            )

        except Exception as e:
            return _err(f"Memory operation failed: {e}")

    @mcp.tool()
    async def omni_context(
        file: Optional[str] = None,
        symbol: Optional[str] = None,
        task: Optional[str] = None,
        token_budget: int = 4000,
        format: str = "json",
        max_files: int = 5,
    ) -> str:
        """Get the minimum-necessary context for a coding task.

        At least one of ``file`` / ``symbol`` / ``task`` must be supplied.
        omni_context is a *composer*: it calls the focused tools
        (search/read/impact/diagnostics/memory) on the AI editor's
        behalf, fits the result into ``token_budget``, and reports both
        what it found and what it skipped.

        Modes:
          * ``symbol``   — resolve the symbol via the symbol index, then
                           call references + impact + memory advisory +
                           diagnostics on the file that defines it.
          * ``file``     — outline the file, then run diagnostics + look
                           for related references defined in it.
          * ``task``     — semantic + lexical search on the task string;
                           lexical hits go through ``omni_search(symbol)``
                           so a query mentioning ``_detect_mode`` lands
                           on the actual function rather than fuzz.

        Response (contract: ``context.v2``)::

            {
              "ok":                 bool,
              "task" / "file" / "symbol":   echoed inputs,
              "symbol_resolution":  "found" | "not_found" | "ambiguous" | "n/a",
              "confidence":         "high" | "medium" | "low",
              "token_budget":       int,
              "budget":             int,           # legacy alias
              "budget_utilization": float (0..1),
              "context": {
                  "primary_symbols":  [...],
                  "related_files":    [...],
                  "diagnostics":      [...],
                  "memories":         [...],
                  "recent_changes":   [...],
                  "references":       [...],
              },
              "diagnostics_status": {ran: bool, source: str, reason: str?},
              "why_selected":       [str, ...],   # one bullet per accepted item
              "truncation_reasons": [str, ...],   # explicit reasons truncated=true
              "truncated":          bool,
              "next_actions":       [str, ...],
              "token_estimate":     int,
              "handler_version":    str,
              "contract_version":   "context.v2",
            }
        """
        # ----- 0. Empty-input guard -----------------------------------
        if not (file or symbol or task):
            err = {
                "ok": False,
                "error": (
                    "omni_context needs at least one of "
                    "file=, symbol=, or task=."
                ),
                "suggested_next_action": (
                    "Supply task='...' for exploration, file='...' to "
                    "anchor on a file, or symbol='...' to gather "
                    "callers/refs/impact for that symbol."
                ),
                "next_actions": [
                    "omni_search(query='...', mode='auto') if you don't have a symbol yet",
                    "omni_status() to confirm the runtime contract",
                ],
            }
            _stamp(err, tool="omni_context")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() == "json"
                else f"❌ {err['error']}"
            )

        fmt = (format or "json").lower()
        if file and _path_looks_unsafe(file):
            safe_file = _safe_rejected_file_label(file)
            err = {
                "ok": False,
                "file": safe_file,
                "symbol": symbol,
                "task": task,
                "error": (
                    "Path access denied: omni_context only accepts "
                    "workspace-relative paths inside the active workspace."
                ),
                "file_status": "path_rejected",
                "confidence": "low",
                "context": {
                    "primary_symbols": [],
                    "related_files": [],
                    "diagnostics": [],
                    "memories": [],
                    "recent_changes": [],
                    "references": [],
                },
                "diagnostics_status": {
                    "ran": False,
                    "source": "path_guard",
                    "reason": "rejected unsafe file path before analysis",
                },
                "memory_status": {
                    "ran": False,
                    "source": "memory.v2.advisory",
                    "reason": "skipped: unsafe file path",
                },
                "symbol_resolution": "n/a",
                "token_budget": token_budget,
                "budget": token_budget,
                "budget_utilization": 0.0,
                "why_selected": [
                    "file path rejected by workspace path guard before context gathering"
                ],
                "truncation_reasons": [],
                "truncated": False,
                "token_estimate": 0,
                "next_actions": _path_guard_next_actions(file),
            }
            _stamp(err, tool="omni_context")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if fmt == "json"
                else (
                    "Context request rejected: use a workspace-relative path "
                    "inside the active workspace."
                )
            )
        if file and symbol and fmt == "json":
            fast_payload = _build_fast_file_symbol_context_payload(
                file=file,
                symbol=symbol,
                task=task,
                token_budget=token_budget,
                max_files=max_files,
            )
            if fast_payload is not None:
                _stamp(fast_payload, tool="omni_context")
                return json.dumps(
                    fast_payload,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        freshness_block, freshness_meta = await _analysis_freshness_gate(
            tool="omni_context",
            fmt=fmt,
            allow_exact=True,
        )
        if freshness_block is not None:
            return freshness_block
        analysis_request = _request_with_freshness_headers(freshness_meta)

        try:
            # ----- 1. Token budget bookkeeping -------------------------
            ctx: Dict[str, Any] = {
                "primary_symbols": [],
                "related_files": [],
                "diagnostics": [],
                "memories": [],
                "recent_changes": [],
                "references": [],
                "definition": {
                    "available": False,
                    "source": "",
                    "reason": "no symbol resolved",
                },
                "local_neighborhood": {
                    "available": False,
                    "source": "",
                    "reason": "no anchor file",
                },
                "semantic": {
                    "available": False,
                    "source": "semantic",
                    "reason": "not requested",
                },
                "graph": {
                    "available": False,
                    "source": "graph",
                    "reason": "not requested",
                },
            }
            why: List[str] = []
            truncation_reasons: List[str] = []
            spent = 0
            truncated = False

            def _spend(n: int, *, section: str) -> bool:
                """Return True iff we can afford ``n`` more tokens.

                When the budget is exhausted we mark truncation and
                record which section was cut so the response says *why*.
                """
                nonlocal spent, truncated
                if token_budget > 0 and spent + n > token_budget:
                    truncated = True
                    reason = f"skipped:{section} (budget)"
                    if reason not in truncation_reasons:
                        truncation_reasons.append(reason)
                    return False
                spent += n
                return True

            def _add_truncation_reason(reason: str) -> None:
                nonlocal truncated
                truncated = True
                if reason not in truncation_reasons:
                    truncation_reasons.append(reason)

            # ----- 2. Symbol resolution (symbol mode) ------------------
            symbol_resolution = "n/a"
            symbol_def_file: Optional[str] = None
            symbol_def_signature: str = ""
            symbol_def_line: int = 0
            ambiguous_defs: List[Dict[str, Any]] = []

            if symbol:
                try:
                    sym_results, _total = await _run_symbol(
                        analysis_request, symbol, None, max_results=5,
                    )
                except Exception as exc:
                    logger.debug("omni_context symbol resolve failed: %s", exc)
                    sym_results = []

                exact = [
                    r for r in sym_results
                    if (r.get("symbol_name") or "") == symbol
                ]
                if not exact:
                    symbol_resolution = "not_found"
                    why.append(
                        f"symbol:'{symbol}' not found via symbol index"
                    )
                elif len(exact) > 1:
                    symbol_resolution = "ambiguous"
                    ambiguous_defs = exact
                    # Use the first one as primary anchor.
                    first = exact[0]
                    symbol_def_file = first.get("file_path") or ""
                    symbol_def_signature = first.get("signature") or ""
                    symbol_def_line = (
                        first.get("line_start") or first.get("line_number") or 0
                    )
                    why.append(
                        f"symbol:{len(exact)} definitions found (ambiguous)"
                    )
                else:
                    symbol_resolution = "found"
                    first = exact[0]
                    symbol_def_file = (first.get("file_path") or "").replace("\\", "/")
                    symbol_def_signature = first.get("signature") or ""
                    symbol_def_line = (
                        first.get("line_start") or first.get("line_number") or 0
                    )
                    why.append(
                        f"symbol:{symbol} resolved to {symbol_def_file}:{symbol_def_line}"
                    )
                    # Surface the definition row at the top of primary_symbols.
                    def_row = {
                        "name": symbol,
                        "kind": "definition",
                        "file": symbol_def_file,
                        "lines": [
                            symbol_def_line,
                            first.get("line_end") or symbol_def_line,
                        ],
                        "signature": (symbol_def_signature or "")[:160],
                    }
                    n = _approx_token_count(json.dumps(def_row))
                    if _spend(n, section="primary_symbols"):
                        ctx["primary_symbols"].append(def_row)
                    ctx["definition"] = {
                        "available": True,
                        "source": first.get("source") or "symbol_index",
                        "name": symbol,
                        "file": symbol_def_file,
                        "line": symbol_def_line,
                        "signature": (symbol_def_signature or "")[:160],
                    }

            # The "anchor file" is the explicit file= param OR the
            # symbol's resolved file. Used for diagnostics + recent
            # changes + outline.
            anchor_file = (
                file or symbol_def_file
                if (file or symbol_def_file) else None
            )

            # ----- 3. Outline of the anchor file -----------------------
            outline_symbols: List[Dict[str, Any]] = []
            if anchor_file:
                odata = _build_local_outline_payload(anchor_file)
                if odata is None:
                    try:
                        outline = await make_request("POST", "/read", params={
                            "file_path": anchor_file,
                            "mode": "outline",
                            "with_line_numbers": True,
                        })
                        odata = outline.get("result") or {}
                    except Exception as exc:
                        logger.debug("omni_context outline failed: %s", exc)
                        odata = {}
                outline_symbols = (odata or {}).get("symbols", []) or []
                ctx["local_neighborhood"] = {
                    "available": bool(odata),
                    "source": (odata or {}).get("source") or "backend",
                    "file": anchor_file,
                    "outline_symbol_count": len(outline_symbols),
                    "reason": (
                        "outline available"
                        if odata
                        else "outline unavailable; deterministic file context limited"
                    ),
                }

                # Only emit outline when caller asked for the file
                # explicitly OR when symbol mode needs it; the goal is
                # to keep symbol-mode responses lean.
                if file:
                    section_added = 0
                    for s in outline_symbols[:30]:
                        row = {
                            "name": s.get("name"),
                            "kind": s.get("kind") or s.get("type"),
                            "lines": s.get("lines") or [
                                s.get("line_start", 0),
                                s.get("line_end", 0),
                            ],
                            "signature": (s.get("signature") or "")[:120],
                            "parent": s.get("parent"),
                        }
                        n = _approx_token_count(json.dumps(row))
                        if not _spend(n, section="primary_symbols"):
                            break
                        ctx["primary_symbols"].append(row)
                        section_added += 1
                    if section_added:
                        why.append(
                            f"outline:file={anchor_file} ({section_added} symbols, "
                            f"source={(odata or {}).get('source') or 'backend'})"
                        )

            # ----- 4. References (symbol mode) -------------------------
            ref_max = 10
            if symbol and symbol_resolution == "found":
                try:
                    ref_results, _ref_total, _ref_meta = await _run_references(
                        make_request, symbol, max_results=ref_max,
                    )
                except Exception as exc:
                    logger.debug("omni_context references failed: %s", exc)
                    ref_results = []

                added = 0
                for r in ref_results:
                    if r.get("kind") == "definition":
                        # Already accounted for in primary_symbols.
                        continue
                    row = {
                        "file": (r.get("file_path") or "").replace("\\", "/"),
                        "line": r.get("line_number") or r.get("line_start") or 0,
                        "kind": r.get("kind") or r.get("match_type") or "call",
                        "source": r.get("source") or "",
                        "confidence": r.get("confidence") or "",
                    }
                    n = _approx_token_count(json.dumps(row))
                    if not _spend(n, section="references"):
                        break
                    ctx["references"].append(row)
                    added += 1
                if added:
                    why.append(
                        f"references:{added} usages of {symbol} (capped at {ref_max})"
                    )
                if len(ref_results) > ref_max:
                    _add_truncation_reason(
                        f"references_capped:{ref_max}"
                    )

            # ----- 5. Impact (symbol mode) -----------------------------
            impact_payload: Dict[str, Any] = {}
            if symbol and symbol_resolution == "found":
                try:
                    risk_t = analysis_request("GET", "/graph/risk", params={
                        "symbol": symbol, "max_files": 200,
                    })
                    impact_t = analysis_request("GET", "/graph/impact", params={
                        "symbol": symbol, "depth": 1, "max_files": 200,
                    })
                    tests_t = analysis_request("GET", "/graph/related-tests", params={
                        "symbol": symbol, "max_files": 200,
                    })
                    import asyncio
                    gathered = await asyncio.gather(
                        risk_t, impact_t, tests_t, return_exceptions=True,
                    )
                    risk_raw: Any = gathered[0]
                    impact_raw: Any = gathered[1]
                    tests_raw: Any = gathered[2]

                    def _safe(r: Any) -> Dict[str, Any]:
                        if isinstance(r, Exception):
                            return {}
                        if isinstance(r, dict):
                            inner = r.get("result", r)
                            if isinstance(inner, dict):
                                return inner
                        return {}

                    risk_d = _safe(risk_raw)
                    impact_d = _safe(impact_raw)
                    tests_d = _safe(tests_raw)

                    callers = (
                        impact_d.get("dependent_symbols")
                        or impact_d.get("dependents")
                        or []
                    )
                    callees = (
                        impact_d.get("affected_symbols")
                        or impact_d.get("affected")
                        or []
                    )
                    files_inv = impact_d.get("files_involved") or []
                    suggested_tests = tests_d.get("test_files") or []
                    suggested_cmds = tests_d.get("suggested_commands") or []
                    graph_has_edges = bool(
                        callers or callees or (impact_d.get("files_count") or 0)
                    )
                    risk_value = risk_d.get("risk") or "unknown"
                    risk_reasons_value = risk_d.get("reasons") or []
                    graph_reason = "graph available"
                    if not graph_has_edges:
                        risk_value = "unknown"
                        risk_reasons_value = []
                        graph_reason = (
                            "graph_index_unavailable_or_empty; returned "
                            "deterministic symbol context only"
                        )

                    impact_payload = {
                        "risk": risk_value,
                        "risk_reasons": risk_reasons_value,
                        "callers": list(callers)[:10],
                        "callees": list(callees)[:10],
                        "files_count": impact_d.get("files_count") or 0,
                        "suggested_tests": suggested_tests[:5],
                        "suggested_commands": suggested_cmds[:3],
                        "source": (
                            "graph"
                            if graph_has_edges
                            else "graph+deterministic_fallback"
                        ),
                        "symbol_resolution": "found",
                        "capabilities_missing": (
                            [] if graph_has_edges else ["impact.graph"]
                        ),
                        "reason": graph_reason,
                    }
                    ctx["graph"] = {
                        "available": graph_has_edges,
                        "source": "graph",
                        "reason": graph_reason,
                        "callers_count": len(callers),
                        "callees_count": len(callees),
                        "files_count": impact_d.get("files_count") or 0,
                    }
                    n = _approx_token_count(json.dumps(impact_payload))
                    if _spend(n, section="impact"):
                        why.append(
                            f"impact:{impact_payload['risk']} risk, "
                            f"{len(callers)} callers, "
                            f"{len(suggested_tests)} suggested tests"
                        )
                    else:
                        impact_payload = {
                            "risk": impact_payload["risk"],
                            "callers_count": len(callers),
                            "callees_count": len(callees),
                            "suggested_tests_count": len(suggested_tests),
                            "note": "impact details skipped due to token budget",
                            "source": impact_payload["source"],
                            "symbol_resolution": "found",
                            "capabilities_missing": impact_payload[
                                "capabilities_missing"
                            ],
                        }
                    # Promote impact's files_involved into related_files
                    # (capped) and impact's suggested_tests into the
                    # references-friendly slot. This is what makes the
                    # composer feel like one call.
                    for fp in files_inv[:max_files]:
                        fp_n = (fp or "").replace("\\", "/")
                        if not fp_n or fp_n == anchor_file:
                            continue
                        row = {
                            "file": fp_n,
                            "symbol": "",
                            "score": 1.0,
                            "reason": "impact:files_involved",
                        }
                        rsize = _approx_token_count(json.dumps(row))
                        if not _spend(rsize, section="related_files"):
                            break
                        ctx["related_files"].append(row)
                except Exception as exc:
                    logger.debug("omni_context impact failed: %s", exc)
                    ctx["graph"] = {
                        "available": False,
                        "source": "graph",
                        "reason": "graph lookup failed: "
                        + _sanitize_error_text(str(exc)),
                    }
            ctx["impact"] = impact_payload

            # ----- 6. Diagnostics (file or symbol-resolved file) ------
            diagnostics_status: Dict[str, Any] = {
                "ran": False,
                "source": "",
                "reason": "no anchor file",
            }
            if anchor_file:
                try:
                    diag_payload = await _collect_diagnostics_payload(
                        file=anchor_file,
                        severity="all",
                        sources="guard,lsp",
                    )
                    if diag_payload.get("ok"):
                        diagnostics_status = {
                            "ran": True,
                            "source": diag_payload.get("source") or "guard+lsp",
                            "local_first": diag_payload.get("local_first"),
                            "local_authority": diag_payload.get("local_authority"),
                            "tools_run": diag_payload.get("tools_run") or [],
                            "tools_skipped": diag_payload.get("tools_skipped") or [],
                            "total_count": diag_payload.get("total_count") or 0,
                        }
                        diags = diag_payload.get("diagnostics") or []
                        # Errors first, drop info noise.
                        diags.sort(key=lambda d: {
                            "error": 0, "warning": 1, "warn": 1,
                        }.get((d.get("severity") or "").lower(), 2))
                        for d in diags:
                            sev = (d.get("severity") or "").lower()
                            if sev == "info":
                                continue
                            row = {
                                "source": d.get("source") or d.get("tool"),
                                "severity": sev,
                                "line": d.get("line"),
                                "rule": d.get("rule") or d.get("code") or "",
                                "message": (d.get("message") or "")[:160],
                            }
                            n = _approx_token_count(json.dumps(row))
                            if not _spend(n, section="diagnostics"):
                                break
                            ctx["diagnostics"].append(row)
                            if sev == "error":
                                why.append(
                                    f"diagnostic:error L{d.get('line')} "
                                    f"{(d.get('message') or '')[:60]}"
                                )
                    else:
                        diagnostics_status = {
                            "ran": False,
                            "source": "guard+lsp",
                            "reason": diag_payload.get("error")
                            or "diagnostics service returned ok=false",
                        }
                except Exception as exc:
                    logger.debug("omni_context diagnostics failed: %s", exc)
                    diagnostics_status = {
                        "ran": False,
                        "source": "guard+lsp",
                        "reason": f"diagnostics call raised: {exc}",
                    }

            # ----- 7. Recent git changes (always cheap) ----------------
            try:
                grsp = await make_request("GET", "/git/status")
                gdata = grsp.get("result") or {}
                status_payload = (
                    (gdata.get("data") or {}).get("status")
                    or gdata.get("status")
                    or gdata
                )
                modified = status_payload.get("modified_files") or []
                untracked = status_payload.get("untracked_files") or []
                staged = status_payload.get("staged_files") or []
                changed_files: List[str] = []
                for c in list(modified) + list(staged) + list(untracked):
                    if isinstance(c, str):
                        changed_files.append(c)
                    elif isinstance(c, dict):
                        path = c.get("path") or c.get("file") or c.get("name") or ""
                        if path:
                            changed_files.append(path)
                changed_files = [c.replace("\\", "/") for c in changed_files if c]
                norm_anchor = (anchor_file or "").replace("\\", "/")
                priority = []
                rest = []
                for c in changed_files:
                    if norm_anchor and (
                        c == norm_anchor
                        or c.split("/")[0:2] == norm_anchor.split("/")[0:2]
                    ):
                        priority.append(c)
                    else:
                        rest.append(c)
                ordered = priority + rest
                added = 0
                for c in ordered[:10]:
                    n = _approx_token_count(c)
                    if not _spend(n, section="recent_changes"):
                        break
                    ctx["recent_changes"].append(c)
                    added += 1
                if priority:
                    why.append(
                        f"git:{len(priority)} recent changes near {anchor_file}"
                    )
                elif added:
                    why.append(f"git:{added} recent workspace changes")
            except Exception as exc:
                logger.debug("omni_context git failed: %s", exc)

            # ----- 8. Task-driven related files (lexical + semantic) --
            if task:
                seen_files: set[str] = set()
                anchor_norm = (anchor_file or "").replace("\\", "/")

                def _accept_related(row: Dict[str, Any]) -> bool:
                    fp = (row.get("file") or "").replace("\\", "/")
                    if not fp or fp == anchor_norm or fp in seen_files:
                        return False
                    if len(ctx["related_files"]) >= max_files:
                        _add_truncation_reason(
                            f"related_files_capped:{max_files}"
                        )
                        return False
                    n = _approx_token_count(json.dumps(row))
                    if not _spend(n, section="related_files"):
                        return False
                    ctx["related_files"].append(row)
                    seen_files.add(fp)
                    return True

                # 8a. Lexical-boost: extract code-shaped tokens and
                # query symbol search for them. This is what fixes the
                # "modify search mode routing" → noise-only result.
                #
                # audit-bundle.r17 (P3): when omni_context is called
                # task-only (no symbol=, no file=) and the lexical
                # boost surfaces high-scoring symbol hits, promote the
                # best one (or two) of those into ``primary_symbols``
                # BEFORE feeding the rest into related_files. Round 8
                # found that under tight token budgets the response
                # had primary_symbols=[] while related_files burned 80%
                # of the budget on lower-value lexical rows; promoting
                # the top hit means the most actionable symbol is
                # always the first thing the AI editor sees.
                lex_terms = _extract_lexical_terms(task)
                lex_terms = lex_terms[:5]
                lex_added = 0
                primary_promoted = 0
                # Only promote when no symbol-mode primary already
                # exists — otherwise the explicit ``symbol=`` row keeps
                # priority.
                can_promote_primary = not ctx["primary_symbols"]
                for term in lex_terms:
                    try:
                        sym_hits, _t = await _run_symbol(
                            analysis_request, term, None, max_results=3,
                        )
                    except Exception:
                        sym_hits = []
                    for idx, h in enumerate(sym_hits[:2]):
                        # The first (highest-relevance) hit per term
                        # may be promoted to primary if (a) no symbol
                        # was given, (b) primary is still empty or has
                        # < 2 promotions yet, and (c) the relevance
                        # score is meaningful. Subsequent hits flow
                        # into related_files as before.
                        score = float(h.get("relevance_score") or 0)
                        if (
                            can_promote_primary
                            and idx == 0
                            and primary_promoted < 2
                            and score >= 0.3
                        ):
                            file_path = (
                                h.get("file_path") or ""
                            ).replace("\\", "/")
                            sym_name = h.get("symbol_name") or term
                            primary_row = {
                                "name": sym_name,
                                "kind": h.get("kind") or "definition",
                                "file": file_path,
                                "lines": [
                                    h.get("line_start") or 0,
                                    h.get("line_end") or 0,
                                ],
                                "signature": (h.get("signature") or "")[:120],
                                "score": score,
                                "reason": f"task→lexical:{term} (promoted)",
                            }
                            n = _approx_token_count(json.dumps(primary_row))
                            if _spend(n, section="primary_symbols"):
                                ctx["primary_symbols"].append(primary_row)
                                primary_promoted += 1
                                continue  # don't double-add to related_files
                            # Budget exhausted — fall through and try
                            # related_files instead, which has its own
                            # cap.
                        row = {
                            "file": (h.get("file_path") or "").replace("\\", "/"),
                            "symbol": h.get("symbol_name") or "",
                            "score": score,
                            "reason": f"task→lexical:{term}",
                        }
                        if _accept_related(row):
                            lex_added += 1
                if primary_promoted:
                    why.append(
                        f"task:promoted {primary_promoted} lexical hit(s) "
                        "to primary_symbols"
                    )
                if lex_added:
                    why.append(
                        f"task:lexical boost added {lex_added} "
                        f"hits from {len(lex_terms)} terms"
                    )

                # 8b. Semantic search.
                try:
                    srsp = await analysis_request("POST", "/search", json={
                        "query": task,
                        "search_type": "semantic",
                        "max_results": max_files * 2,
                    })
                    sdata = srsp.get("result") or {}
                    hits = sdata.get("results", []) or []
                    sem_added = 0
                    for h in hits:
                        row = {
                            "file": (h.get("file_path") or "").replace("\\", "/"),
                            "symbol": h.get("symbol_name") or "",
                            "score": float(h.get("relevance_score") or 0),
                            "reason": "task→semantic top hit",
                        }
                        if _accept_related(row):
                            sem_added += 1
                        if len(ctx["related_files"]) >= max_files:
                            break
                    if sem_added:
                        why.append(
                            f"task:{sem_added} related files via semantic"
                        )
                    if len(hits) > sem_added:
                        # Some semantic hits were filtered (anchor / dedup
                        # / cap); record as a truncation reason only when
                        # the cap was hit.
                        if len(ctx["related_files"]) >= max_files and len(hits) > max_files:
                            _add_truncation_reason(
                                f"related_files_capped:{max_files}"
                            )
                    ctx["semantic"] = {
                        "available": bool(hits),
                        "source": "semantic",
                        "reason": (
                            "semantic search returned task hits"
                            if hits
                            else "semantic search returned no hits"
                        ),
                        "hits": len(hits),
                        "accepted": sem_added,
                    }
                except Exception as exc:
                    logger.debug("omni_context task search failed: %s", exc)
                    ctx["semantic"] = {
                        "available": False,
                        "source": "semantic",
                        "reason": "semantic unavailable: "
                        + _sanitize_error_text(str(exc)),
                    }

            # ----- 9. Memory advisory ----------------------------------
            mem_query_parts: List[str] = []
            if symbol:
                mem_query_parts.append(symbol)
            if task:
                mem_query_parts.append(task)
            mem_query = " ".join(mem_query_parts) if mem_query_parts else (file or "")

            memory_status: Dict[str, Any] = {"ran": False, "reason": "no query"}
            if mem_query:
                # Pre-flight budget check: skip the whole memory section
                # explicitly if there's no headroom. This makes the
                # truncation_reason readable rather than silent.
                if token_budget > 0 and spent >= token_budget:
                    memory_status = {
                        "ran": False,
                        "source": "memory.v2.advisory",
                        "reason": "skipped due to budget",
                    }
                    _add_truncation_reason("skipped:memories (budget)")
                else:
                    try:
                        # Use the shared advisory pipeline so omni_context's
                        # memory section ships memory_id + accurate
                        # memory_count, matching omni_memory v2.
                        advisory_data = await _collect_advisory_payload(
                            symbol=symbol, file=anchor_file,
                            task=task, query=None,
                        )
                        recalled = advisory_data["memories"]
                        # Cap how many full rows we surface inside the
                        # composer to keep the budget honest. Action_items
                        # and risks ride along in the synthesis row below.
                        max_inline_rows = 5
                        added = 0
                        for m in recalled[:max_inline_rows]:
                            n = _approx_token_count(json.dumps(m))
                            if not _spend(n, section="memories"):
                                break
                            ctx["memories"].append(m)
                            added += 1
                        if added < len(recalled):
                            _add_truncation_reason(
                                f"memories_capped:{max_inline_rows}"
                            )
                        memory_status = {
                            "ran": True,
                            "source": "memory.v2.advisory",
                            "memory_count": advisory_data["memory_count"],
                            "synthesis_summary": advisory_data["summary"],
                            "action_items": advisory_data["action_items"],
                            "risks": advisory_data["risks"],
                            "confidence": advisory_data["confidence"],
                            "why_recalled": advisory_data["why_recalled"],
                        }
                        if memory_status.get("memory_count", 0) > 0:
                            why.append(
                                f"memory:advisory recalled "
                                f"{memory_status['memory_count']} memories "
                                f"({memory_status['confidence']} confidence)"
                            )
                    except Exception as exc:
                        logger.debug("omni_context memory failed: %s", exc)
                        memory_status = {
                            "ran": False,
                            "source": "memory.v2.advisory",
                            "reason": f"memory call raised: {exc}",
                        }

            # ----- 10. Confidence + symbol_resolution finalisation ----
            if symbol:
                if symbol_resolution == "found":
                    confidence = (
                        "high" if ctx["primary_symbols"] and (
                            ctx["references"] or impact_payload
                        )
                        else "medium"
                    )
                elif symbol_resolution == "ambiguous":
                    confidence = "medium"
                else:  # not_found
                    confidence = "low"
            elif file:
                confidence = "high" if outline_symbols else "low"
            elif task:
                # Lexical hit on a real symbol → high; semantic only → medium.
                if any(
                    r.get("reason", "").startswith("task→lexical:")
                    for r in ctx["related_files"]
                ):
                    confidence = "high"
                else:
                    confidence = "medium" if ctx["related_files"] else "low"
            else:
                confidence = "low"

            # Budget utilization + 80% truncation rule.
            budget_utilization = (
                spent / token_budget if token_budget > 0 else 0.0
            )
            if token_budget > 0 and budget_utilization >= 0.8 and not truncated:
                # Heuristic: even if no section was outright cut, an
                # ≥80% budget burn indicates we're squeezing.
                _add_truncation_reason(
                    f"budget_utilization:{budget_utilization:.2f}"
                )

            # Promote ambiguous-definition note if applicable.
            if symbol_resolution == "ambiguous" and ambiguous_defs:
                ctx["ambiguous_definitions"] = [
                    {
                        "file": (d.get("file_path") or "").replace("\\", "/"),
                        "line": d.get("line_start") or d.get("line_number") or 0,
                        "signature": (d.get("signature") or "")[:120],
                    }
                    for d in ambiguous_defs[:5]
                ]

            # ----- 11. next_actions -----------------------------------
            primary_file = None
            if ctx["related_files"]:
                primary_file = ctx["related_files"][0].get("file")
            next_actions = _next_actions_for_context(
                has_file=bool(file),
                has_symbol=bool(symbol),
                has_task=bool(task),
                symbol=symbol,
                file=file or symbol_def_file,
                primary_file=primary_file,
            )
            # Symbol-not-found gets a tighter next-action set.
            if symbol_resolution == "not_found":
                next_actions = [
                    f"omni_search(query='{symbol}', mode='symbol') "
                    f"to confirm the symbol exists",
                    f"omni_search(query='{symbol}', mode='auto') for fuzzy lookup",
                ]

            # ----- 12. Build payload ----------------------------------
            note: Optional[str] = None
            if symbol_resolution == "not_found":
                note = (
                    f"symbol '{symbol}' not found in workspace; "
                    "primary_symbols / references / impact are empty by design. "
                    "Use omni_search(mode='symbol') first to confirm the name."
                )
            elif symbol_resolution == "ambiguous":
                note = (
                    f"symbol '{symbol}' has {len(ambiguous_defs)} definitions; "
                    "see context.ambiguous_definitions for candidates."
                )

            payload: Dict[str, Any] = {
                "ok": True,
                "task": task,
                "file": file,
                "symbol": symbol,
                "symbol_resolution": symbol_resolution,
                "confidence": confidence,
                "token_budget": token_budget,
                "budget": token_budget,  # legacy alias for context.v1 callers
                "budget_utilization": round(budget_utilization, 3),
                "context": ctx,
                "diagnostics_status": diagnostics_status,
                "memory_status": memory_status,
                "why_selected": why,
                "truncation_reasons": truncation_reasons,
                "truncated": truncated,
                "token_estimate": spent,
                "next_actions": next_actions,
            }
            capabilities_used: List[str] = []
            capabilities_missing: List[str] = []
            if ctx.get("definition", {}).get("available"):
                capabilities_used.append("search.symbol_exact")
            elif symbol:
                capabilities_missing.append("search.symbol_exact")
            if ctx.get("local_neighborhood", {}).get("available"):
                capabilities_used.append("read.outline")
            elif anchor_file:
                capabilities_missing.append("read.outline")
            if ctx.get("references"):
                capabilities_used.append("search.references")
            if ctx.get("diagnostics"):
                capabilities_used.append("diagnostics")
            if ctx.get("graph", {}).get("available"):
                capabilities_used.append("impact.graph")
            elif symbol:
                capabilities_missing.append("impact.graph")
            if ctx.get("semantic", {}).get("available"):
                capabilities_used.append("search.semantic")
            elif task:
                capabilities_missing.append("search.semantic")
            payload["context_builder"] = "deterministic"
            payload["degraded"] = bool(capabilities_missing)
            payload["capabilities_used"] = sorted(set(capabilities_used))
            payload["capabilities_missing"] = sorted(set(capabilities_missing))
            if freshness_meta:
                payload.update(freshness_meta)
            if note:
                payload["note"] = note

            # ---------- audit-bundle.r15 (P2): file-existence guard ----
            # When the caller explicitly supplied ``file=`` but the file
            # could not be resolved (diagnostics reported "File not
            # found", primary_symbols is empty, and the outline produced
            # nothing), we must NOT return ok=true with stray memory
            # rows attached — that's the misleading-success pattern the
            # Round 5 audit flagged. Convert the response to ok=false +
            # top-level error + file_status, and replace next_actions
            # with recovery steps. Memory advisory is dropped to avoid
            # presenting unrelated lessons as if they applied.
            file_supplied = bool(file and file.strip())
            diag_reason = (diagnostics_status.get("reason") or "") if isinstance(
                diagnostics_status, dict
            ) else ""
            file_not_found = (
                "file not found" in diag_reason.lower()
                or "no such" in diag_reason.lower()
            )
            anchor_resolved = bool(anchor_file)
            if (
                file_supplied
                and file_not_found
                and not ctx.get("primary_symbols")
                and (not anchor_resolved or anchor_file == file)
            ):
                file_for_error = file or ""
                safe_file = _safe_rejected_file_label(file_for_error)
                safe_query = _safe_path_search_query(file_for_error)
                safe_query_lit = json.dumps(safe_query, ensure_ascii=False)
                payload["ok"] = False
                payload["file"] = safe_file
                payload["error"] = f"File not found: {safe_file}"
                payload["file_status"] = "not_found"
                # Drop memory rows so we don't surface unrelated lessons
                # under a failed call. Keep the slot present for shape
                # parity, but with an empty list and a status note.
                ctx["memories"] = []
                payload["memory_status"] = {
                    "ran": False,
                    "source": "memory.v2.advisory",
                    "memory_count": 0,
                    "reason": (
                        "skipped: anchor file does not exist; advisory "
                        "would be unrelated to the requested file."
                    ),
                }
                payload["next_actions"] = [
                    f"Check the workspace-relative path and retry: "
                    f"omni_read(file='{safe_file}', mode='outline', format='json').",
                    f"omni_search(query={safe_query_lit}, mode='text', format='json') "
                    "to locate a similar file.",
                    "omni_context(symbol='<name>', task='<intent>', "
                    "format='json') if you can anchor on a symbol instead.",
                ]
                payload["why_selected"] = [
                    f"file:{safe_file} requested but could not be resolved "
                    "(omni_context refused to fabricate context)."
                ]

            if (format or "json").lower() == "text":
                lines = [
                    f"📦 Context  task={task!r}  file={file!r}  "
                    f"symbol={symbol!r}  budget={token_budget}",
                    f"   resolution={symbol_resolution}  confidence={confidence}",
                ]
                if truncated:
                    lines.append(
                        f"   ⚠️ truncated at {spent}/{token_budget} tokens"
                    )
                    for tr_reason in truncation_reasons:
                        lines.append(f"      • {tr_reason}")
                if ctx["primary_symbols"]:
                    lines.append(
                        f"   📄 {len(ctx['primary_symbols'])} primary symbols"
                    )
                if ctx["references"]:
                    lines.append(
                        f"   🔗 {len(ctx['references'])} references"
                    )
                if impact_payload:
                    lines.append(
                        f"   💥 impact risk={impact_payload.get('risk')}, "
                        f"{len(impact_payload.get('callers', []))} callers"
                    )
                if ctx["diagnostics"]:
                    lines.append(
                        f"   ⚠️ {len(ctx['diagnostics'])} diagnostics"
                    )
                if ctx["related_files"]:
                    lines.append(
                        f"   📚 {len(ctx['related_files'])} related files"
                    )
                if ctx["recent_changes"]:
                    lines.append(
                        f"   🌿 {len(ctx['recent_changes'])} recent changes"
                    )
                if ctx["memories"]:
                    lines.append(f"   🧠 {len(ctx['memories'])} memories")
                if note:
                    lines.append(f"\n   ℹ️ {note}")
                if next_actions:
                    lines.append("\n   next_actions:")
                    for a in next_actions:
                        lines.append(f"   → {a}")
                if why:
                    lines.append("\n   why_selected:")
                    for w in why:
                        lines.append(f"   • {w}")
                _stamp(payload, tool="omni_context")
                return "\n".join(lines)
            _stamp(payload, tool="omni_context")
            return json.dumps(payload, ensure_ascii=False, indent=2)

        except Exception as e:
            err = {"ok": False, "error": f"omni_context failed: {e}"}
            _stamp(err, tool="omni_context")
            return (
                json.dumps(err, ensure_ascii=False, indent=2)
                if (format or "json").lower() == "json"
                else f"❌ Context gathering failed: {e}"
            )

    @mcp.tool()
    async def omni_edit(
        action: str = "preview",
        file: Optional[str] = None,
        patch: Optional[str] = None,
        content: Optional[str] = None,
        instructions: Optional[str] = None,
        session_id: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
        force_reason: Optional[str] = None,
        format: str = "text",
    ) -> str:
        """[deprecated alias] Use omni_patch for safe edits.

        Actions:
          - preview / validate / apply / rollback: delegate to omni_patch
          - ai_edit: LLM-driven edit (only when OMNICODE_LLM_ROUTER=true).
                     When ``dry_run=True`` the LLM still runs but no
                     write happens — the response carries a unified
                     diff under preview_diff so you can show the user.

        audit-bundle.r13 (P0 close): ``apply`` now runs the same
        ``_do_validate`` gate as ``omni_patch`` before any write. To
        bypass validation you must pass both ``force=True`` AND
        ``force_reason='...'``; the response then carries
        ``validation_bypassed=true`` and a ⚠️ warning as the first
        ``next_actions`` item — exactly the same audit trail
        ``omni_patch`` uses. The path guard introduced in r9 is
        unchanged.

        Kept so older MCP configs don't break. Pass ``format='json'``
        for the structured deprecated-alias envelope (which now lifts
        ``session_id`` / ``rollback_available`` / ``validation_passed``
        / ``validation_bypassed`` / ``force_reason`` / diff fields to
        the top level for shape-parity with ``omni_patch``).
        """
        fmt = (format or "text").lower()
        # ``content`` is the modern param name; ``patch`` is the legacy one.
        # Prefer content when both are supplied.
        edit_content = content if content is not None else patch

        def _alias_text(ok: bool, msg: str) -> str:
            mark = "✅" if ok else "❌"
            return (
                f"{mark} {action}: {msg}\n"
                f"   (omni_edit is a deprecated alias — prefer omni_patch)"
            )

        def _alias_json(payload: Dict[str, Any]) -> str:
            return json.dumps(
                _alias_envelope("omni_edit", payload),
                ensure_ascii=False, default=str,
            )

        def _llm_edit_disabled() -> Tuple[bool, Dict[str, Any]]:
            import os as _os

            llm_mode = (
                _os.environ.get("OMNICODE_LLM_MODE") or "off"
            ).strip().lower()
            router_raw = _os.environ.get("OMNICODE_LLM_ROUTER")
            router_enabled = not (
                router_raw is not None
                and router_raw.strip().lower() in {"0", "false", "no", "off"}
            )
            disabled = llm_mode == "off" or not router_enabled
            return disabled, {
                "llm_mode": llm_mode,
                "llm_router_enabled": router_enabled,
            }

        try:
            if action == "ai_edit":
                disabled, llm_state = _llm_edit_disabled()
                if disabled:
                    payload = {
                        "ok": False,
                        "action": "ai_edit",
                        "error": "LLM editing is disabled",
                        "llm_disabled": True,
                        **llm_state,
                        "next_actions": [
                            "Use omni_patch(action='preview', file='...', "
                            "content='...', format='json') for a deterministic edit.",
                            "Set OMNICODE_LLM_MODE to local/remote/auto and "
                            "OMNICODE_LLM_ROUTER=true only if LLM editing is intended.",
                        ],
                    }
                    if fmt == "json":
                        return _alias_json(payload)
                    return "ERROR ai_edit: LLM editing is disabled"

            # ---------------------------------------------------------------
            # Path guard — applies to every action that takes a file and may
            # touch disk (ai_edit / preview / validate / apply). Runs BEFORE
            # any make_request so a bad path never reaches the backend and
            # never creates a session. Reuses the exact same helper +
            # ValueError as omni_patch v2.
            # ---------------------------------------------------------------
            if action in ("ai_edit", "preview", "validate", "apply") and file:
                try:
                    _resolve_workspace_path(file)
                except ValueError as guard_exc:
                    if fmt == "json":
                        return _alias_json(
                            _alias_path_guard_error("omni_edit", file, guard_exc)
                        )
                    return _alias_text(
                        False,
                        f"path-guard: {guard_exc} "
                        "(use omni_patch with a workspace-relative path)",
                    )

            # =====================================================
            #                     ai_edit branch
            # =====================================================
            if action == "ai_edit":
                if not file or not instructions:
                    err = "file and instructions are required for ai_edit"
                    if fmt == "json":
                        return _alias_json({
                            "ok": False, "action": "ai_edit", "error": err,
                            "next_actions": [
                                "omni_context(task='...', format='json') first to anchor",
                                "omni_patch(action='preview', file='...', content='...', "
                                "format='json') for a deterministic edit",
                            ],
                        })
                    return f"❌ {err}"
                try:
                    result = await make_request("POST", "/edit", json={
                        "target_file": file,
                        "instructions": instructions,
                        "code_edit": edit_content or "#",
                        "save_to_file": not dry_run,
                        "dry_run": dry_run,
                    })
                except Exception as exc:
                    err = f"ai_edit backend call failed: {exc}"
                    if fmt == "json":
                        return _alias_json({
                            "ok": False, "action": "ai_edit", "error": err,
                            "next_actions": [
                                "Enable OMNICODE_LLM_ROUTER=true if not already, or",
                                "Use the safe deterministic workflow: omni_context + "
                                "omni_patch preview/validate/apply.",
                            ],
                        })
                    return f"❌ {err}"
                if isinstance(result, dict) and "error" in result:
                    err = f"ai_edit error: {result['error']}"
                    if fmt == "json":
                        return _alias_json({
                            "ok": False, "action": "ai_edit",
                            "file": file, "error": err,
                            "next_actions": [
                                "ai_edit may be disabled — set OMNICODE_LLM_ROUTER=true "
                                "or use omni_patch(action='preview'/'validate'/'apply') "
                                "for a deterministic edit.",
                            ],
                        })
                    return f"❌ {err}"
                data = (
                    result.get("result", result)
                    if isinstance(result, dict) else {}
                )
                success = bool(data.get("success", False))

                # ---- dry_run path: never writes; emit suggested content
                # plus a preview diff. JSON path is required by r13.
                if dry_run:
                    diff = data.get("preview_diff", "") or ""
                    summary = data.get("preview_summary", {}) or {}
                    diff_lines = diff.splitlines()
                    diff_truncated = len(diff_lines) > 80
                    diff_show = (
                        "\n".join(diff_lines[:80])
                        if diff_truncated else diff
                    )
                    a = summary.get("lines_added", 0)
                    r_lines = summary.get("lines_removed", 0)
                    no_changes = bool(summary.get("no_changes"))
                    if fmt == "json":
                        return _alias_json({
                            "ok": True,
                            "action": "ai_edit",
                            "file": file,
                            "dry_run": True,
                            "no_changes": no_changes,
                            "lines_added": a,
                            "lines_removed": r_lines,
                            "diff": diff_show,
                            "diff_truncated": diff_truncated,
                            "diff_total_lines": len(diff_lines),
                            "suggested_content": data.get("suggested_content"),
                            "next_actions": [
                                "Re-run with dry_run=False to apply (NOT recommended).",
                                "Prefer omni_patch(action='preview', file='%s', "
                                "content=<suggested_content>, format='json') for a "
                                "deterministic preview, then validate, then apply." % file,
                            ],
                        })
                    if no_changes:
                        return f"⚪ Dry run: LLM produced no changes for {file}"
                    tail = (
                        f"\n... ({len(diff_lines) - 80} more diff lines)"
                        if diff_truncated else ""
                    )
                    return (
                        f"📋 Dry-run preview for {file}\n"
                        f"   +{a} / -{r_lines} lines (no write performed)\n\n"
                        f"{diff_show}{tail}\n\n"
                        f"   Re-run with dry_run=False to apply, or "
                        f"omni_patch(action='preview', file=…, content=…) "
                        f"to render externally."
                    )

                # ---- non-dry-run ai_edit. We don't auto-write through
                # this path in r13: ai_edit doesn't go through the
                # patch.v2 validate gate, and re-routing it would be a
                # bigger change than this release allows. Force JSON
                # callers down the safer path.
                if fmt == "json":
                    return _alias_json({
                        "ok": success,
                        "action": "ai_edit",
                        "file": file,
                        "message": data.get("message", ""),
                        "result": data,
                        "next_actions": [
                            "⚠️ ai_edit non-dry-run does NOT run the patch.v2 "
                            "validate gate. Prefer omni_patch(action='validate' "
                            "→ 'apply', file='%s', content=<llm_output>, "
                            "format='json') for the safety contract." % file,
                            "If you accept the risk, run with dry_run=True first "
                            "to inspect the diff.",
                        ],
                    })
                if success:
                    score = data.get("quality_score", 0)
                    sid = data.get("edit_session_id")
                    line = f"✅ Edit applied to {file} (quality={score:.2f})"
                    if sid:
                        line += (
                            f"\n   session_id: {sid}\n"
                            f"   To undo: omni_patch(action='rollback', "
                            f"session_id='{sid}')"
                        )
                    return line
                analysis = data.get("failure_analysis", {}) or {}
                stage = analysis.get("stage", "?")
                reason = analysis.get(
                    "root_cause", analysis.get("failure_reasons", "unknown"),
                )
                return f"❌ Edit failed at stage '{stage}': {reason}"

            # =====================================================
            #          preview / validate / apply / rollback
            # =====================================================
            if action == "preview":
                if not file or edit_content is None:
                    msg = "omni_edit preview needs both file and content."
                    return _alias_json({"ok": False, "error": msg}) if fmt == "json" else f"❌ {msg}"
                raw = await make_request("POST", "/patch/preview", json={
                    "file_path": file, "content": edit_content,
                })
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = bool(data.get("success", False))
                msg = data.get("message", "")
                if fmt == "json":
                    # Lift the patch.v2 preview shape to the top level so
                    # callers built against omni_patch can consume the
                    # alias response with the same field paths.
                    diff = data.get("diff", "") or ""
                    diff_lines = diff.splitlines()
                    payload = {
                        "ok": ok,
                        "action": "preview",
                        "file": file,
                        "message": msg,
                        "lines_added": data.get("lines_added", 0),
                        "lines_removed": data.get("lines_removed", 0),
                        "diff": diff,
                        "diff_truncated": len(diff_lines) > 80,
                        "diff_total_lines": len(diff_lines),
                        "newline_normalized": False,
                        "result": data,
                        "next_actions": [
                            "Prefer omni_patch(action='preview', ...) directly — "
                            "omni_edit is a deprecated alias.",
                        ],
                    }
                    # ---- audit-bundle.r14 (P1): when the backend reports
                    # failure (e.g. File not found), surface the message
                    # under the canonical top-level ``error`` key. The
                    # original ``message`` is kept for back-compat.
                    if not ok and msg:
                        payload["error"] = msg
                        # next_actions tailored to common preview failures
                        low = msg.lower()
                        recovery: List[str] = []
                        if "not found" in low or "no such" in low:
                            recovery = [
                                "Confirm the file exists with omni_read(file='%s', "
                                "mode='outline', format='json'). For a new file, "
                                "use omni_patch(action='apply', ...) directly — "
                                "preview requires an existing target." % file,
                                "omni_search(query='%s', mode='text', format='json') "
                                "to locate the file." % file,
                            ]
                        elif "permission" in low or "denied" in low:
                            recovery = [
                                "Re-check workspace permissions on '%s'." % file,
                            ]
                        if recovery:
                            payload["next_actions"] = recovery + payload["next_actions"]
                    return _alias_json(payload)
                return _alias_text(ok, msg)

            if action == "validate":
                if not file or edit_content is None:
                    msg = "omni_edit validate needs both file and content."
                    return _alias_json({"ok": False, "error": msg}) if fmt == "json" else f"❌ {msg}"
                v = await _do_validate(file, edit_content)
                if fmt == "json":
                    return _alias_json({
                        "ok": v["validation_passed"],
                        "action": "validate",
                        "file": file,
                        "message": v["message"],
                        "validation_passed": v["validation_passed"],
                        "checks": v["checks"],
                        "counts": v["counts"],
                        "tools_run": v["tools_run"],
                        "tools_skipped": v["tools_skipped"],
                        "source": v["source"],
                        "next_actions": (
                            [
                                "Prefer omni_patch(action='validate', ...) directly — "
                                "omni_edit is a deprecated alias.",
                                "If validation passed, you can apply via "
                                "omni_patch(action='apply', file=..., content=...).",
                            ]
                            if v["validation_passed"]
                            else [
                                "Fix the listed errors and re-validate.",
                                "Prefer omni_patch(action='validate', ...) directly — "
                                "omni_edit is a deprecated alias.",
                            ]
                        ),
                    })
                return _alias_text(v["validation_passed"], v["message"])

            if action == "apply":
                if not file or edit_content is None:
                    msg = "omni_edit apply needs both file and content."
                    return _alias_json({"ok": False, "error": msg}) if fmt == "json" else f"❌ {msg}"

                # ---- audit-bundle.r13 (P0 close): force_reason gate.
                # Same contract omni_patch uses: force=True without a
                # force_reason is rejected.
                if force and not (force_reason and force_reason.strip()):
                    err = (
                        "omni_edit apply: force=True requires a non-empty "
                        "force_reason."
                    )
                    if fmt == "json":
                        return _alias_json({
                            "ok": False,
                            "action": "apply",
                            "file": file,
                            "error": err,
                            "validation_passed": None,
                            "validation_bypassed": False,
                            "force": True,
                            "force_reason": None,
                            "next_actions": [
                                "Re-call with force_reason='<why this bypass is "
                                "acceptable>'.",
                                "Or fix the content and apply without force=True "
                                "(strongly recommended).",
                            ],
                        })
                    return f"❌ {err}"

                # ---- audit-bundle.r13 (P0 close): validate gate.
                # Reuses the exact ``_do_validate`` closure that omni_patch
                # uses, so the alias and the modern tool agree on what
                # counts as "passed".
                v = await _do_validate(file, edit_content)
                if not v["validation_passed"] and not force:
                    err_payload = {
                        "ok": False,
                        "action": "apply",
                        "file": file,
                        "error": "apply blocked by validation failure",
                        "validation_passed": False,
                        "validation_bypassed": False,
                        "force": False,
                        "force_reason": None,
                        "checks": v["checks"],
                        "counts": v["counts"],
                        "tools_run": v["tools_run"],
                        "tools_skipped": v["tools_skipped"],
                        "source": v["source"],
                        "next_actions": [
                            "Fix the listed errors and retry.",
                            "Prefer omni_patch(action='validate', file='%s', "
                            "content=...) directly — omni_edit is a deprecated "
                            "alias." % file,
                            "Set force=True with force_reason='<why>' to override "
                            "(strongly discouraged).",
                        ],
                    }
                    if fmt == "json":
                        return _alias_json(err_payload)
                    return (
                        f"❌ Apply blocked: validation failed "
                        f"({v['counts']['error']} error(s)). "
                        f"(omni_edit is a deprecated alias — prefer omni_patch.)"
                    )

                # ---- write goes through.
                raw = await make_request("POST", "/patch/apply", json={
                    "file_path": file, "content": edit_content,
                })
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                apply_ok = bool(data.get("success", False))
                if not apply_ok:
                    err = f"apply failed: {data.get('message', 'unknown')}"
                    if fmt == "json":
                        return _alias_json({
                            "ok": False,
                            "action": "apply",
                            "file": file,
                            "error": err,
                            "validation_passed": v["validation_passed"],
                            "validation_bypassed": (
                                (not v["validation_passed"]) and force
                            ),
                            "force": force,
                            "force_reason": force_reason if force else None,
                            "result": data,
                            "next_actions": [
                                "Re-check the file path / permissions and retry.",
                                "Prefer omni_patch(action='apply', ...) directly.",
                            ],
                        })
                    return f"❌ {err}"

                sid = data.get("session_id")
                rb = bool(data.get("rollback_available", True))
                validation_bypassed = (not v["validation_passed"]) and force
                next_actions: List[str] = []
                if validation_bypassed:
                    next_actions.append(
                        "⚠️ Validation was bypassed via force=True. "
                        "Re-run omni_diagnostics + tests immediately."
                    )
                if sid and rb:
                    next_actions.append(
                        "omni_patch(action='rollback', session_id='%s') if "
                        "anything looks wrong" % sid
                    )
                next_actions.append(
                    "Prefer omni_patch(action='apply', ...) directly — "
                    "omni_edit is a deprecated alias."
                )
                if fmt == "json":
                    return _alias_json({
                        "ok": True,
                        "action": "apply",
                        "file": file,
                        "message": data.get("message", ""),
                        "session_id": sid,
                        "rollback_available": rb,
                        "validation_passed": v["validation_passed"],
                        "validation_bypassed": validation_bypassed,
                        "force": force,
                        "force_reason": force_reason if force else None,
                        "lines_added": data.get("lines_added", 0),
                        "lines_removed": data.get("lines_removed", 0),
                        "original_hash": data.get("original_hash"),
                        "new_hash": data.get("new_hash"),
                        "result": data,
                        "next_actions": next_actions,
                    })
                bypass_tag = "  ⚠️ validation bypassed" if validation_bypassed else ""
                return (
                    f"✅ Applied: {file}{bypass_tag}\n"
                    f"   session_id: {sid}\n"
                    f"   rollback_available: {rb}\n"
                    f"   (omni_edit is a deprecated alias — prefer omni_patch)"
                )

            if action == "rollback":
                if not session_id:
                    msg = "omni_edit rollback needs session_id."
                    return _alias_json({"ok": False, "error": msg}) if fmt == "json" else f"❌ {msg}"
                raw = await make_request(
                    "POST", "/patch/rollback",
                    params={"session_id": session_id},
                )
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = bool(data.get("success", False))
                msg = data.get("message", "")
                if fmt == "json":
                    rollback_payload: Dict[str, Any] = {
                        "ok": ok,
                        "action": "rollback",
                        "session_id": session_id,
                        "message": msg,
                        "rolled_back": ok,
                        "result": data,
                        "next_actions": [
                            "Prefer omni_patch(action='rollback', ...) directly — "
                            "omni_edit is a deprecated alias.",
                        ],
                    }
                    # audit-bundle.r14 (P1): error-field alignment for
                    # rollback failures (e.g. session not found, already
                    # rolled back).
                    if not ok and msg:
                        rollback_payload["error"] = msg
                    return _alias_json(rollback_payload)
                return _alias_text(ok, msg)

            unknown = (
                f"Unknown action: {action}. "
                "Use: preview, validate, apply, rollback, ai_edit"
            )
            if fmt == "json":
                return _alias_json({"ok": False, "error": unknown})
            return f"❌ {unknown}"

        except Exception as e:
            if fmt == "json":
                return _alias_json({
                    "ok": False, "error": f"Edit operation failed: {e}",
                })
            return f"❌ Edit operation failed: {e}"


    @mcp.tool()
    async def omni_intelligence(
        task: Optional[str] = None,
        file: Optional[str] = None,
        symbol: Optional[str] = None,
        query: Optional[str] = None,
        token_budget: int = 4096,
        impact_depth: int = 2,
        max_search_results: int = 5,
    ) -> str:
        """Single-call multi-capability composer (Intelligence Layer).

        Combines code understanding + search + impact + memory + git
        history into one structured payload that fits the requested
        token budget. Use when the editor needs broad context for an
        unfamiliar file or symbol — far cheaper than chaining the six
        single-purpose tools by hand.

        Returns JSON-formatted IntelligenceContext with capability
        status, results from each capability that ran, and a flat
        ``advisories`` list summarising what to watch out for. This is a
        deprecated alias for omni_context — the JSON payload carries the
        common deprecated/replacement compat fields.
        """
        # ---------------------------------------------------------------
        # audit-bundle.r13 (P1-D): empty input must be a structured
        # ok=false envelope, not an all-empty success blob. Mirrors
        # omni_context's "needs at least one of file/symbol/task"
        # contract so the alias surface cannot be used to fake a
        # successful intelligence call without an anchor.
        # ---------------------------------------------------------------
        any_input = any(
            (v or "").strip() if isinstance(v, str) else v
            for v in (task, file, symbol, query)
        )
        if not any_input:
            return json.dumps(
                _alias_envelope("omni_intelligence", {
                    "ok": False,
                    "error": (
                        "omni_intelligence needs at least one of file=, "
                        "symbol=, task=, or query=."
                    ),
                    "suggested_next_action": (
                        "omni_context(symbol='<name>', "
                        "task='<what you are about to do>', format='json')"
                    ),
                    "next_actions": [
                        "omni_context(file='<path>', task='<intent>', "
                        "format='json') — the supported composer.",
                        "omni_search(mode='auto', query='<keywords>', "
                        "format='json') if you need lexical/semantic recall first.",
                    ],
                }),
                ensure_ascii=False, default=str,
            )

        try:
            payload = {
                "task": task,
                "file_path": file,
                "symbol": symbol,
                "query": query,
                "token_budget": token_budget,
                "impact_depth": impact_depth,
                "max_search_results": max_search_results,
            }
            res = await make_request(
                "POST", "/intelligence/context", json=payload
            )
            if not res.get("success"):
                return json.dumps(
                    _alias_envelope("omni_intelligence", {
                        "ok": False,
                        "error": f"Intelligence call failed: {res.get('error')}",
                    }),
                    ensure_ascii=False, default=str,
                )
            ctx = res.get("result", {})
            # ---------------------------------------------------------------
            # audit-bundle.r13 (P1-C): symbol-resolution parity with
            # omni_context. When the caller passed a symbol but the
            # backend returned an effectively-empty payload (no
            # impact, no understanding, no search hits), we annotate
            # the response so the AI editor knows the symbol could
            # not be found rather than silently presenting "all-empty"
            # as ok=true success.
            # ---------------------------------------------------------------
            symbol_resolution: Optional[str] = None
            confidence: Optional[str] = None
            note: Optional[str] = None
            symbol_clean = (symbol or "").strip()
            if symbol_clean:
                impact_blob = ctx.get("impact") or {}
                cu_blob = ctx.get("code_understanding") or {}
                search_blob = ctx.get("search") or {}
                impact_empty = (
                    not impact_blob
                    or (
                        (impact_blob.get("affected_count") in (0, None))
                        and (impact_blob.get("dependent_count") in (0, None))
                        and not impact_blob.get("callers")
                        and not impact_blob.get("callees")
                    )
                )
                cu_empty = not cu_blob or not (
                    cu_blob.get("symbols")
                    or cu_blob.get("definition")
                    or cu_blob.get("file_path")
                )
                search_results = (
                    search_blob.get("results")
                    or search_blob.get("hits")
                    or []
                )
                # Live-test fix: the intelligence backend sometimes
                # returns "fallback" search rows with score=None and
                # snippet="" when the symbol can't be resolved — those
                # are noise, not real hits. Only count rows that look
                # like genuine matches.
                meaningful_hits = [
                    h for h in search_results
                    if isinstance(h, dict) and (
                        h.get("score") is not None
                        or (h.get("snippet") or "").strip()
                        or h.get("symbol")
                        or h.get("line") is not None
                        or h.get("start_line") is not None
                    )
                ]
                search_empty = not meaningful_hits
                if impact_empty and cu_empty and search_empty:
                    symbol_resolution = "not_found"
                    confidence = "low"
                    note = (
                        f"Symbol '{symbol_clean}' could not be resolved by the "
                        "intelligence backend (no impact, no code "
                        "understanding, no search hits). Re-issue via "
                        "omni_context / omni_search to confirm."
                    )
                else:
                    symbol_resolution = "found"
                    confidence = "high"

            envelope_payload: Dict[str, Any] = {
                "ok": True,
                "elapsed_ms": ctx.get("elapsed_ms"),
                "token_estimate": ctx.get("token_estimate"),
                "token_budget": ctx.get("token_budget"),
                # audit-bundle.r14 (P2): every JSON envelope that has a
                # token estimate should also carry a ``truncated`` flag.
                # The backend doesn't surface one for /intelligence/context,
                # so we derive it: when token_estimate exceeds budget the
                # composer must have trimmed something.
                "truncated": bool(
                    ctx.get("token_budget")
                    and ctx.get("token_estimate")
                    and ctx.get("token_estimate", 0) > ctx.get("token_budget", 0)
                ),
                "advisories": ctx.get("advisories", []),
                "capability_status": ctx.get("capability_status", []),
                "code_understanding": ctx.get("code_understanding", {}),
                "search": ctx.get("search", {}),
                "impact": ctx.get("impact", {}),
                "memory": ctx.get("memory", {}),
                "git_history": ctx.get("git_history", {}),
                "errors": ctx.get("errors", {}),
                "next_actions": [
                    "Prefer omni_context(symbol=..., task=..., format='json') "
                    "— the supported composer with budget + truncation_reasons.",
                ],
            }
            # ---------- audit-bundle.r14 (P2): confidence normalisation
            # The intelligence backend ships memory.confidence as a raw
            # float (e.g. 0.767). Other tools (omni_search, omni_impact,
            # omni_context, omni_memory) all use the {high, medium, low}
            # band. Lift a normalised band to top level and preserve the
            # raw value as memory.confidence_score for callers that want
            # the underlying score.
            mem_blob = envelope_payload.get("memory") or {}
            raw_conf = mem_blob.get("confidence")
            if isinstance(raw_conf, (int, float)):
                memory_blob_copy = dict(mem_blob)
                memory_blob_copy["confidence_score"] = float(raw_conf)
                if raw_conf >= 0.75:
                    band = "high"
                elif raw_conf >= 0.4:
                    band = "medium"
                else:
                    band = "low"
                memory_blob_copy["confidence"] = band
                envelope_payload["memory"] = memory_blob_copy
                # Top-level confidence reflects the strongest available
                # signal — symbol_resolution overrides this below.
                envelope_payload["confidence"] = band
            if symbol_resolution is not None:
                envelope_payload["symbol_resolution"] = symbol_resolution
            if confidence is not None:
                envelope_payload["confidence"] = confidence
            if note is not None:
                envelope_payload["note"] = note
                envelope_payload["next_actions"].insert(
                    0,
                    "omni_search(mode='symbol', query='%s', format='json') "
                    "to confirm the symbol exists." % symbol_clean,
                )
                envelope_payload["next_actions"].insert(
                    1,
                    "omni_context(symbol='%s', task='<intent>', "
                    "format='json') for the modern composer."
                    % symbol_clean,
                )
            return json.dumps(
                _alias_envelope("omni_intelligence", envelope_payload),
                ensure_ascii=False,
                default=str,
            )
        except Exception as exc:
            return json.dumps(
                _alias_envelope("omni_intelligence", {
                    "ok": False, "error": f"omni_intelligence failed: {exc}",
                }),
                ensure_ascii=False, default=str,
            )

    @mcp.tool()
    async def omni_skill(
        action: str = "list",
        name: Optional[str] = None,
        query: Optional[str] = None,
        format: str = "json",
    ) -> str:
        """Discover and inspect packaged workflow recipes (skills).

        Skills are declarative manifests that bundle a sequence of MCP
        tool calls into a recommended workflow. The OmniCode server
        does NOT execute them — this tool just shows you the recipe so
        you can decide whether to follow it.

        Actions:
          - list:    show every available skill (name + description).
          - search:  filter skills by ``query`` (matches name / description / keywords).
          - show:    return the full step-by-step recipe for ``name``.

        First-party skills:
          - omni-impact-review:  pre-edit blast-radius bundle
          - omni-safe-refactor:  preview → validate → apply → rollback
          - omni-test-coverage:  find related tests + suggested commands

        Drop your own skills as JSON or YAML at ``~/.kiro/skills/`` or
        ``<workspace>/.kiro/skills/``.
        """
        is_json = (format or "json").lower() != "text"
        ALLOWED_ACTIONS = ["list", "search", "show"]

        def _err(msg: str, **extra: Any) -> str:
            payload = {
                "ok": False,
                "action": action,
                "error": msg,
                "allowed_actions": list(ALLOWED_ACTIONS),
                **extra,
            }
            payload.setdefault("next_actions", [
                "omni_skill(action='list', format='json') "
                "to see available skills",
                "omni_skill(action='search', query='safe refactor', format='json') "
                "to find a relevant workflow",
                "omni_skill(action='show', name='omni-safe-refactor', format='json') "
                "to inspect a recipe",
            ])
            _stamp(payload, tool="omni_skill")
            return (
                json.dumps(payload, ensure_ascii=False, indent=2)
                if is_json
                else f"❌ {msg}"
            )

        try:
            from omnicode_core.skills import (
                SkillNotFoundError,
                get_skill_loader,
            )
            loader = get_skill_loader()

            if action == "list":
                skills = loader.list_skills()
                if is_json:
                    return json.dumps(
                        _stamp({
                            "ok": True,
                            "action": "list",
                            "count": len(skills),
                            "skills": [s.to_dict() for s in skills],
                        }, tool="omni_skill"),
                        ensure_ascii=False,
                        indent=2,
                    )
                if not skills:
                    return "📦 No skills installed. Drop manifests under ~/.kiro/skills/."
                lines = [f"📦 {len(skills)} skill(s) available:\n"]
                for s in skills:
                    lines.append(f"  • {s.name:<24} v{s.version}")
                    lines.append(f"      {s.description}")
                    if s.keywords:
                        lines.append(f"      keywords: {', '.join(s.keywords[:6])}")
                    lines.append("")
                lines.append(
                    "💡 Use omni_skill(action='show', name=<name>) to see the steps."
                )
                return "\n".join(lines)

            if action == "search":
                if not query:
                    return _err("omni_skill search needs a query.")
                ranked = loader.search(query, max_results=10)
                if is_json:
                    return json.dumps(
                        _stamp({
                            "ok": True,
                            "action": "search",
                            "query": query,
                            "count": len(ranked),
                            "results": [
                                {
                                    "skill": s.to_dict(),
                                    "score": int(score),
                                    "why_matched": list(why),
                                }
                                for s, score, why in ranked
                            ],
                        }, tool="omni_skill"),
                        ensure_ascii=False,
                        indent=2,
                    )
                if not ranked:
                    return f"📦 No skills matching '{query}'"
                lines = [f"🔍 {len(ranked)} skill(s) matching '{query}':\n"]
                for s, score, why in ranked:
                    why_tail = (
                        f"   why_matched: {', '.join(why[:4])}" if why else ""
                    )
                    lines.append(f"  • {s.name}  (score={score}): {s.description}")
                    if why_tail:
                        lines.append(why_tail)
                return "\n".join(lines)

            if action == "show":
                if not name:
                    return _err("omni_skill show needs name=<skill-name>.")
                try:
                    skill = loader.get_skill(name)
                except SkillNotFoundError:
                    return _err(
                        f"Skill '{name}' not found. Try omni_skill(action='list').",
                        name=name,
                    )
                if is_json:
                    return json.dumps(
                        _stamp({
                            "ok": True,
                            "action": "show",
                            "name": name,
                            "skill": skill.to_dict(),
                        }, tool="omni_skill"),
                        ensure_ascii=False,
                        indent=2,
                    )
                lines = [
                    f"📦 {skill.name}  v{skill.version}",
                    f"   {skill.description}",
                ]
                if skill.when_to_use:
                    lines.append(f"   When to use: {skill.when_to_use}")
                if skill.tools_used:
                    lines.append(f"   Tools used: {', '.join(skill.tools_used)}")
                lines.append(
                    f"   does_execute: {skill.does_execute}  "
                    f"(omni_skill never auto-runs these steps)"
                )
                if skill.safety_notes:
                    lines.append("")
                    lines.append("   Safety notes:")
                    for note in skill.safety_notes:
                        lines.append(f"     • {note}")
                if skill.keywords:
                    lines.append("")
                    lines.append(f"   Keywords: {', '.join(skill.keywords)}")
                if skill.inputs:
                    lines.append("")
                    lines.append("   Inputs:")
                    for inp in skill.inputs:
                        req = "*" if inp.get("required") else " "
                        lines.append(
                            f"     {req} {inp.get('name', '?'):<14} "
                            f"{inp.get('description', '')}"
                        )
                lines.append("")
                lines.append(f"   Steps ({len(skill.steps)}):")
                for i, step in enumerate(skill.steps, 1):
                    sid = step.get("id", "")
                    title = step.get("title", "")
                    tool = step.get("tool", "?")
                    purpose = step.get("purpose") or step.get("explain", "")
                    required = step.get("required", True)
                    condition = step.get("condition", "always")
                    head = f"     {i}. [{sid}] {tool}"
                    if title:
                        head += f"  — {title}"
                    if not required:
                        head += "  (optional)"
                    lines.append(head)
                    if purpose:
                        lines.append(f"        purpose: {purpose}")
                    if condition and condition != "always":
                        lines.append(f"        condition: {condition}")
                    args = step.get("args", {})
                    if args:
                        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                        lines.append(f"        args: {args_str}")
                lines.append("")
                lines.append(
                    f"   Source: {skill.source}\n"
                    f"   Note: OmniCode does NOT auto-execute skills. Read the\n"
                    f"   steps and call each tool yourself, interpolating\n"
                    f"   ${{...}} placeholders from the user's request."
                )
                return "\n".join(lines)

            return _err(
                f"Unknown omni_skill action: {action}. "
                f"Use: {', '.join(ALLOWED_ACTIONS)}.",
                allowed_actions=ALLOWED_ACTIONS,
            )

        except Exception as exc:  # noqa: BLE001
            return _err(f"omni_skill failed: {exc}")

    @mcp.tool()
    async def omni_index(
        action: str = "status",
        scope: str = "semantic",
        workspace_id: Optional[str] = None,
        force: bool = False,
        background: bool = True,
        format: str = "json",
    ) -> str:
        """Manage cloud snapshot indexing for hybrid analysis.

        Use this when ``omni_status`` reports ``recommended_query_mode`` as
        ``exact_first`` and you want to opt into semantic enrichment.

        Actions:
          - status:    inspect the current background snapshot-index job
          - bootstrap: start explicit snapshot indexing

        Scopes:
          - semantic:     full semantic bootstrap over snapshot content
          - exact_policy: index only policy-selected source-like files
          - workspace:    deterministic local exact index (files/lines/symbols)
        """

        fmt = (format or "json").lower()
        action_value = (action or "status").strip().lower()
        scope_value = (scope or "semantic").strip().lower()
        valid_actions = ("status", "bootstrap")
        valid_scopes = ("semantic", "exact_policy", "workspace")

        def _render(payload: Dict[str, Any]) -> str:
            _stamp(payload, tool="omni_index")
            if fmt == "text":
                if not payload.get("ok", False):
                    return f"ERROR omni_index: {payload.get('error', 'unknown')}"
                if payload.get("action") == "status":
                    job = payload.get("job") or {}
                    return (
                        "omni_index status\n"
                        f"  workspace_id: {payload.get('workspace_id')}\n"
                        f"  state:        {payload.get('state')}\n"
                        f"  job:          {job.get('job_id', '-')}\n"
                        f"  scope:        {job.get('scope', payload.get('scope'))}\n"
                    )
                job = payload.get("job") or {}
                return (
                    "omni_index bootstrap\n"
                    f"  workspace_id: {payload.get('workspace_id')}\n"
                    f"  scope:        {payload.get('scope')}\n"
                    f"  background:   {payload.get('background')}\n"
                    f"  state:        {payload.get('state', job.get('state'))}\n"
                    f"  job:          {job.get('job_id', '-')}\n"
                )
            return json.dumps(payload, ensure_ascii=False, indent=2)

        if action_value not in valid_actions:
            return _render(
                {
                    "ok": False,
                    "action": action_value,
                    "error": "action must be one of: status, bootstrap",
                    "allowed_actions": list(valid_actions),
                    "next_actions": [
                        "omni_index(action='status', format='json')",
                        "omni_index(action='bootstrap', scope='semantic', background=True, format='json')",
                    ],
                }
            )
        if scope_value not in valid_scopes:
            return _render(
                {
                    "ok": False,
                    "action": action_value,
                    "scope": scope_value,
                    "error": "scope must be one of: semantic, exact_policy, workspace",
                    "allowed_scopes": list(valid_scopes),
                    "next_actions": [
                        "omni_index(action='bootstrap', scope='workspace', background=False, format='json')",
                        "omni_index(action='bootstrap', scope='semantic', background=True, format='json')",
                    ],
                }
            )

        import os as _os

        effective_workspace_id = (
            workspace_id or _os.environ.get("OMNICODE_WORKSPACE_ID") or ""
        ).strip() or None
        params: Dict[str, Any] = {}
        headers: Dict[str, str] = {}
        if effective_workspace_id:
            params["workspace_id"] = effective_workspace_id
            headers["X-Omnicode-Workspace"] = effective_workspace_id

        try:
            if scope_value == "workspace":
                from omnicode_core.workspace.exact_index import SnapshotExactIndex

                ws_root, ws_source, ws_warnings = _get_workspace_root()
                local_workspace_id = (
                    effective_workspace_id or ws_root.name or "workspace"
                )
                index = SnapshotExactIndex()
                if action_value == "status":
                    status = index.status(workspace_id=local_workspace_id)
                    ready = bool(
                        int(status.get("files") or 0) > 0
                        and int(status.get("symbols") or 0) > 0
                    )
                    payload = {
                        "ok": True,
                        "action": action_value,
                        "scope": scope_value,
                        "workspace_id": local_workspace_id,
                        "source": "local_exact_index",
                        "state": "ready" if ready else "not_ready",
                        "workspace_root_source": ws_source,
                        "workspace_warnings": ws_warnings,
                        "local_index_ready": ready,
                        "status": status,
                        "next_actions": (
                            [
                                "omni_search(query='<symbol>', mode='symbol', format='json') for deterministic symbol search.",
                                "omni_search(query='<literal>', mode='text', format='json') for deterministic text search.",
                            ]
                            if ready
                            else [
                                "omni_index(action='bootstrap', scope='workspace', background=False, format='json') to build files/lines/symbols.",
                            ]
                        ),
                    }
                    return _render(payload)

                result = index.index_workspace_root(
                    workspace_id=local_workspace_id,
                    root=ws_root,
                    force=bool(force),
                )
                status = result.get("status") or {}
                payload = {
                    "ok": True,
                    "action": action_value,
                    "scope": scope_value,
                    "workspace_id": local_workspace_id,
                    "source": "local_exact_index",
                    "background": False,
                    "state": "ready",
                    "workspace_root": str(ws_root),
                    "workspace_root_source": ws_source,
                    "workspace_warnings": ws_warnings,
                    "result": result,
                    "local_index_ready": bool(
                        int(status.get("files") or 0) > 0
                        and int(status.get("symbols") or 0) > 0
                    ),
                    "next_actions": [
                        "omni_status(format='json') to confirm local_index.local_index_ready=true.",
                        "omni_search(query='<symbol>', mode='symbol', format='json') to use the exact symbol index.",
                        "omni_search(query='<literal>', mode='text', format='json') to use deterministic text search.",
                    ],
                }
                return _render(payload)

            if action_value == "status":
                raw = await make_request(
                    "GET",
                    "/search/index/status",
                    params=params,
                    headers=headers,
                )
            else:
                raw = await make_request(
                    "POST",
                    "/search/index",
                    params={
                        **params,
                        "force": bool(force),
                        "background": bool(background),
                        "scope": scope_value,
                    },
                    headers=headers,
                )

            data = raw.get("result", raw) if isinstance(raw, dict) else {}
            backend_error = _backend_error_message(data)
            if backend_error:
                return _render(
                    {
                        "ok": False,
                        "action": action_value,
                        "scope": scope_value,
                        "workspace_id": effective_workspace_id,
                        "error": _sanitize_error_text(str(backend_error)),
                        "next_actions": [
                            "omni_status(format='json') to inspect backend availability.",
                            "Check workspace_id and backend_url, then retry omni_index.",
                        ],
                    }
                )

            payload: Dict[str, Any] = {
                "ok": True,
                "action": action_value,
                "scope": scope_value,
                "workspace_id": effective_workspace_id,
                "backend_action": (
                    "GET /search/index/status"
                    if action_value == "status"
                    else "POST /search/index"
                ),
            }
            if isinstance(data, dict):
                payload.update(data)
            payload["next_actions"] = (
                [
                    "omni_status(format='json') to confirm semantic_index_ready and recommended_query_mode.",
                    "omni_search(query='<task>', mode='semantic', format='json') after semantic indexing completes.",
                ]
                if action_value == "bootstrap"
                else [
                    "omni_index(action='bootstrap', scope='semantic', background=True, format='json') to start full semantic bootstrap.",
                    "omni_status(format='json') to inspect index_readiness_contract.",
                ]
            )
            return _render(payload)
        except Exception as exc:  # noqa: BLE001
            return _render(
                {
                    "ok": False,
                    "action": action_value,
                    "scope": scope_value,
                    "workspace_id": effective_workspace_id,
                    "error": _sanitize_error_text(
                        f"{exc.__class__.__name__}: {exc}"
                    ),
                    "next_actions": [
                        "omni_status(format='json') to inspect backend availability.",
                        "Retry omni_index after the backend is reachable.",
                    ],
                }
            )

    @mcp.tool()
    async def omni_status() -> str:
        """Runtime self-check for the live MCP host.

        Returns a JSON envelope describing the actual code that's serving
        traffic right now. Use this BEFORE any human acceptance test —
        it's the only reliable way to detect the "source updated, unit
        tests pass, but the running FastMCP host is still bound to a
        previous handler" failure mode that bit the late-May 2026 audit.

        Always returns::

            {
              "ok":                bool,
              "pid":               int,
              "process_start_time": str (ISO-8601, UTC),
              "module_path":       str,
              "module_sha1":       str,                  # full sha1 of high_level_tools.py
              "module_mtime":      str (ISO-8601),
              "python_executable": str,
              "python_version":    str,
              "handler_version":   str,                  # _HANDLER_VERSION
              "handler_features":  List[str],            # _HANDLER_FEATURES
              "registered_tools":  List[str],            # what FastMCP is actually serving
              "deprecated_aliases_present": List[str],   # subset of registered_tools
              "warnings":          List[str]             # cross-checks that failed
            }

        ``warnings`` is empty when the running runtime matches the
        on-disk source. A non-empty list means a stale binding — fix by
        killing the MCP host process (not a soft reload) and restarting.
        """
        import datetime as _dt
        import hashlib as _hashlib
        import os as _os
        import platform as _platform
        import sys as _sys
        from pathlib import Path as _Path

        # Lazily capture the boot time the first call into this tool —
        # accurate enough for "is this the same process I saw 5 minutes
        # ago" without requiring a hook into the FastMCP lifespan.
        global _PROCESS_START_TIME
        if _PROCESS_START_TIME is None:
            _PROCESS_START_TIME = _dt.datetime.now(_dt.timezone.utc).isoformat()

        warnings_list: List[str] = []

        # --- Module identity ------------------------------------------
        module_path = ""
        module_sha1 = ""
        module_mtime = ""
        try:
            this_module = _sys.modules[__name__]
            module_path = getattr(this_module, "__file__", "") or ""
            if module_path:
                p = _Path(module_path)
                module_sha1 = _hashlib.sha1(p.read_bytes()).hexdigest()
                module_mtime = _dt.datetime.fromtimestamp(
                    p.stat().st_mtime, tz=_dt.timezone.utc,
                ).isoformat()
                try:
                    started_at = _dt.datetime.fromisoformat(
                        str(_PROCESS_START_TIME).replace("Z", "+00:00")
                    )
                    module_mtime_dt = _dt.datetime.fromisoformat(module_mtime)
                    if module_mtime_dt > started_at + _dt.timedelta(seconds=1):
                        warnings_list.append(
                            "module_mtime_after_process_start: source changed "
                            "after MCP host started; restart the MCP host."
                        )
                except Exception as exc:  # noqa: BLE001
                    warnings_list.append(f"module_staleness_check_failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"module_identity_failed: {exc}")

        # --- Cross-check: did the loaded module export every helper
        #     advertised by _HANDLER_FEATURES? Catches partial reloads.
        feature_to_attr = {
            "search.source_confidence": "_infer_source_confidence",
            "read.diagnostics_aligned": "_collect_diagnostics_payload",
            "read.language_fallback": "_guess_language_from_path",
            "read.next_actions_per_mode": "_next_actions_for_mode",
        }
        for feature, attr in feature_to_attr.items():
            if feature not in _HANDLER_FEATURES:
                continue
            if attr == "_collect_diagnostics_payload":
                # Closure inside register_high_level_tools — verify by
                # reading the source rather than hasattr.
                try:
                    src_text = _Path(module_path).read_text(
                        encoding="utf-8", errors="ignore",
                    ) if module_path else ""
                    if attr not in src_text:
                        warnings_list.append(
                            f"feature_missing_in_source:{feature}"
                        )
                except Exception:  # noqa: BLE001
                    warnings_list.append(f"feature_check_failed:{feature}")
            else:
                if not hasattr(this_module, attr):
                    warnings_list.append(
                        f"feature_missing_at_runtime:{feature}"
                    )

        # --- Registered tools (FastMCP introspection) -----------------
        registered_tools: List[str] = []
        try:
            tm = getattr(mcp, "_tool_manager", None)
            if tm is not None:
                tools_attr = getattr(tm, "_tools", None)
                if isinstance(tools_attr, dict):
                    registered_tools = sorted(tools_attr.keys())
                else:
                    # Fallback: async list_tools().
                    listing = await mcp.list_tools()
                    registered_tools = sorted(
                        getattr(t, "name", str(t)) for t in listing
                    )
        except Exception as exc:  # noqa: BLE001
            warnings_list.append(f"tool_listing_failed: {exc}")

        deprecated = [
            t for t in registered_tools
            if t in {"omni_analyze", "omni_edit", "omni_intelligence"}
        ]

        # --- Sanity: every flagship tool we ship MUST be registered.
        flagship = (
            "omni_search", "omni_read", "omni_impact",
            "omni_diagnostics", "omni_patch", "omni_memory",
            "omni_context", "omni_skill", "omni_index", "discover_tools",
            "omni_status",
        )
        missing_flagship = [
            t for t in flagship if t not in registered_tools
        ]
        if missing_flagship:
            warnings_list.append(
                "flagship_tools_missing:" + ",".join(missing_flagship)
            )

        # Audit rule: every tool that has a contract_version MUST also
        # support format="json" so the live response can be stamped.
        # Anything in _CONTRACT_VERSIONS but absent from
        # _TOOLS_WITH_JSON_STAMP gets surfaced — this prevents a future
        # text-only tool slipping into the flagship roster without an
        # auditable response.
        json_stamp_set = set(_TOOLS_WITH_JSON_STAMP)
        for tool_name in _CONTRACT_VERSIONS:
            if tool_name not in json_stamp_set:
                warnings_list.append(f"json_stamp_unsupported:{tool_name}")

        # audit-bundle.r11 (workspace.root_alignment): surface the
        # canonical workspace root + cwd so an auditor can spot a
        # mismatch (the symptom that motivated r11). When the helper
        # had to fall back to cwd, ``workspace_root_fallback_to_cwd``
        # is added to the warning list above via ``ws_warnings``.
        ws_root, ws_source, ws_warnings = _get_workspace_root()
        cwd_path = Path.cwd().resolve()
        warnings_list.extend(ws_warnings)

        # audit-bundle.r12 (workspace.backend_root_visibility): probe the
        # backend so an auditor can see whether the MCP-host root and the
        # FastAPI backend root agree. Best-effort — when the backend
        # doesn't expose a canonical root or a resolved path, we leave
        # the field null and add a workspace_root_warning.
        backend_workspace_root: Optional[str] = None
        workspace_root_matches_backend: Optional[bool] = None
        backend_workspace_root_source: Optional[str] = None
        workspace_root_warning: Optional[str] = None
        try:
            probe = await _get_backend_file_markers("README.md")
            backend_workspace_root = probe.get("backend_workspace_root")
            resolved = probe.get("resolved_file_path")
            if backend_workspace_root:
                backend_workspace_root_source = "backend_response"
            elif resolved:
                # Derive root from the resolved file path: strip the file
                # name to get a directory, then resolve. Best-effort —
                # only valid when the backend returned an absolute path.
                p = Path(resolved)
                if p.is_absolute():
                    # README.md is at the workspace root by convention.
                    backend_workspace_root = str(p.parent.resolve())
                    backend_workspace_root_source = "derived_from_resolved_path"
            if backend_workspace_root:
                try:
                    workspace_root_matches_backend = (
                        Path(backend_workspace_root).resolve() == ws_root
                    )
                except Exception:
                    workspace_root_matches_backend = None
            else:
                workspace_root_warning = (
                    "backend workspace root not exposed via /read probe"
                )
        except Exception as exc:
            workspace_root_warning = (
                f"backend root probe failed: {exc.__class__.__name__}"
            )

        # workspace-bridge step 12: aggregate local sync state, cloud snapshot
        # state, and HybridToolRouter decisions. This is deliberately
        # best-effort: a workspace without sync configured should still have
        # a clean omni_status response.
        workspace_id = _os.environ.get("OMNICODE_WORKSPACE_ID") or None
        executor_mode = _os.environ.get("OMNICODE_EXECUTOR_MODE") or "local"
        backend_url = (
            _os.environ.get("OMNICODE_REMOTE")
            or _os.environ.get("OMNICODE_FASTAPI_BASE_URL")
            or None
        )
        sync_payload: Dict[str, Any] = {
            "configured": bool(workspace_id),
            "workspace_id": workspace_id,
            "executor_mode": executor_mode,
            "backend_url": backend_url,
            "cloud_available": bool(backend_url),
            "cloud_unavailable": False,
            "cloud_status_warning": None,
            "local_revision": 0,
            "accepted_revision": 0,
            "indexed_revision": 0,
            "manifest_present": False,
            "pending_count": 0,
            "pending_paths": [],
            "snapshot_store": None,
            "routes": {},
            "warning": None,
        }
        try:
            from dataclasses import asdict as _asdict

            from omnicode_core.workspace import (
                HybridToolRouter,
                SyncRevisionState,
            )

            local_revision = 0
            accepted_revision = 0
            indexed_revision = 0
            pending_count = 0
            snapshot_status: Optional[Dict[str, Any]] = None
            snapshot_store_source: Optional[str] = None
            cloud_available = bool(backend_url)
            cloud_status_warning: Optional[str] = None
            cloud_index_status: Dict[str, Any] = {}
            semantic_index_status: Dict[str, Any] = {}

            if workspace_id:
                try:
                    from omnicode_core.workspace.local import LocalWorkspace
                    from omnicode_core.workspace.manifest import (
                        LocalManifest,
                        default_manifest_path,
                    )

                    local_ws = LocalWorkspace(root=ws_root, workspace_id=workspace_id)
                    manifest_path = default_manifest_path(workspace_id)
                    sync_payload["manifest_path"] = str(manifest_path)
                    if manifest_path.exists():
                        manifest = LocalManifest.load(workspace=local_ws)
                        local_revision = int(manifest.local_revision)
                        accepted_revision = int(
                            manifest.data.get("last_accepted_revision", 0)
                        )
                        indexed_revision = int(
                            manifest.data.get("last_indexed_revision", 0)
                        )
                        pending_entries = manifest.data.get("pending") or []
                        if isinstance(pending_entries, list):
                            pending_count = len(pending_entries)
                            sync_payload["pending_count"] = pending_count
                            sync_payload["pending_paths"] = [
                                str(row.get("path"))
                                for row in pending_entries
                                if isinstance(row, dict) and row.get("path")
                            ][:20]
                        sync_payload["manifest_present"] = True
                except Exception as exc:  # noqa: BLE001
                    sync_payload["manifest_warning"] = (
                        f"{exc.__class__.__name__}: {exc}"
                    )

                if backend_url and executor_mode in {"hybrid", "remote"}:
                    try:
                        raw_status = await make_request(
                            "GET",
                            "/sync/status",
                            params={"workspace_id": workspace_id},
                        )
                        cloud_status = (
                            raw_status.get("result", raw_status)
                            if isinstance(raw_status, dict)
                            else {}
                        )
                        backend_error = _backend_error_message(cloud_status)
                        if backend_error:
                            cloud_available = False
                            cloud_status_warning = backend_error
                        elif isinstance(cloud_status, dict) and cloud_status:
                            accepted_revision = max(
                                accepted_revision,
                                int(cloud_status.get("accepted_revision") or 0),
                            )
                            indexed_revision = max(
                                indexed_revision,
                                int(cloud_status.get("indexed_revision") or 0),
                            )
                            cloud_index_status = {
                                key: cloud_status.get(key)
                                for key in (
                                    "semantic_index_ready",
                                    "semantic_index_coverage",
                                    "semantic_initial_exact_only",
                                    "exact_index_ready",
                                    "snapshot_ready",
                                    "index_worker_busy",
                                    "search_degraded",
                                    "semantic_pending_revisions",
                                    "exact_pending_revisions",
                                    "pending_files",
                                    "index_queue_depth",
                                    "index_worker_running",
                                    "last_index_error",
                                    "last_index_elapsed_ms",
                                    "current_index_files",
                                    "current_index_bytes",
                                    "current_index_elapsed_ms",
                                    "recommended_query_mode",
                                    "query_mode_reason",
                                    "supported_query_modes",
                                    "exact_query_safe",
                                    "strict_semantic_safe",
                                    "semantic_query_safe",
                                    "index_readiness_contract",
                                )
                                if key in cloud_status
                            }
                            if isinstance(cloud_status.get("exact_index"), dict):
                                cloud_index_status["exact_index"] = dict(
                                    cloud_status["exact_index"]
                                )
                            cloud_snapshot = cloud_status.get("snapshot_store")
                            if isinstance(cloud_snapshot, dict):
                                snapshot_status = dict(cloud_snapshot)
                                snapshot_store_source = "cloud"
                                accepted_revision = max(
                                    accepted_revision,
                                    int(
                                        snapshot_status.get("accepted_revision")
                                        or snapshot_status.get("latest_revision")
                                        or 0
                                    ),
                                )
                                indexed_revision = max(
                                    indexed_revision,
                                    int(
                                        snapshot_status.get("indexed_revision")
                                        or 0
                                    ),
                                )
                    except Exception as exc:  # noqa: BLE001
                        cloud_available = False
                        cloud_status_warning = f"{exc.__class__.__name__}: {exc}"

                    if cloud_available:
                        try:
                            raw_search_stats = await make_request(
                                "GET",
                                "/search/stats",
                            )
                            search_stats = (
                                raw_search_stats.get("result", raw_search_stats)
                                if isinstance(raw_search_stats, dict)
                                else {}
                            )
                            if isinstance(search_stats, dict):
                                raw_semantic = search_stats.get("semantic_index")
                                if isinstance(raw_semantic, dict):
                                    semantic_index_status = dict(raw_semantic)
                                    for key in (
                                        "semantic_index_ready",
                                        "semantic_index_model",
                                        "semantic_index_dimension",
                                        "semantic_index_stale_reason",
                                        "semantic_index_invalid",
                                        "semantic_index_stale",
                                        "chunker_version",
                                        "vector_count",
                                        "faiss_dimension",
                                    ):
                                        if key in raw_semantic:
                                            cloud_index_status[key] = raw_semantic[key]
                        except Exception as exc:  # noqa: BLE001
                            cloud_index_status.setdefault(
                                "semantic_index_status_warning",
                                f"{exc.__class__.__name__}: {exc}",
                            )

                try:
                    from omnicode_core.workspace.snapshot_store import (
                        CloudSnapshotStore,
                    )

                    local_snapshot = CloudSnapshotStore().status(workspace_id)
                    if snapshot_status is None:
                        snapshot_status = local_snapshot
                        snapshot_store_source = "local"
                        accepted_revision = max(
                            accepted_revision,
                            int(local_snapshot.get("accepted_revision", 0)),
                        )
                        indexed_revision = max(
                            indexed_revision,
                            int(local_snapshot.get("indexed_revision", 0)),
                        )
                except Exception as exc:  # noqa: BLE001
                    sync_payload["snapshot_warning"] = (
                        f"{exc.__class__.__name__}: {exc}"
                    )

            sync_state = SyncRevisionState(
                local_revision=local_revision,
                accepted_revision=accepted_revision,
                indexed_revision=indexed_revision,
                cloud_available=cloud_available,
                pending_count=pending_count,
                required_revision=(
                    accepted_revision
                    if pending_count <= 0 and accepted_revision > 0
                    else max(local_revision, accepted_revision)
                ),
            )
            indexed_file_count = 0
            if isinstance(snapshot_status, dict):
                indexed_file_count = int(
                    snapshot_status.get("files")
                    or snapshot_status.get("file_count")
                    or snapshot_status.get("indexed_files")
                    or 0
                )
            index_fresh = (
                bool(workspace_id)
                and cloud_available
                and accepted_revision > 0
                and indexed_revision >= accepted_revision
            )
            exact_query_safe = bool(
                cloud_index_status.get("exact_query_safe")
                or cloud_index_status.get("exact_index_ready")
            )
            strict_semantic_safe = bool(
                cloud_index_status.get("strict_semantic_safe")
                or cloud_index_status.get("semantic_index_ready", index_fresh)
            )
            recommended_query_mode = str(
                cloud_index_status.get("recommended_query_mode")
                or (
                    "semantic_first"
                    if strict_semantic_safe
                    else "exact_first"
                    if exact_query_safe
                    else "snapshot_only"
                    if indexed_file_count
                    else "local_only"
                )
            )
            index_readiness = {
                "text_index_ready": bool(
                    exact_query_safe
                    or cloud_index_status.get("snapshot_ready", indexed_file_count)
                ),
                "symbol_index_ready": bool(
                    exact_query_safe or strict_semantic_safe
                ),
                "graph_index_ready": False,
                "fresh": bool(index_fresh),
                "exact_query_safe": exact_query_safe,
                "semantic_query_safe": strict_semantic_safe,
                "strict_semantic_safe": strict_semantic_safe,
                "recommended_query_mode": recommended_query_mode,
                "query_mode_reason": cloud_index_status.get(
                    "query_mode_reason"
                ),
                "supported_query_modes": cloud_index_status.get(
                    "supported_query_modes",
                    [],
                ),
                "semantic_index_ready": bool(
                    cloud_index_status.get("semantic_index_ready", index_fresh)
                ),
                "semantic_index_model": cloud_index_status.get(
                    "semantic_index_model"
                ),
                "semantic_index_dimension": cloud_index_status.get(
                    "semantic_index_dimension"
                ),
                "semantic_index_stale_reason": cloud_index_status.get(
                    "semantic_index_stale_reason"
                ),
                "semantic_index_invalid": bool(
                    cloud_index_status.get("semantic_index_invalid", False)
                ),
                "semantic_index_stale": bool(
                    cloud_index_status.get("semantic_index_stale", False)
                ),
                "semantic_index_chunker_version": cloud_index_status.get(
                    "chunker_version"
                ),
                "semantic_vector_count": int(
                    cloud_index_status.get("vector_count") or 0
                ),
                "semantic_index_coverage": cloud_index_status.get(
                    "semantic_index_coverage",
                    "unknown",
                ),
                "semantic_initial_exact_only": bool(
                    cloud_index_status.get("semantic_initial_exact_only", False)
                ),
                "exact_index_ready": bool(
                    cloud_index_status.get("exact_index_ready", exact_query_safe)
                ),
                "exact_index": cloud_index_status.get("exact_index", {}),
                "index_worker_busy": bool(
                    cloud_index_status.get("index_worker_busy", False)
                ),
                "search_degraded": bool(
                    cloud_index_status.get("search_degraded", not index_fresh)
                ),
                "semantic_pending_revisions": int(
                    cloud_index_status.get("semantic_pending_revisions") or 0
                ),
                "indexed_files": indexed_file_count,
                "accepted_revision": accepted_revision,
                "indexed_revision": indexed_revision,
                "exact_pending_revisions": int(
                    cloud_index_status.get("exact_pending_revisions") or 0
                ),
                "graph_index_reason": (
                    "graph bootstrap is not persisted yet; omni_impact may "
                    "run a bounded live graph scan and report caveats."
                ),
            }
            router = HybridToolRouter(executor=executor_mode)
            route_tools = (
                "omni_read", "omni_patch", "omni_diagnostics",
                "omni_search", "omni_context", "omni_impact",
                "omni_status",
            )
            sync_payload.update(
                {
                    "local_revision": local_revision,
                    "accepted_revision": accepted_revision,
                    "indexed_revision": indexed_revision,
                    "cloud_available": cloud_available,
                    "cloud_unavailable": not cloud_available,
                    "cloud_status_warning": cloud_status_warning,
                    "snapshot_store": snapshot_status,
                    "snapshot_store_source": snapshot_store_source,
                    "semantic_index": semantic_index_status,
                    "cloud_index_status": cloud_index_status,
                    "index_readiness": index_readiness,
                    "routes": {
                        tool: _asdict(router.route(tool, sync_state=sync_state))
                        for tool in route_tools
                    },
                }
            )
            if cloud_status_warning:
                sync_payload["warning"] = (
                    "cloud backend unavailable: " + cloud_status_warning
                )
                warnings_list.append(
                    "cloud_unavailable:" + cloud_status_warning
                )
        except Exception as exc:  # noqa: BLE001
            sync_payload["warning"] = f"{exc.__class__.__name__}: {exc}"

        capability_contract: Dict[str, Any]
        try:
            from omnicode_core.config.capabilities import (
                build_capability_contract,
            )
            from omnicode_core.config.runtime import RuntimeConfig

            runtime_for_status = RuntimeConfig(
                workspace_root=ws_root,
                workspace_id=workspace_id or ws_root.name or "workspace",
                executor=executor_mode,
                backend_url=backend_url,
                llm_mode=_os.environ.get("OMNICODE_LLM_MODE") or "off",
                embedding_mode=(
                    _os.environ.get("OMNICODE_EMBEDDING_MODE") or "cloud"
                ),
                diagnostics_mode=(
                    _os.environ.get("OMNICODE_DIAGNOSTICS_MODE")
                    or "local-first"
                ),
            )
            capability_contract = build_capability_contract(
                runtime_for_status,
            ).to_dict()
        except Exception as exc:  # noqa: BLE001
            capability_contract = {
                "warning": f"{exc.__class__.__name__}: {exc}",
            }

        agent_auto: Dict[str, Any]
        try:
            from omnicode_core.config.runtime import RuntimeConfig
            from omnicode_core.workspace.agent_auto import decide_agent_auto

            runtime_for_agent = RuntimeConfig(
                workspace_root=ws_root,
                workspace_id=workspace_id or ws_root.name or "workspace",
                executor=executor_mode,
                backend_url=backend_url,
                sync_mode=_os.environ.get("OMNICODE_SYNC_MODE") or "smart",
                agent_mode=_os.environ.get("OMNICODE_AGENT_MODE") or "auto",
                debounce_ms=int(
                    _os.environ.get("OMNICODE_AGENT_DEBOUNCE_MS") or "1200"
                ),
            )
            agent_auto = decide_agent_auto(runtime_for_agent).to_dict()
        except Exception as exc:  # noqa: BLE001
            agent_auto = {
                "warning": f"{exc.__class__.__name__}: {exc}",
            }

        embedding_payload: Dict[str, Any]
        try:
            from omnicode_core.embeddings.models import embedding_status

            embedding_payload = embedding_status(
                deployment_mode=executor_mode,
            )
        except Exception as exc:  # noqa: BLE001
            embedding_payload = {
                "available": False,
                "loaded": False,
                "error_code": exc.__class__.__name__,
                "error": str(exc),
            }

        local_index_payload: Dict[str, Any]
        try:
            from omnicode_core.workspace.exact_index import SnapshotExactIndex

            local_index_workspace_id = (
                workspace_id or ws_root.name or "workspace"
            )
            exact_status = SnapshotExactIndex().status(
                workspace_id=local_index_workspace_id,
            )
            local_index_payload = {
                "workspace_id": local_index_workspace_id,
                "local_index_ready": bool(
                    int(exact_status.get("files") or 0) > 0
                    and int(exact_status.get("symbols") or 0) > 0
                ),
                "local_files": int(exact_status.get("files") or 0),
                "local_symbols": int(exact_status.get("symbols") or 0),
                "local_lines": int(exact_status.get("lines") or 0),
                "local_line_fts_available": bool(
                    exact_status.get("line_fts_available")
                ),
                "local_line_fts_reason": exact_status.get("line_fts_reason"),
                "schema_version": exact_status.get("schema_version"),
                "exact_indexed_revision": exact_status.get(
                    "exact_indexed_revision"
                ),
                "local_index_state_dir": str(
                    SnapshotExactIndex().store.workspaces_root
                ),
            }
        except Exception as exc:  # noqa: BLE001
            local_index_payload = {
                "local_index_ready": False,
                "warning": f"{exc.__class__.__name__}: {exc}",
            }

        capability_registry_payload: Dict[str, Any]
        language_matrix_payload: Dict[str, Any]
        try:
            from omnicode_core.capabilities.languages import (
                capability_matrix_payload,
            )
            from omnicode_core.capabilities.registry import (
                build_runtime_capabilities,
            )

            readiness = (
                sync_payload.get("index_readiness")
                if isinstance(sync_payload.get("index_readiness"), dict)
                else {}
            )
            capability_registry_payload = build_runtime_capabilities(
                cloud_available=bool(sync_payload.get("cloud_available")),
                local_index_ready=bool(
                    local_index_payload.get("local_index_ready")
                ),
                line_fts_available=bool(
                    local_index_payload.get("local_line_fts_available")
                ),
                embedding_available=bool(embedding_payload.get("available")),
                semantic_index_ready=bool(
                    readiness.get("semantic_index_ready")
                ),
                graph_index_ready=bool(readiness.get("graph_index_ready")),
            )
            language_matrix_payload = capability_matrix_payload()
        except Exception as exc:  # noqa: BLE001
            capability_registry_payload = {
                "warning": f"{exc.__class__.__name__}: {exc}",
            }
            language_matrix_payload = {}

        payload: Dict[str, Any] = {
            "ok": not warnings_list,
            "pid": _os.getpid(),
            "process_start_time": _PROCESS_START_TIME,
            "module_path": module_path,
            "module_sha1": module_sha1,
            "module_mtime": module_mtime,
            "python_executable": _sys.executable,
            "python_version": _platform.python_version(),
            "handler_version": _HANDLER_VERSION,
            "handler_features": list(_HANDLER_FEATURES),
            "registered_tools": registered_tools,
            "deprecated_aliases_present": deprecated,
            "expected_contract_versions": dict(_CONTRACT_VERSIONS),
            "tools_with_json_stamp": list(_TOOLS_WITH_JSON_STAMP),
            "workspace_root": str(ws_root),
            "workspace_root_source": ws_source,
            "workspace_id": workspace_id,
            "executor_mode": executor_mode,
            "backend_url": backend_url,
            "local_workspace_root": (
                _os.environ.get("OMNICODE_WORKSPACE_ROOT")
                or _os.environ.get("OMNICODE_WORKSPACE")
                or None
            ),
            "sync": sync_payload,
            "capability_contract": capability_contract,
            "embedding": embedding_payload,
            "local_index": local_index_payload,
            "capabilities": capability_registry_payload,
            "language_capabilities": language_matrix_payload,
            "agent_auto": agent_auto,
            "cwd": str(cwd_path),
            "workspace_root_matches_cwd": ws_root == cwd_path,
            "backend_workspace_root": backend_workspace_root,
            "backend_workspace_root_source": backend_workspace_root_source,
            "workspace_root_matches_backend": workspace_root_matches_backend,
            "workspace_root_warning": workspace_root_warning,
            "warnings": warnings_list,
        }
        # Note: _stamp injects contract_version="status.v1" + handler_version
        # for omni_status itself. expected_contract_versions above is the
        # per-tool reference table — callers compare each tool's response
        # contract_version against this map to detect a stale binding.
        _stamp(payload, tool="omni_status")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def discover_tools(
        query: str = "",
        matcher: str = "rule",
        format: str = "text",
    ) -> str:
        """Discover available OmniCode tools and their capabilities.

        Call with no query to get the full default tool listing plus the
        recommended pre-edit workflow.  Call with a free-text query —
        in **English or Chinese** — and the tool runs the workspace's
        Tool Intent Registry against it: tokeniser → per-tool keyword
        match (EN + ZH) → multilingual intent patterns → ranked output
        with ``why_matched`` annotations and a tailored next-step
        pipeline.

        Notes for AI editors:
          * The eight core tools below are the recommended surface.
          * ``omni_analyze`` / ``omni_edit`` / ``omni_intelligence`` are
            deprecated aliases that still work; this tool will only
            recommend them if the caller explicitly names them.
          * On zero query matches we fall back to the default listing
            so the caller never goes home empty-handed.
          * ``matcher='rule'`` (default) is the only implemented backend;
            ``matcher='embedding'`` is a reserved name for a future
            semantic backend and currently falls back to rule-based
            matching with a notice in the response.
          * ``format='text'`` (default) returns the human-readable
            listing. ``format='json'`` returns a structured envelope
            with ``handler_version`` + ``contract_version`` so audits
            can verify the live binding matches the on-disk source.

        Eight core tools:
          - omni_search:      search code (auto/semantic/symbol/text/hybrid/references)
          - omni_read:        read files (outline/symbols/full/imports/diagnostics/range)
          - omni_impact:      blast radius — callers / callees / risk / related tests
          - omni_diagnostics: lint / type / static analysis for a file
          - omni_context:     composer — outline + impact + memory + git in one call
          - omni_memory:      project memory (search/store/advisory)
          - omni_patch:       safe edit (preview / validate / apply / rollback)
          - omni_skill:       discover packaged workflow recipes (skills)

        Plus ``discover_tools`` (this tool) for introspection.
        """
        if (format or "text").lower() == "json":
            import os as _os

            cloud_available: Optional[bool] = None
            semantic_ready = False
            graph_ready = False
            backend_url = (
                _os.environ.get("OMNICODE_REMOTE")
                or _os.environ.get("OMNICODE_FASTAPI_BASE_URL")
                or ""
            )
            workspace_id = _os.environ.get("OMNICODE_WORKSPACE_ID") or None
            executor_mode = (
                _os.environ.get("OMNICODE_EXECUTOR_MODE")
                or _os.environ.get("OMNICODE_EXECUTOR")
                or "local"
            ).strip().lower()
            if backend_url and executor_mode in {"hybrid", "remote"}:
                try:
                    raw_status = await make_request(
                        "GET",
                        "/sync/status",
                        params={"workspace_id": workspace_id} if workspace_id else {},
                    )
                    cloud_status = (
                        raw_status.get("result", raw_status)
                        if isinstance(raw_status, dict)
                        else {}
                    )
                    backend_error = (
                        _backend_error_message(cloud_status)
                        if isinstance(cloud_status, dict)
                        else "invalid cloud status"
                    )
                    cloud_available = not bool(backend_error)
                    if cloud_available and isinstance(cloud_status, dict):
                        semantic_ready = bool(
                            cloud_status.get("semantic_index_ready")
                            or cloud_status.get("strict_semantic_safe")
                            or cloud_status.get("semantic_query_safe")
                        )
                        readiness = cloud_status.get("index_readiness_contract")
                        if isinstance(readiness, dict):
                            graph_ready = bool(readiness.get("graph_index_ready"))
                except Exception:
                    cloud_available = False

            capability_registry = _runtime_capability_registry_snapshot(
                cloud_available=cloud_available,
                semantic_index_ready=semantic_ready,
                graph_index_ready=graph_ready,
            )
            payload = _recommend_tools_payload(
                query,
                matcher=matcher,
                capability_registry=capability_registry,
            )
            _stamp(payload, tool="discover_tools")
            return json.dumps(payload, ensure_ascii=False, indent=2)
        return _recommend_tools(query, matcher=matcher)
        # Tool catalogue with per-tool keywords + scenarios + deprecation flag.
        tools_catalogue: List[Dict[str, Any]] = [
            {
                "name": "omni_context",
                "desc": "Composer — outline + impact + memory + git in one call",
                "scenario": "First call when starting a task; aggregates outline/impact/memory/git into one response with a token_budget.",
                "keywords": [
                    "context", "compose", "task", "understand", "before",
                    "summary", "lay of the land", "starting", "entry",
                    "investigate", "explore", "what does",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_search",
                "desc": "Search code (auto/semantic/symbol/text/hybrid/references)",
                "scenario": "Find something by name, by literal string, or by natural language; mode=references for cross-file usages.",
                "keywords": [
                    "search", "find", "locate", "grep", "lookup", "look up",
                    "symbol", "function", "method", "class", "where",
                    "references", "usages", "callers", "imports",
                    "semantic", "hybrid", "rrf", "fuzzy", "text",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_read",
                "desc": "Read files (outline/symbols/full/imports/diagnostics/range/relevant_chunks/symbol)",
                "scenario": "Read one file with the right granularity; use mode=outline first, then mode=symbol or mode=range to drill in.",
                "keywords": [
                    "read", "view", "open", "show", "print", "outline",
                    "structure", "signature", "signatures", "imports",
                    "lines", "range", "snippet", "function body",
                    "definition", "what does", "implementation",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_impact",
                "desc": "Blast radius — callers/callees/risk/suggested tests",
                "scenario": "Before any non-trivial edit; reports risk level + callers + callees + recommended tests.",
                "keywords": [
                    "impact", "blast", "radius", "risk", "callers", "callees",
                    "affected", "depend", "dependencies", "dependents",
                    "before changing", "before modifying", "before editing",
                    "safe to change", "ripple", "graph",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_diagnostics",
                "desc": "Lint / type / static-analysis diagnostics for a file",
                "scenario": "Get ruff + mypy + bandit (Py) or eslint + tsc (TS/JS) issues for a single file.",
                "keywords": [
                    "diagnostics", "lint", "linter", "type", "types",
                    "typecheck", "mypy", "ruff", "eslint", "tsc",
                    "errors", "warnings", "static", "analysis",
                    "issues", "problems",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_memory",
                "desc": "Project memory (search/store/advisory)",
                "scenario": "Recall prior solutions/mistakes/architecture decisions; advisory mode auto-recalls on file+symbol+task.",
                "keywords": [
                    "memory", "remember", "recall", "history", "learned",
                    "previous", "past", "advisory", "lesson", "lessons",
                    "store", "save", "note",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_patch",
                "desc": "Safe edit (preview / validate / apply / rollback / sessions)",
                "scenario": "Never write to disk directly; always go preview→validate→apply, keep session_id for rollback.",
                "keywords": [
                    "patch", "edit", "modify", "change", "write",
                    "preview", "validate", "apply", "rollback", "rewrite",
                    "fix", "refactor", "session", "diff", "revert",
                    "undo", "safely", "safe edit",
                ],
                "deprecated": False,
            },
            {
                "name": "omni_skill",
                "desc": "Discover packaged workflow recipes (impact-review, safe-refactor, …)",
                "scenario": "Look up a multi-step recipe before improvising — recipes bundle the right tool sequence.",
                "keywords": [
                    "skill", "recipe", "workflow", "playbook", "guide",
                    "how to", "best practice", "pipeline",
                ],
                "deprecated": False,
            },
            {
                "name": "discover_tools",
                "desc": "Find what's available — keyword + intent based",
                "scenario": "When unsure which tool fits the task.",
                "keywords": ["discover", "tools", "what tools", "available"],
                "deprecated": False,
            },
            # Deprecated aliases — only matched when user names them explicitly.
            {
                "name": "omni_analyze",
                "desc": "[deprecated alias] Use omni_impact",
                "scenario": "Kept for old MCP configs; new clients should use omni_impact.",
                "keywords": ["omni_analyze"],
                "deprecated": True,
                "alias_for": "omni_impact",
            },
            {
                "name": "omni_edit",
                "desc": "[deprecated alias] Use omni_patch (or omni_edit ai_edit when LLM_ROUTER=true)",
                "scenario": "Kept for old MCP configs; new clients should use omni_patch.",
                "keywords": ["omni_edit"],
                "deprecated": True,
                "alias_for": "omni_patch",
            },
            {
                "name": "omni_intelligence",
                "desc": "[deprecated alias] Use omni_context",
                "scenario": "Kept for old MCP configs; new clients should use omni_context.",
                "keywords": ["omni_intelligence"],
                "deprecated": True,
                "alias_for": "omni_context",
            },
        ]

        default_pipeline = [
            "1. omni_skill(action='list')             — see if a recipe exists",
            "2. omni_context(file=… or task=…)       — gather outline + impact + memory + git",
            "3. omni_impact(symbol=…)                — check blast radius before editing",
            "4. omni_diagnostics(file=…)             — see existing lint/type issues",
            "5. omni_search(mode='references', …)    — find every callsite of the symbol",
            "6. omni_patch(action='preview', …)      — render the diff",
            "7. omni_patch(action='validate', …)     — run static checks on the patch",
            "8. omni_patch(action='apply', …)        — write + create rollback hook",
            "9. omni_patch(action='rollback', session_id=…)  — undo on regret",
        ]

        # ------------------------------------------------------------------
        # Empty query → unchanged default listing + full pipeline.
        # ------------------------------------------------------------------
        if not (query and query.strip()):
            non_deprecated = [t for t in tools_catalogue if not t["deprecated"]]
            lines = ["📦 OmniCode tools:\n"]
            for t in non_deprecated:
                lines.append(f"  • {t['name']:<18} {t['desc']}")
            lines.append("")
            lines.append("💡 Recommended flow before any edit:")
            for step in default_pipeline:
                lines.append(f"   {step}")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # Non-empty query → tokenise + intent-match + score.
        # ------------------------------------------------------------------
        # 1) Tokenise: lowercase + word-boundary split, drop stop-words.
        STOPWORDS = {
            "i", "me", "my", "we", "us", "you", "your", "to", "the", "a", "an",
            "and", "or", "of", "in", "on", "for", "is", "be", "with", "before",
            "after", "this", "that", "these", "those", "it", "its", "want",
            "need", "should", "would", "could", "can", "do", "does", "have",
            "has", "had", "but", "so", "as", "at", "by", "from", "into",
            "what", "which", "how",
        }
        raw_tokens = re.findall(r"[A-Za-z_]+", query.lower())
        tokens = [t for t in raw_tokens if t not in STOPWORDS and len(t) >= 2]

        explicit_alias = None
        for t in tools_catalogue:
            if t["deprecated"] and t["name"].lower() in query.lower():
                explicit_alias = t["name"]
                break

        # 2) Intent patterns (multi-token rules with target tools).
        intents: List[Tuple[List[List[str]], List[str], str]] = [
            # (any-of token groups all required, target tools, why_matched label)
            (
                [["preview", "validate", "apply", "rollback", "diff", "revert", "undo"]],
                ["omni_patch"],
                "intent:safe-edit-flow",
            ),
            (
                [["safely", "safe"], ["edit", "modify", "change", "patch"]],
                ["omni_patch"],
                "intent:safe-edit",
            ),
            (
                [["risk", "impact", "blast", "radius", "callers", "callees",
                  "affected", "ripple", "depend"]],
                ["omni_impact"],
                "intent:risk-analysis",
            ),
            (
                [["references", "usages", "callsites", "callsite"]],
                ["omni_search"],
                "intent:references-mode",
            ),
            (
                [["understand", "investigate", "explore", "context"],
                 ["function", "method", "class", "symbol", "code", "file"]],
                ["omni_context", "omni_read", "omni_impact", "omni_search"],
                "intent:understand-before-edit",
            ),
            (
                [["lint", "linter", "type", "typecheck", "diagnostics",
                  "errors", "warnings", "issues", "mypy", "ruff", "eslint", "tsc"]],
                ["omni_diagnostics"],
                "intent:diagnostics",
            ),
            (
                [["recall", "remember", "memory", "lesson", "learned",
                  "past", "previous", "history"]],
                ["omni_memory"],
                "intent:memory",
            ),
            (
                [["recipe", "workflow", "playbook", "skill"]],
                ["omni_skill"],
                "intent:skill",
            ),
            (
                [["search", "find", "locate", "grep", "lookup"]],
                ["omni_search"],
                "intent:search",
            ),
            (
                [["read", "view", "show", "open", "print", "outline", "signature"]],
                ["omni_read"],
                "intent:read",
            ),
        ]

        def _intent_matches(token_set: set, groups: List[List[str]]) -> bool:
            """Every group must have at least one matching token."""
            for grp in groups:
                if not any(g in token_set for g in grp):
                    return False
            return True

        token_set = set(tokens)
        scores: Dict[str, int] = {}
        why: Dict[str, List[str]] = {}

        # 3a) Per-tool keyword overlap (skip deprecated unless explicit).
        for t in tools_catalogue:
            if t["deprecated"] and t["name"] != explicit_alias:
                continue
            hits = [k for k in t["keywords"] if k in token_set]
            if hits:
                scores[t["name"]] = scores.get(t["name"], 0) + 3 * len(hits)
                why.setdefault(t["name"], []).extend(
                    f"keyword:{h}" for h in hits[:3]
                )

        # 3b) Intent pattern bonus.
        for groups, targets, label in intents:
            if _intent_matches(token_set, groups):
                for tgt in targets:
                    scores[tgt] = scores.get(tgt, 0) + 5
                    why.setdefault(tgt, []).append(label)

        # 4) If user explicitly named a deprecated alias, surface it AND its
        # modern replacement (with a warning tag).
        if explicit_alias:
            modern = next(
                (t["alias_for"] for t in tools_catalogue if t["name"] == explicit_alias),
                None,
            )
            scores[explicit_alias] = scores.get(explicit_alias, 0) + 4
            why.setdefault(explicit_alias, []).append("named:deprecated_alias")
            if modern:
                scores[modern] = scores.get(modern, 0) + 6
                why.setdefault(modern, []).append(f"replacement_for:{explicit_alias}")

        # ------------------------------------------------------------------
        # 5) Build response. Fallback on zero matches.
        # ------------------------------------------------------------------
        if not scores:
            lines = [
                f"🔍 No direct keyword match for '{query}'.",
                "",
                "📦 Showing default tool listing — pick one of:",
                "",
            ]
            for t in tools_catalogue:
                if t["deprecated"]:
                    continue
                lines.append(f"  • {t['name']:<18} {t['desc']}")
            lines.append("")
            lines.append("💡 Default workflow before any edit:")
            for step in default_pipeline:
                lines.append(f"   {step}")
            return "\n".join(lines)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        info_by_name = {t["name"]: t for t in tools_catalogue}
        lines = [f"🔍 Tools matching '{query}' (ranked):\n"]
        for name, score in ranked[:6]:
            info = info_by_name[name]
            tag = " ⚠️ deprecated alias" if info.get("deprecated") else ""
            lines.append(f"  • {name}{tag}  (score={score})")
            lines.append(f"      {info['desc']}")
            lines.append(f"      ↳ {info['scenario']}")
            if why.get(name):
                shown = ", ".join(why[name][:4])
                lines.append(f"      why_matched: {shown}")
            lines.append("")

        # If the top match is omni_patch, append the safe-edit pipeline as
        # an actionable hint — saves the AI an extra discovery call.
        top_name = ranked[0][0]
        if top_name == "omni_patch":
            lines.append("💡 Safe edit pipeline:")
            lines.append("   1. omni_patch(action='preview', file=…, content=…)")
            lines.append("   2. omni_patch(action='validate', file=…, content=…)")
            lines.append("   3. omni_patch(action='apply', file=…, content=…)")
            lines.append("   4. omni_patch(action='rollback', session_id=…)  # if needed")
        elif top_name in ("omni_context", "omni_impact", "omni_read"):
            lines.append("💡 Pre-edit understanding pipeline:")
            lines.append("   1. omni_context(task=… or file=…)")
            lines.append("   2. omni_search(mode='references', query=…)")
            lines.append("   3. omni_read(file=…, mode='symbol', symbol=…)")
            lines.append("   4. omni_impact(symbol=…)")

        return "\n".join(lines)
