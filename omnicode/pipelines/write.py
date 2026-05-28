"""
Write Pipeline (STAGE 4.6 — Token-aware)
========================================
Persists files to disk, indexes them in the search engine and tracks token
metrics so the Web UI can display compression statistics.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from omnicode.guard.analyzer import ProactiveGuard
from omnicode.llm.router import LLMRouter
from omnicode.llm.token_manager import (
    CommentStripper,
    FunctionFolder,
    TokenManager,
)

logger = logging.getLogger(__name__)


class WritePipeline:
    """Pipeline for handling file writes."""

    def __init__(
        self,
        search_engine: Any = None,
        patch_manager: Any = None,
    ) -> None:
        """Initialize the write pipeline.

        Parameters
        ----------
        search_engine:
            Used to incrementally re-index a file after writing.
        patch_manager:
            Optional :class:`omnicode_core.edit.patch.PatchManager`. When
            provided every write goes through ``apply_patch`` so the
            file gets a snapshot + EditSession + rollback hook. When
            ``None`` we fall back to a direct write (legacy behaviour;
            logs a warning).
        """
        self.search_engine = search_engine
        self.patch_manager = patch_manager
        self.router = LLMRouter()
        provider = self._pick_provider()
        self.token_manager = TokenManager(provider)
        self.guard = ProactiveGuard()

    # ------------------------------------------------------------------
    def _pick_provider(self):
        for pref in ("gemini", "claude", "openai", "deepseek", "default"):
            if pref in self.router.providers:
                return self.router.providers[pref]
        return next(iter(self.router.providers.values()))

    def get_stats(self) -> Dict[str, Any]:
        return {"status": "active"}

    def _safe_write(self, *, file_path: str, content: str) -> Optional[str]:
        """Write content via PatchManager when available; fallback to direct write.

        Returns the EditSession id on PatchManager success; ``None``
        when falling back. The caller can surface the id so the user
        can rollback this write.
        """
        if self.patch_manager is None:
            logger.warning(
                "WritePipeline has no PatchManager; writing %s without "
                "snapshot/rollback. Wire up PatchManager via lifespan.",
                file_path,
            )
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Saved file %s (no rollback available)", file_path)
            return None

        rel_path = self._workspace_relative(file_path)
        result = self.patch_manager.apply_patch(
            file_path=rel_path,
            new_content=content,
            source="write_pipeline",
        )
        if not result.success:
            logger.error(
                "WritePipeline.apply_patch failed for %s: %s",
                rel_path, result.message,
            )
            return None
        logger.info(
            "Saved %s via PatchManager (session=%s, +%d/-%d)",
            rel_path, result.session_id,
            result.lines_added, result.lines_removed,
        )
        return result.session_id

    def _workspace_relative(self, file_path: str) -> str:
        if not self.patch_manager:
            return file_path
        try:
            rel = os.path.relpath(file_path, self.patch_manager.working_dir)
            return rel.replace("\\", "/")
        except (ValueError, OSError):
            return file_path

    # ------------------------------------------------------------------
    async def process_write(
        self,
        content: str,
        file_path: str,
        purpose: Optional[str] = None,
        language: Optional[str] = None,
        save_to_file: bool = True,
    ):
        start_time = time.time()
        logger.info("Processing write for %s", file_path)

        lang = language or os.path.splitext(file_path)[1].lstrip(".") or "python"

        # ---- Token analytics ------------------------------------------------------
        original_tokens = self.token_manager.count_tokens(content) if content else 0

        # Optional compaction preview (does NOT modify the saved content)
        try:
            stripped_preview = CommentStripper.strip(content, lang) if content else ""
            stripped_tokens = self.token_manager.count_tokens(stripped_preview)
        except Exception:
            stripped_tokens = original_tokens
        try:
            folded_preview = FunctionFolder.fold(stripped_preview, lang) if content else ""
            folded_tokens = self.token_manager.count_tokens(folded_preview)
        except Exception:
            folded_tokens = stripped_tokens

        token_stats = {
            "original_tokens": original_tokens,
            "stripped_tokens": stripped_tokens,
            "folded_tokens": folded_tokens,
            "potential_savings_pct": round(
                (1 - folded_tokens / max(1, original_tokens)) * 100, 2
            ),
        }
        logger.info("Write content tokens: %s", token_stats)

        # ---- Save -----------------------------------------------------------------
        edit_session_id: Optional[str] = None
        if save_to_file:
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            edit_session_id = self._safe_write(file_path=file_path, content=content)

        # ---- Index ----------------------------------------------------------------
        if self.search_engine and hasattr(self.search_engine, "update_file"):
            try:
                await self.search_engine.update_file(file_path)
            except Exception as exc:
                logger.warning("Failed to update index for %s: %s", file_path, exc)

        # ---- Guard ----------------------------------------------------------------
        class FormatResult:
            success = True
            changes_made: list = []
            errors: list = []
            warnings: list = []

        try:
            guard_result = await self.guard.check(file_path)
            if not guard_result.is_clean:
                FormatResult.success = False
                if guard_result.errors:
                    FormatResult.errors.append(guard_result.errors)
                if guard_result.warnings:
                    FormatResult.warnings.append(guard_result.warnings)
        except Exception as exc:
            logger.warning("Guard check failed in write pipeline: %s", exc)
            FormatResult.warnings.append(f"Guard check failed: {exc}")

        class DependencyResult:
            success = True
            imports_found: list = []
            missing_dependencies: list = []
            resolved_symbols: list = []
            duplicate_definitions: list = []
            suggestions: list = []

        class WriteResult:
            def __init__(self, fp: str) -> None:
                self.file_path = fp
                self.success = FormatResult.success
                self.quality_score = 0.95 if FormatResult.success else 0.5
                self.summary = (
                    f"Written successfully: {purpose or ''}. "
                    f"Tokens: {token_stats['original_tokens']} "
                    f"(could compress to {token_stats['folded_tokens']} = "
                    f"{token_stats['potential_savings_pct']}% savings)"
                )
                self.format_result = FormatResult()
                self.dependency_result = DependencyResult()
                self.errors = FormatResult.errors
                self.warnings = FormatResult.warnings
                self.processing_time_seconds = time.time() - start_time
                self.token_stats = token_stats
                self.edit_session_id = edit_session_id

        return WriteResult(file_path)
