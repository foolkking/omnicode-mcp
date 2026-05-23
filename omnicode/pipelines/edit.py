"""
Edit Pipeline (STAGE 4.6 + 4.7 + 5.4 + 6.9 + 7.5)
==================================================
Wraps the LLM router with full Smart Token Compressor support, role-based
provider selection, Git-aware history advisories, automatic Guard escalation,
and long-term-memory injection.

Key behaviours
--------------
* **Role-aware budget** — the token manager is rebuilt per call against the
  provider that will actually serve the ``edit`` role (`STAGE 4.7`).  This
  means a small local model gets a tight budget, a 200K-window Claude gets
  to keep most of the file context, and the manager is never wrong about
  which model is on the other end of the wire.

* **Guard escalation** — after the first edit completes, the Proactive Guard
  runs.  If it finds *errors* (not just warnings), the pipeline retries with
  the ``review`` role (typically a higher-quality model) and feeds the
  Guard's report back into the prompt so the LLM can self-correct
  (`STAGE 6.9`).

* **History advisory** — `GitHistoryAnalyzer` produces a 1-paragraph risk
  summary that gets injected as low-priority context so the LLM thinks
  twice before stripping defensive code (`STAGE 5.4`).

* **Memory injection** — relevant long-term memories are searched up-front
  and added as low-priority context items.  Successful edits that produced
  novel learnings get written back as new memories so future edits can
  benefit (`STAGE 7.5`).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from omnicode.guard.analyzer import ProactiveGuard
from omnicode.guard.models import GuardResult, IssueSeverity
from omnicode.llm.base import LLMMessage, Role
from omnicode.llm.router import LLMRouter, RoutingStrategy
from omnicode.llm.token_manager import (
    ContextItem,
    TokenManager,
)

logger = logging.getLogger(__name__)


class EditRequest:
    """Request payload for file edit operation."""

    def __init__(
        self,
        target_file: str,
        instructions: str,
        code_edit: str,
        language: Optional[str] = None,
    ) -> None:
        self.target_file = target_file
        self.instructions = instructions
        self.code_edit = code_edit
        self.language = language


class EditPipeline:
    """Pipeline for AI-assisted file editing with smart token compression."""

    # Severities that trigger a Guard-escalation retry. Anything below ERROR
    # is considered non-fatal (warnings & info are surfaced but tolerated).
    _ESCALATION_SEVERITIES = {IssueSeverity.ERROR}

    # Files larger than these thresholds switch from "rewrite the whole file"
    # mode to "emit SEARCH/REPLACE patch blocks" mode.  Whole-file rewrites
    # don't scale — Gemini-Flash output is capped around 8K tokens, Claude
    # Haiku around 4K, so a 2,000-line file simply cannot be reproduced
    # verbatim and the model truncates.  Patch mode lets the LLM only
    # describe the diff.
    _PATCH_MODE_LINE_THRESHOLD = 400
    _PATCH_MODE_CHAR_THRESHOLD = 24_000

    # Sentinel markers for the patch protocol (Aider-style).
    _PATCH_BEGIN = "<<<<<<< SEARCH"
    _PATCH_DIVIDER = "======="
    _PATCH_END = ">>>>>>> REPLACE"

    # Symbol-surgical mode parameters.  When the user's instruction names a
    # specific symbol that the AST can locate uniquely, we extract just that
    # symbol's source (plus padding) and ask the LLM to rewrite ONLY that
    # snippet.  This avoids both whole-file truncation AND patch-mode anchor
    # hallucination.
    _SURGICAL_MIN_FILE_LINES = 60       # below this, whole-file is fine
    _SURGICAL_PADDING = 3                # extra lines before/after for context
    _SURGICAL_MAX_SYMBOL_LINES = 200    # if symbol body is bigger, skip surgical

    def __init__(self, write_pipeline: Any = None) -> None:
        self.write_pipeline = write_pipeline
        self.router = LLMRouter()
        self.guard = ProactiveGuard()

    def get_stats(self) -> Dict[str, Any]:
        return {"status": "active", "router": self.router.get_status()}

    # ------------------------------------------------------------------
    async def process_edit(self, request: EditRequest, save_to_file: bool = True):
        start_time = time.time()
        file_path = request.target_file
        logger.info("Processing edit for %s", file_path)

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            original_content = f.read()

        language = request.language or os.path.splitext(file_path)[1].lstrip(".") or "python"

        # ---- 1. Build context items (instructions, sketch, original, blame, history, memory)
        keep_symbols = self._guess_keep_symbols(request.code_edit)
        items = self._build_base_context(
            request, original_content, language, keep_symbols
        )

        # Long-term memory injection (STAGE 7.5)
        memory_snippet = await self._collect_memory_advisory(request.instructions, file_path)
        if memory_snippet:
            items.append(
                ContextItem(
                    content=memory_snippet,
                    priority=18,
                    role="comment",
                    language="text",
                    label="memory",
                )
            )

        # ---- 2. Try symbol-surgical mode first (most reliable for large files)
        #         If the instruction names a single symbol that AST can locate,
        #         we extract just that symbol's source, send it to the LLM with
        #         a "rewrite this snippet" prompt, then splice the result back
        #         into the original file deterministically.  Avoids both
        #         whole-file truncation AND patch-mode anchor hallucination.
        surgical = self._try_symbol_surgical(
            request=request,
            original_content=original_content,
            language=language,
        )
        if surgical is not None:
            logger.info(
                "Symbol-surgical mode: targeting %s (lines %d-%d, %d snippet lines)",
                surgical["target_symbol"],
                surgical["snippet_start_line"],
                surgical["snippet_end_line"],
                surgical["snippet_end_line"] - surgical["snippet_start_line"] + 1,
            )

        # ---- 3. First pass: edit role with role-aware token budget
        first = await self._run_pass(
            request=request,
            items=items,
            language=language,
            file_path=file_path,
            original_content=original_content,
            role="edit",
            save_to_file=save_to_file,
            attempt=1,
            surgical=surgical,
        )

        # ---- 3. Guard escalation: if the edit produced ERROR-level issues, re-run
        #         with the 'review' role and the Guard report folded back into the prompt.
        guard_result = first["guard_result"]
        escalated = False
        escalation_payload: Optional[Dict[str, Any]] = None
        if (
            first["edit_success"]
            and guard_result is not None
            and self._guard_has_errors(guard_result)
        ):
            logger.warning(
                "Guard found %d errors in initial edit — escalating to 'review' role.",
                guard_result.error_count,
            )
            # Add a Guard-feedback context item so the LLM knows what to fix.
            feedback = self._format_guard_feedback(guard_result)
            esc_items = list(items)
            esc_items.append(
                ContextItem(
                    content=feedback,
                    priority=110,  # higher than instructions on purpose
                    role="instruction",
                    language="text",
                    label="guard_feedback",
                )
            )
            # Use the just-edited file as the new "original" context so the
            # review pass starts from the broken state and improves it.
            with open(file_path, "r", encoding="utf-8") as f:
                broken_content = f.read()
            for it in esc_items:
                if it.role == "context" and it.label and it.label.endswith(
                    os.path.basename(file_path)
                ):
                    it.content = broken_content
                    break
            escalated = True
            second = await self._run_pass(
                request=request,
                items=esc_items,
                language=language,
                file_path=file_path,
                original_content=broken_content,
                role="review",
                save_to_file=save_to_file,
                attempt=2,
                surgical=None,  # review pass operates on the whole broken file
            )
            escalation_payload = second
            # The escalation result becomes the canonical outcome.
            first = second

        # ---- 4. Memory write-back: persist a "solution" memory on success
        if first.get("edit_success") and first.get("guard_result") is not None and not self._guard_has_errors(first["guard_result"]):
            try:
                await self._write_memory(
                    instructions=request.instructions,
                    file_path=file_path,
                    summary=first.get("summary", ""),
                )
            except Exception as exc:
                logger.debug("Memory write-back skipped: %s", exc)

        processing_time = time.time() - start_time

        class EditResult:
            def __init__(self) -> None:
                self.file_path = file_path
                self.success = bool(first.get("edit_success") and first.get("guard_clean", True))
                self.instructions = request.instructions
                self.summary = first.get("summary", "")
                self.quality_score = 0.9 if first.get("guard_clean", True) else 0.5
                self.gemini_edit_success = first.get("edit_success", False)
                self.format_success = first.get("guard_clean", True)
                self.error_correction_attempts = 1 if escalated else 0
                self.total_gemini_calls = 2 if escalated else 1
                self.processing_time_seconds = processing_time
                self.original_content = original_content
                self.final_content = first.get("final_content", original_content)
                self.gemini_errors = first.get("errors", [])
                self.format_errors = first.get("format_errors", [])
                self.warnings = first.get("warnings", [])
                self.token_stats = first.get("token_stats", {})
                self.role_used = first.get("role_used")
                self.escalated = escalated
                self.escalation = (
                    {
                        "triggered": True,
                        "first_pass_role": "edit",
                        "escalation_role": escalation_payload.get("role_used") if escalation_payload else None,
                        "first_pass_errors": guard_result.error_count if guard_result else 0,
                    }
                    if escalated
                    else {"triggered": False}
                )

        return EditResult()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_base_context(
        self,
        request: EditRequest,
        original_content: str,
        language: str,
        keep_symbols: List[str],
    ) -> List[ContextItem]:
        file_path = request.target_file
        items: List[ContextItem] = [
            ContextItem(
                content=request.instructions,
                priority=100,
                role="instruction",
                language=language,
                label="instructions",
            ),
            ContextItem(
                content=request.code_edit,
                priority=90,
                role="target",
                language=language,
                label="code_edit_sketch",
                keep_symbols=keep_symbols,
            ),
            ContextItem(
                content=original_content,
                priority=50,
                role="context",
                language=language,
                label=os.path.basename(file_path),
                keep_symbols=keep_symbols,
            ),
        ]
        # Symbol anchors — verbatim source of every symbol mentioned in
        # instructions / sketch.  Critical for patch-mode editing: gives the
        # LLM an authoritative SEARCH anchor instead of letting it hallucinate
        # one (the bug where it produced `async def cleanup(): pass`).  These
        # are high-priority context so they survive token compression.
        anchors = self._collect_symbol_anchors(
            request=request,
            original_content=original_content,
            language=language,
            keep_symbols=keep_symbols,
        )
        for anchor in anchors:
            items.append(
                ContextItem(
                    content=anchor["content"],
                    priority=80,
                    role="comment",
                    language=language,
                    label=f"anchor:{anchor['name']}",
                )
            )
        git_blame = self._collect_git_blame(file_path)
        if git_blame:
            items.append(
                ContextItem(
                    content=git_blame,
                    priority=20,
                    role="comment",
                    language="text",
                    label="git_blame",
                )
            )
        history_advisory = self._collect_history_advisory(file_path)
        if history_advisory:
            items.append(
                ContextItem(
                    content=history_advisory,
                    priority=15,
                    role="comment",
                    language="text",
                    label="git_history",
                )
            )
        return items

    async def _run_pass(
        self,
        *,
        request: EditRequest,
        items: List[ContextItem],
        language: str,
        file_path: str,
        original_content: str,
        role: str,
        save_to_file: bool,
        attempt: int,
        surgical: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one full LLM round: compress → call → save → guard. Returns a dict.

        When ``surgical`` is provided (a dict from ``_try_symbol_surgical``),
        we ask the LLM to rewrite ONLY a small snippet around the target
        symbol, then splice the result back into ``original_content``.  This
        bypasses both whole-file truncation and patch-mode anchor
        hallucination.
        """
        # ---- Token manager scoped to the role we're about to invoke -----------
        try:
            tm = TokenManager.for_role(self.router, role=role, task=role)
        except Exception:
            # Fall back to first available provider.
            tm = TokenManager(next(iter(self.router.providers.values())))
        budget = tm.budget_info()
        original_tokens = tm.count_tokens(original_content)

        # ---- Compress to fit ---------------------------------------------------
        reserved = 1024
        kept_items, prune_report = tm.compress_for_llm(items, reserved_tokens=reserved)

        compressed_content = next(
            (
                it.content
                for it in kept_items
                if it.label and it.label.endswith(os.path.basename(file_path))
            ),
            original_content,
        )
        compressed_tokens = tm.count_tokens(compressed_content)

        # ---- Decide mode: surgical > patch > whole-file --------------------
        use_surgical = surgical is not None
        use_patch_mode = (not use_surgical) and (
            len(original_content.splitlines()) > self._PATCH_MODE_LINE_THRESHOLD
            or len(original_content) > self._PATCH_MODE_CHAR_THRESHOLD
        )

        # ---- Build prompt ------------------------------------------------------
        if use_surgical:
            target_name = surgical["target_symbol"]
            snippet = surgical["snippet"]
            snippet_start = surgical["snippet_start_line"]
            snippet_end = surgical["snippet_end_line"]
            system_prompt = (
                f"You are an expert software engineer making a PRECISE LOCAL EDIT "
                f"to a single symbol named `{target_name}` inside a larger file. "
                f"You will receive ONLY the snippet (lines {snippet_start}-{snippet_end}) "
                f"that contains that symbol.\n\n"
                "OUTPUT CONTRACT — read carefully:\n"
                "1. Reply with EXACTLY ONE markdown code block and nothing else.\n"
                f"2. The code block MUST start with ```{language} and end with ```.\n"
                "3. Inside the block put ONLY runnable source code — no narration, "
                "no 'thinking' notes, no preface.\n"
                f"4. Reproduce the snippet's lines verbatim, applying ONLY the "
                "requested change. Preserve indentation, blank lines, and surrounding "
                f"context lines (the symbol may not start on the very first line).\n"
                "5. Do NOT output the entire file — only this snippet's updated "
                "version.\n"
                "6. Do NOT include line numbers, '...' placeholders, or summaries — "
                "every line you emit will REPLACE the corresponding line in the file.\n"
                "7. If the request is ambiguous or you cannot make the change "
                "safely, output the snippet unchanged.\n"
            )
        elif use_patch_mode:
            system_prompt = (
                "You are an expert software engineer making PRECISE EDITS to a "
                "large existing file. The file is too long to rewrite verbatim, "
                "so you will output ONE OR MORE patch blocks instead.\n\n"
                "PATCH FORMAT — follow EXACTLY:\n"
                f"```{language}\n"
                f"{self._PATCH_BEGIN}\n"
                "<a few CONSECUTIVE lines copied verbatim from the original file — "
                "enough to uniquely identify where to make the change>\n"
                f"{self._PATCH_DIVIDER}\n"
                "<the replacement lines that should appear in the file>\n"
                f"{self._PATCH_END}\n"
                "```\n\n"
                "RULES:\n"
                "1. The SEARCH section MUST match the original file character-for-"
                "character including indentation, trailing spaces, and blank lines.\n"
                "2. Make the SEARCH section the smallest unique anchor possible — "
                "usually 3 to 15 lines that surround the edit.\n"
                "3. To INSERT new code, copy a small surrounding block as SEARCH "
                "and include both the original context and the inserted code in "
                "REPLACE.\n"
                "4. To DELETE code, leave the REPLACE section empty (a divider "
                "immediately followed by the end marker).\n"
                "5. Emit MULTIPLE patch blocks when there are multiple unrelated "
                "edits — one block per logical change, all inside the same "
                f"```{language} fenced code block.\n"
                "6. Output ONLY the fenced code block. No prose, no narration, no "
                "explanations before or after.\n"
                "7. If you cannot make the change safely, output an empty fenced "
                f"```{language} block — DO NOT output prose explaining why.\n"
            )
        else:
            system_prompt = (
                "You are an expert software engineer specializing in refactoring and precise editing. "
                "You will be provided with a file's original content and instructions to edit it. "
                "You must apply the changes and output the COMPLETE updated file content.\n\n"
                "OUTPUT CONTRACT — read carefully:\n"
                "1. Reply with EXACTLY ONE markdown code block and nothing else.\n"
                f"2. The code block MUST start with ```{language} and end with ```.\n"
                "3. Inside the block put ONLY runnable source code — no English/Chinese narration, "
                "no 'thinking' notes, no 'Sure, here is...' preface.\n"
                "4. Do NOT include any text before or after the code block.\n"
                "5. Reproduce the entire file verbatim, applying only the requested changes. "
                "Preserve every line you are not asked to modify, including imports, comments, "
                "blank lines, and trailing newlines.\n"
                "6. If the request is ambiguous or you cannot make the change safely, output the "
                "ORIGINAL file unchanged inside the code block — DO NOT output prose explaining why.\n"
            )
        if role == "review":
            system_prompt += (
                "\nThis is a SECOND-PASS review: a previous edit attempt produced static-analysis "
                "errors. You are receiving the broken file plus the Guard report. Fix every "
                "reported issue while preserving the user's original intent and any defensive "
                "code highlighted by the git history advisory."
            )
        user_prompt_parts: List[str] = [f"Target File: {file_path}\nLanguage: {language}\n"]

        if use_surgical:
            # Surgical mode renders a focused, no-distractions prompt so the
            # LLM only sees what it needs to rewrite.  Original-file context
            # and AST anchors are deliberately omitted — they would only
            # tempt the model to invent things it can't see.
            target_name = surgical["target_symbol"]
            snippet = surgical["snippet"]
            snippet_start = surgical["snippet_start_line"]
            snippet_end = surgical["snippet_end_line"]
            user_prompt_parts.append(f"Instructions:\n{request.instructions}\n")
            if request.code_edit and request.code_edit.strip() not in {"#", "//", "/*"}:
                user_prompt_parts.append(
                    f"User-supplied edit sketch (advisory):\n```{language}\n"
                    f"{request.code_edit}\n```\n"
                )
            user_prompt_parts.append(
                f"Snippet to edit — symbol `{target_name}` "
                f"(lines {snippet_start}-{snippet_end}):\n"
                f"```{language}\n{snippet}\n```\n"
            )
            user_prompt_parts.append(
                f"Output the updated snippet above (lines {snippet_start}-{snippet_end}) "
                f"with the requested change applied. Reply ONLY with the updated "
                f"snippet inside a single ```{language} fenced code block."
            )
        else:
            for item in kept_items:
                if item.role == "instruction":
                    # Render Guard feedback distinctly so the LLM sees it first.
                    if item.label == "guard_feedback":
                        user_prompt_parts.insert(
                            1,
                            f"Static-analysis report from the previous attempt — fix EVERYTHING below:\n{item.content}\n",
                        )
                    else:
                        user_prompt_parts.append(f"Instructions:\n{item.content}\n")
                elif item.role == "target":
                    user_prompt_parts.append(f"Code Edit Context:\n{item.content}\n")
                elif item.label and item.label.startswith("anchor:"):
                    anchor_name = item.label.split(":", 1)[1]
                    user_prompt_parts.append(
                        f"Authoritative Symbol Anchor for `{anchor_name}` — "
                        f"copy these lines VERBATIM if you patch this symbol:\n"
                        f"```{language}\n{item.content}\n```\n"
                    )
                elif item.label == "git_blame":
                    user_prompt_parts.append(f"Git Blame Context:\n{item.content}\n")
                elif item.label == "git_history":
                    user_prompt_parts.append(f"Git History Advisory:\n{item.content}\n")
                elif item.label == "memory":
                    user_prompt_parts.append(f"Relevant Past Memories:\n{item.content}\n")
                else:
                    user_prompt_parts.append(
                        f"Original File Content:\n```{language}\n{item.content}\n```\n"
                    )
            if use_patch_mode:
                user_prompt_parts.append(
                    "Apply the change(s) above. Remember: output ONLY one or more "
                    f"{self._PATCH_BEGIN} ... {self._PATCH_END} blocks inside a "
                    f"single ```{language} fenced code block. Do NOT rewrite the "
                    "whole file."
                )
            else:
                user_prompt_parts.append(
                    "Please edit this file and return the COMPLETE updated file content inside a markdown code block."
                )
        user_prompt = "\n".join(user_prompt_parts)

        messages = [
            LLMMessage(role=Role.SYSTEM, content=system_prompt),
            LLMMessage(role=Role.USER, content=user_prompt),
        ]
        cost_check = tm.check_messages_cost(messages)
        if not cost_check["ok"]:
            logger.warning("[%s pass] Cost guard tripped: %s", role, cost_check.get("warning"))

        logger.info(
            "[%s pass] budget=%s · original=%d compressed=%d (saved %.1f%%)",
            role,
            budget,
            original_tokens,
            compressed_tokens,
            (1 - compressed_tokens / max(1, original_tokens)) * 100,
        )

        # ---- LLM call ---------------------------------------------------------
        edit_success = False
        errors: List[str] = []
        final_content = original_content
        try:
            response = await self.router.complete(
                messages=messages,
                strategy=RoutingStrategy.QUALITY_FIRST,
                task=role,  # forwarded into get_provider_for / chain resolution
            )
            llm_text = response.content or ""
            extracted = self._extract_code_block(llm_text, language)
            if extracted is None:
                # The LLM returned prose / thinking / a refusal instead of a
                # code block.  REFUSE to overwrite the file with that — this
                # was the bug that wiped mcp_server.py with a "thinking" log.
                preview = llm_text.strip().splitlines()[0:3]
                preview_str = " | ".join(preview)[:300]
                errors.append(
                    f"LLM did not return a fenced code block. "
                    f"First lines were: {preview_str!r}"
                )
                logger.error(
                    "[%s pass] No code block in LLM response — keeping the "
                    "original file untouched. Preview: %s",
                    role, preview_str,
                )
                final_content = original_content
                edit_success = False
            elif use_surgical:
                # Surgical mode: the LLM rewrote a small snippet.  Splice it
                # back into the original file at the exact line range we
                # extracted from.
                if self._looks_like_prose(extracted, language):
                    errors.append(
                        "LLM's snippet rewrite looks like prose, not source code. "
                        "Refusing to overwrite the snippet."
                    )
                    logger.error(
                        "[%s pass] Surgical mode: extracted block looks like "
                        "prose — keeping the original file.", role,
                    )
                    final_content = original_content
                    edit_success = False
                else:
                    final_content = self._splice_snippet(
                        original_content,
                        new_snippet=extracted,
                        start_line=surgical["snippet_start_line"],
                        end_line=surgical["snippet_end_line"],
                    )
                    edit_success = True
                    logger.info(
                        "[%s pass] Surgical mode spliced %d line(s) into %s "
                        "at lines %d-%d.",
                        role,
                        len(extracted.splitlines()),
                        surgical["target_symbol"],
                        surgical["snippet_start_line"],
                        surgical["snippet_end_line"],
                    )
            elif use_patch_mode:
                # Patch mode: the block contains SEARCH/REPLACE blobs which
                # we apply locally to the original file.
                patched, applied, patch_errors = self._apply_search_replace_patches(
                    original_content, extracted
                )
                if patch_errors:
                    errors.extend(patch_errors)
                if applied == 0:
                    errors.append(
                        "LLM returned no applicable SEARCH/REPLACE blocks. "
                        "Either the SEARCH text didn't match the file or the "
                        "patch syntax was malformed. Original file kept."
                    )
                    logger.error(
                        "[%s pass] Patch-mode produced 0 applicable blocks — "
                        "keeping the original file.",
                        role,
                    )
                    final_content = original_content
                    edit_success = False
                else:
                    final_content = patched
                    edit_success = True
                    logger.info(
                        "[%s pass] Patch mode applied %d block(s) cleanly.",
                        role, applied,
                    )
            elif self._looks_like_prose(extracted, language):
                # Even inside a fenced block, the LLM might have just typed
                # prose ("Sure, here's the change..."). Reject and keep the
                # original.
                errors.append(
                    "LLM returned a code block whose contents look like prose, "
                    "not source code. Refusing to overwrite the file."
                )
                logger.error(
                    "[%s pass] Extracted block looks like prose — keeping "
                    "the original file untouched.", role,
                )
                final_content = original_content
                edit_success = False
            else:
                final_content = extracted
                edit_success = True
        except Exception as exc:
            logger.error("[%s pass] LLM call failed: %s", role, exc)
            errors.append(str(exc))

        if edit_success and save_to_file:
            # Final safety net: refuse to write if the new content drastically
            # shrinks the file. This catches "LLM emitted only a thinking
            # snippet" or "LLM truncated mid-file" failures that slipped past
            # the prose detector.  We allow shrinkage when the user explicitly
            # asked for deletion (heuristic: instruction contains delete /
            # remove / 删除 / 移除).  Patch mode is exempt because we already
            # applied deterministic SEARCH/REPLACE blocks against the original
            # — anything we produced there is by construction the original
            # plus localised diffs.
            allow_shrink = use_patch_mode or use_surgical or any(
                kw in (request.instructions or "").lower()
                for kw in ("delete", "remove", "drop", "strip", "purge",
                           "删除", "移除", "去掉", "清理")
            )
            orig_lines = max(1, len(original_content.splitlines()))
            new_lines = len(final_content.splitlines())
            shrink_ratio = new_lines / orig_lines
            if not allow_shrink and orig_lines >= 30 and shrink_ratio < 0.5:
                errors.append(
                    f"Refusing to write: new content has {new_lines} lines vs "
                    f"original {orig_lines} (shrunk to {shrink_ratio:.0%}). "
                    "This usually means the LLM truncated the file. Set the "
                    "instruction to mention 'delete' / 'remove' / '删除' if "
                    "this shrinkage is intentional."
                )
                logger.error(
                    "[%s pass] File-shrink guard tripped: %d -> %d lines (%.0f%%). "
                    "Keeping the original file.",
                    role, orig_lines, new_lines, shrink_ratio * 100,
                )
                final_content = original_content
                edit_success = False
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_content)
                logger.info("[%s pass] Saved %s", role, file_path)

        # ---- Guard ------------------------------------------------------------
        guard_result: Optional[GuardResult] = None
        format_errors: List[Any] = []
        warnings: List[str] = []
        guard_clean = True
        if edit_success:
            try:
                guard_result = await self.guard.check(file_path)
                guard_clean = guard_result.is_clean and not self._guard_has_errors(guard_result)
                if guard_result.errors:
                    format_errors.append(guard_result.errors)
                if guard_result.warnings:
                    warnings.append(guard_result.warnings)
            except Exception as exc:
                logger.warning("[%s pass] Proactive guard failed: %s", role, exc)
                warnings.append(f"Guard check failed: {exc}")

        return {
            "role_used": role,
            "attempt": attempt,
            "edit_success": edit_success,
            "errors": errors,
            "final_content": final_content,
            "guard_result": guard_result,
            "guard_clean": guard_clean,
            "format_errors": format_errors,
            "warnings": warnings,
            "token_stats": {
                "original_tokens": original_tokens,
                "compressed_tokens": compressed_tokens,
                "savings_pct": round(
                    (1 - compressed_tokens / max(1, original_tokens)) * 100, 2
                ),
                "cost_check": cost_check,
                "prune_report": prune_report,
                "budget_info": budget,
            },
            "summary": (
                f"[{role}] {budget.get('model', 'unknown')} · tokens "
                f"{original_tokens}→{compressed_tokens} "
                f"({prune_report.get('items_kept', 0)}/{prune_report.get('items_in', 0)} ctx)"
            ),
        }

    @classmethod
    def _guard_has_errors(cls, result: GuardResult) -> bool:
        return any(i.severity in cls._ESCALATION_SEVERITIES for i in result.issues)

    @staticmethod
    def _format_guard_feedback(result: GuardResult) -> str:
        lines = [
            f"Guard summary: {result.summary()}",
            f"Tools run: {', '.join(result.tools_run) or '(none)'}",
        ]
        if result.tools_skipped:
            lines.append(f"Tools skipped: {', '.join(result.tools_skipped)}")
        lines.append("")
        if result.issues:
            lines.append("Issues to fix:")
            for issue in result.issues[:40]:
                lines.append(f"  - {issue.format()}")
            if len(result.issues) > 40:
                lines.append(f"  ... and {len(result.issues) - 40} more issues")
        return "\n".join(lines)

    @staticmethod
    def _guess_keep_symbols(code_sketch: str) -> List[str]:
        names = re.findall(r"\b(?:def|function|fn)\s+([A-Za-z_]\w*)", code_sketch)
        names += re.findall(r"\bclass\s+([A-Za-z_]\w*)", code_sketch)
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    @staticmethod
    def _extract_mentioned_symbols(text: str) -> List[str]:
        """Pull symbol-like identifiers out of free-text instructions.

        Catches patterns like::
            "为 main 函数上方添加注释"
            "为main函数上方添加注释"          ← no space — Chinese-ASCII boundary
            "rename foo() to bar()"
            "modify the `process_edit` method"
            "fix MyClass.handle_request"

        IMPORTANT: we use ASCII-only lookarounds instead of ``\\b`` because
        Python's ``\\b`` operates on Unicode word boundaries, so in
        ``为main函数`` the 'm' has no boundary on either side (CJK chars are
        word chars too) and ``\\bmain\\b`` matches NOTHING.  The lookaround
        form ``(?<![A-Za-z0-9_])...(?![A-Za-z0-9_])`` only treats ASCII
        identifier chars as boundaries, so CJK / punctuation / whitespace
        all correctly delimit identifiers.

        Returns up to 8 candidate names in first-seen order.
        """
        if not text:
            return []
        candidates: List[str] = []
        seen: set = set()

        # Boundary regex fragments — see docstring for why we don't use \b.
        BL = r"(?<![A-Za-z0-9_])"     # left boundary: not an ASCII ident char
        BR = r"(?![A-Za-z0-9_])"      # right boundary: not an ASCII ident char
        IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

        def _push(name: str) -> None:
            if not name:
                return
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                return
            if name.lower() in {
                "the", "a", "an", "this", "that", "function", "class", "method",
                "to", "from", "in", "on", "for", "and", "or", "if", "else",
                "is", "are", "was", "were", "be", "been", "being",
                "add", "remove", "delete", "change", "fix", "update", "modify",
                "make", "do", "ensure", "please", "should",
            }:
                return
            if name in seen:
                return
            seen.add(name)
            candidates.append(name)

        # Highest signal: backtick-quoted identifiers
        for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_\.]*)`", text):
            for part in m.group(1).split("."):
                _push(part)
        # Identifier followed by () — clearly a function call mention
        for m in re.finditer(rf"{BL}({IDENT})\s*\(", text):
            _push(m.group(1))
        # Class.method dotted refs
        for m in re.finditer(rf"{BL}({IDENT})\.({IDENT}){BR}", text):
            _push(m.group(1))
            _push(m.group(2))
        # Bare CamelCase / snake_case words (lowest priority).  Three-char
        # minimum filters out 'a', 'is', etc. that slipped past the stop list.
        for m in re.finditer(rf"{BL}({IDENT[:-1]}{{2,}}){BR}", text):
            _push(m.group(1))
        return candidates[:8]

    def _collect_symbol_anchors(
        self,
        *,
        request: "EditRequest",
        original_content: str,
        language: str,
        keep_symbols: List[str],
    ) -> List[Dict[str, Any]]:
        """For every symbol mentioned by the user, extract its verbatim source.

        Returns a list of ``{name, content, line_start, line_end}`` dicts
        ready to be wrapped in ContextItem.  Empty list when AST parsing
        fails or no symbols match — the rest of the pipeline gracefully
        degrades to whole-file mode in that case.
        """
        # Pool of candidate names: explicit ``def foo`` markers from the
        # sketch, plus every identifier-shaped word in the instructions.
        candidates: List[str] = list(keep_symbols or [])
        for n in self._extract_mentioned_symbols(request.instructions or ""):
            if n not in candidates:
                candidates.append(n)
        if not candidates:
            return []

        try:
            from omnicode.ast_engine.parser import UnifiedASTParser  # noqa: PLC0415
            parser = UnifiedASTParser()
            symbols = parser.extract_symbols(original_content, language) or []
        except Exception as exc:
            logger.debug("Symbol-anchor extraction skipped: %s", exc)
            return []

        if not symbols:
            return []

        # Build a name -> [symbol] index.  Symbols are dicts as documented in
        # parser._generic_extract_symbols.
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for sym in symbols:
            if not isinstance(sym, dict):
                continue
            n = sym.get("name")
            if n:
                by_name.setdefault(n, []).append(sym)

        lines = original_content.splitlines()
        anchors: List[Dict[str, Any]] = []
        seen_keys: set = set()
        # Cap how many anchors we emit so a verbose instruction doesn't blow
        # the budget.
        MAX_ANCHORS = 5
        MAX_LINES_PER_ANCHOR = 80

        for cand in candidates:
            if cand not in by_name:
                continue
            for sym in by_name[cand]:
                s_line = sym.get("line_start") or sym.get("start_line")
                e_line = sym.get("line_end") or sym.get("end_line")
                if not s_line or not e_line:
                    continue
                key = (cand, s_line, e_line)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                # Clip very long symbols so a 500-line class doesn't dominate
                # the prompt — the LLM only needs enough lines to anchor the
                # SEARCH block.
                slice_end = min(e_line, s_line + MAX_LINES_PER_ANCHOR - 1)
                snippet = "\n".join(lines[s_line - 1 : slice_end])
                content = (
                    f"# Authoritative anchor for `{cand}` "
                    f"(lines {s_line}-{slice_end}{' truncated' if slice_end < e_line else ''}). "
                    f"Use these EXACT lines as the SEARCH section if you patch this symbol.\n"
                    f"{snippet}"
                )
                anchors.append({
                    "name": cand,
                    "content": content,
                    "line_start": s_line,
                    "line_end": slice_end,
                })
                if len(anchors) >= MAX_ANCHORS:
                    break
            if len(anchors) >= MAX_ANCHORS:
                break

        if anchors:
            logger.info(
                "Injected %d symbol anchor(s): %s",
                len(anchors),
                ", ".join(a["name"] for a in anchors),
            )
        return anchors

    @staticmethod
    def _collect_git_blame(file_path: str) -> str:
        try:
            cwd = os.path.dirname(os.path.abspath(file_path))
            cmd = ["git", "blame", os.path.basename(file_path)]
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return "\n".join(result.stdout.splitlines()[:50])
        except Exception as exc:
            logger.debug("git blame skipped: %s", exc)
        return ""

    @staticmethod
    def _collect_history_advisory(file_path: str) -> str:
        try:
            from omnicode.git_context.history import GitHistoryAnalyzer

            cwd = os.path.dirname(os.path.abspath(file_path)) or "."
            analyzer = GitHistoryAnalyzer(cwd, max_commits_scanned=80)
            report = analyzer.analyze_file(os.path.basename(file_path))
            if report.total_commits == 0:
                return ""
            lines = [
                f"Risk score: {report.risk_score:.2f} ({report.risk_level}).",
                f"Total commits scanned: {report.total_commits}; defensive: {report.defensive_commit_count}; hardening: {report.hardening_commit_count}.",
                f"Advisory: {report.advisory}",
            ]
            if report.defensive_patches:
                lines.append("Recent defensive commits to NOT revert:")
                for c in report.defensive_patches[:3]:
                    lines.append(f"  - {c.short_hash} ({c.author}): {c.message[:120]}")
            if report.related_issues:
                lines.append(
                    f"Related issues: {', '.join(report.related_issues[:6])}"
                )
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("history advisory skipped: %s", exc)
            return ""

    @staticmethod
    async def _collect_memory_advisory(query: str, file_path: str) -> str:
        """Pull the top-3 most relevant historical memories for this edit (STAGE 7.5)."""
        try:
            from core import get_memory_manager
            from memory_system.models import MemorySearchRequest

            mgr = get_memory_manager()
            if mgr is None:
                return ""
            results = await mgr.search_memories(
                MemorySearchRequest(query=query, max_results=3)
            )
            if not results:
                return ""
            out = ["Relevant past learnings (use these to avoid repeating mistakes):"]
            for r in results:
                m = r.memory
                cat = getattr(m.category, "value", str(m.category))
                content = (m.content or "").strip()
                if len(content) > 240:
                    content = content[:240] + "..."
                out.append(f"  - [{cat}] {content}")
            return "\n".join(out)
        except Exception as exc:
            logger.debug("memory advisory skipped: %s", exc)
            return ""

    @staticmethod
    async def _write_memory(*, instructions: str, file_path: str, summary: str) -> None:
        """Write a 'solution' memory after a successful edit (STAGE 7.5)."""
        try:
            from core import get_memory_manager
            from memory_system.models import (
                MemoryCategory,
                MemoryImportance,
                MemoryRequest,
            )

            mgr = get_memory_manager()
            if mgr is None:
                return
            content = (
                f"Edit succeeded for {os.path.basename(file_path)}. "
                f"Instructions: {instructions[:240]}. "
                f"Summary: {summary[:240]}."
            )
            await mgr.store_memory(
                MemoryRequest(
                    category=MemoryCategory.SOLUTION,
                    content=content,
                    importance=MemoryImportance.MEDIUM,
                    tags=["edit", "auto"],
                    related_files=[file_path],
                )
            )
        except Exception as exc:
            logger.debug("memory store skipped: %s", exc)

    def _try_symbol_surgical(
        self,
        *,
        request: "EditRequest",
        original_content: str,
        language: str,
    ) -> Optional[Dict[str, Any]]:
        """Decide whether we can do a symbol-surgical edit.

        Returns a dict with keys::

            target_symbol, snippet, snippet_start_line, snippet_end_line

        ...or ``None`` if the heuristic can't safely identify a single
        target symbol.

        Heuristic:
          1. Must successfully AST-parse the file.
          2. Pull candidate names from the user's instruction (and sketch).
          3. Each candidate must resolve to EXACTLY ONE symbol in the file
             (multiple matches -> ambiguous, fall back to whole-file/patch).
          4. Pick the FIRST such unique candidate.
          5. Snippet = symbol body + a small ``_SURGICAL_PADDING`` of
             surrounding lines so the LLM has context (imports, docstrings,
             above-comment ZONE for things like '在 main 上方添加注释').

        We INTENTIONALLY don't try to pick when there are multiple unique
        symbols — making a wrong choice silently could be worse than asking
        the user to be more specific.
        """
        if len(original_content.splitlines()) < self._SURGICAL_MIN_FILE_LINES:
            return None

        try:
            from omnicode.ast_engine.parser import UnifiedASTParser  # noqa: PLC0415
            parser = UnifiedASTParser()
            symbols = parser.extract_symbols(original_content, language) or []
        except Exception as exc:
            logger.debug("Surgical mode: AST parse failed (%s) — skipping", exc)
            return None

        if not symbols:
            return None

        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for sym in symbols:
            if not isinstance(sym, dict):
                continue
            n = sym.get("name")
            if n:
                by_name.setdefault(n, []).append(sym)

        candidates: List[str] = self._extract_mentioned_symbols(
            request.instructions or ""
        )
        for n in self._guess_keep_symbols(request.code_edit or ""):
            if n not in candidates:
                candidates.append(n)

        for cand in candidates:
            matches = by_name.get(cand, [])
            if len(matches) != 1:
                continue
            sym = matches[0]
            s_line = sym.get("line_start") or sym.get("start_line")
            e_line = sym.get("line_end") or sym.get("end_line")
            if not s_line or not e_line:
                continue
            if (e_line - s_line + 1) > self._SURGICAL_MAX_SYMBOL_LINES:
                logger.debug(
                    "Surgical mode: %s spans %d lines (> %d) — skipping",
                    cand, e_line - s_line + 1, self._SURGICAL_MAX_SYMBOL_LINES,
                )
                continue

            lines = original_content.splitlines()
            total = len(lines)
            pad = self._SURGICAL_PADDING
            s_idx = max(0, s_line - 1 - pad)
            e_idx = min(total, e_line + pad)
            snippet = "\n".join(lines[s_idx:e_idx])
            return {
                "target_symbol": cand,
                "snippet": snippet,
                "snippet_start_line": s_idx + 1,
                "snippet_end_line": e_idx,
                "symbol_line_start": s_line,
                "symbol_line_end": e_line,
            }
        return None

    @staticmethod
    def _splice_snippet(
        original: str,
        *,
        new_snippet: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """Replace lines ``start_line..end_line`` (1-based, inclusive) in
        ``original`` with ``new_snippet``.

        Preserves the original file's trailing newline behaviour.
        """
        lines = original.splitlines(keepends=True)
        s = max(0, start_line - 1)
        e = min(len(lines), end_line)
        snippet_lines = new_snippet.splitlines(keepends=True)
        if snippet_lines and not snippet_lines[-1].endswith("\n") and (
            e < len(lines) or (lines and lines[-1].endswith("\n"))
        ):
            snippet_lines[-1] = snippet_lines[-1] + "\n"
        return "".join(lines[:s] + snippet_lines + lines[e:])

    @classmethod
    def _apply_search_replace_patches(
        cls, original: str, blob: str
    ) -> tuple[str, int, List[str]]:
        """Apply Aider-style SEARCH/REPLACE blocks to ``original``.

        Returns a tuple ``(new_text, applied_count, errors)``.

        Format::

            <<<<<<< SEARCH
            <verbatim original lines>
            =======
            <replacement lines>
            >>>>>>> REPLACE

        Multiple blocks may appear back-to-back inside ``blob``.

        Matching strategy (in order):
          1. Exact substring match
          2. Whitespace-normalised match (handles trailing-space drift)
          3. Skip with an error if neither hits
        """
        errors: List[str] = []
        applied = 0
        text = original

        # Tolerate variants the LLM might emit
        BEGIN_RE = re.compile(r"^[<]{3,}\s*SEARCH\s*$", re.MULTILINE)
        END_RE = re.compile(r"^[>]{3,}\s*REPLACE\s*$", re.MULTILINE)
        DIVIDER_RE = re.compile(r"^={3,}\s*$", re.MULTILINE)

        # Walk the blob splitting at BEGIN markers.
        cursor = 0
        while True:
            m_begin = BEGIN_RE.search(blob, cursor)
            if not m_begin:
                break
            after_begin = blob[m_begin.end():]
            m_div = DIVIDER_RE.search(after_begin)
            if not m_div:
                errors.append("Malformed patch block: missing '=======' divider.")
                break
            search_text = after_begin[: m_div.start()]
            after_div = after_begin[m_div.end():]
            m_end = END_RE.search(after_div)
            if not m_end:
                errors.append("Malformed patch block: missing '>>>>>>> REPLACE' end marker.")
                break
            replace_text = after_div[: m_end.start()]
            cursor = m_begin.end() + m_div.end() + m_end.end()

            # Strip exactly one leading + trailing newline introduced by the
            # markdown formatting — keep the rest of the user's whitespace
            # intact.
            search_text = search_text.lstrip("\n").rstrip("\n")
            replace_text = replace_text.lstrip("\n").rstrip("\n")

            if not search_text:
                errors.append("Empty SEARCH section — refusing to apply.")
                continue

            # Try exact match first
            if search_text in text:
                text = text.replace(search_text, replace_text, 1)
                applied += 1
                continue

            # Try whitespace-normalised match
            patched = cls._fuzzy_apply(text, search_text, replace_text)
            if patched is not None:
                text = patched
                applied += 1
                continue

            preview = search_text.splitlines()[:3]
            errors.append(
                "SEARCH text not found in the original file. First lines: "
                + repr(" | ".join(preview))[:300]
            )

        return text, applied, errors

    @staticmethod
    def _fuzzy_apply(haystack: str, search: str, replace: str) -> Optional[str]:
        """Try to apply a SEARCH/REPLACE when whitespace differs slightly.

        Strategy: split both haystack and search into lines, compare with
        leading/trailing whitespace stripped per line.  When all search
        lines are found contiguously, splice in the replacement.
        """
        s_lines = search.splitlines()
        if not s_lines:
            return None
        h_lines = haystack.splitlines(keepends=True)
        s_norm = [ln.strip() for ln in s_lines]

        for i in range(len(h_lines) - len(s_lines) + 1):
            window = [ln.strip() for ln in h_lines[i : i + len(s_lines)]]
            if window == s_norm:
                # Splice while preserving the haystack's original line endings
                # outside the matched region.
                replacement = replace
                if not replacement.endswith("\n") and i + len(s_lines) < len(h_lines):
                    replacement += "\n"
                head = "".join(h_lines[:i])
                tail = "".join(h_lines[i + len(s_lines):])
                return head + replacement + tail
        return None

    @staticmethod
    def _extract_code_block(text: str, language: str) -> Optional[str]:
        marker = f"```{language}"
        if marker in text:
            tail = text.split(marker, 1)[1]
            return tail.split("```", 1)[0].strip() if "```" in tail else tail.strip()
        if "```" in text:
            parts = text.split("```", 2)
            if len(parts) >= 2:
                lines = parts[1].splitlines()
                if lines and len(lines[0].strip()) < 15 and not any(
                    c in lines[0] for c in " =:()[]{}"
                ):
                    return "\n".join(lines[1:]).strip()
                return parts[1].strip()
        return None

    @staticmethod
    def _looks_like_prose(text: str, language: str) -> bool:
        """Heuristic to catch LLM responses that put English/Chinese narration
        inside a code fence instead of actual code.

        Returns True only when *no* line resembles source code. Mixed
        comments + code is fine — we only reject pure prose so that the file
        never gets overwritten by a "thinking" dump.
        """
        if not text or not text.strip():
            return True

        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return True

        # Per-language quick wins: any line that *starts* with one of these
        # is almost certainly real source code.
        code_prefixes_by_lang = {
            "python":     ("import ", "from ", "def ", "class ", "async ", "@", "if __name__"),
            "javascript": ("import ", "export ", "const ", "let ", "var ", "function ",
                           "class ", "async ", "//", "/*"),
            "typescript": ("import ", "export ", "const ", "let ", "var ", "function ",
                           "class ", "async ", "interface ", "type ", "//", "/*"),
            "cpp":        ("#include", "#define", "#pragma", "namespace ", "class ",
                           "struct ", "template", "//", "/*"),
            "c":          ("#include", "#define", "#pragma", "//", "/*"),
            "java":       ("package ", "import ", "public ", "private ", "protected ",
                           "class ", "interface ", "//", "/*"),
            "go":         ("package ", "import ", "func ", "type ", "var ", "const ",
                           "//", "/*"),
            "rust":       ("use ", "pub ", "fn ", "struct ", "enum ", "impl ",
                           "trait ", "mod ", "//"),
        }
        prefixes = code_prefixes_by_lang.get(
            (language or "").lower(),
            ("import ", "from ", "def ", "class ", "function ", "//", "#include"),
        )
        for ln in lines:
            stripped = ln.strip()
            if stripped.startswith(prefixes):
                return False

        # Generic code-shape signals across all languages.
        code_signal_chars = ("{", "}", ";", "==", "!=", "->", "=>", "::")
        signal_lines = sum(
            1 for ln in lines
            if any(sig in ln for sig in code_signal_chars)
            or "    " in ln  # 4-space indent strongly suggests code
            or "\t" in ln
        )
        # If 30%+ of lines look code-shaped, accept it.
        if signal_lines / len(lines) >= 0.3:
            return False

        # Last sanity check: if the text is *very* short (a single # comment
        # or one-line edit), accept it.  Pure-prose multi-line blocks are
        # the dangerous case.
        return not (len(lines) <= 2 and len(text) <= 120)
