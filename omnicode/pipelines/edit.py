import time
import os
import logging
from typing import Optional
from omnicode.llm.router import LLMRouter, RoutingStrategy
from omnicode.llm.base import LLMMessage, Role
from omnicode.guard.analyzer import ProactiveGuard

logger = logging.getLogger(__name__)

class EditRequest:
    """
    Request payload for file edit operation.
    """
    def __init__(self, target_file: str, instructions: str, code_edit: str, language: Optional[str] = None):
        self.target_file = target_file
        self.instructions = instructions
        self.code_edit = code_edit
        self.language = language

class EditPipeline:
    """
    Pipeline for assisting with AI-assisted file editing, 
    running guards, and managing fallbacks.
    """
    def __init__(self, write_pipeline=None):
        self.write_pipeline = write_pipeline
        self.router = LLMRouter()
        self.guard = ProactiveGuard()

    def get_stats(self) -> dict:
        return {
            "status": "active"
        }


    async def process_edit(self, request: EditRequest, save_to_file: bool = True):
        start_time = time.time()
        file_path = request.target_file
        logger.info(f"Processing edit for {file_path}")

        # Read original content
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            original_content = f.read()

        language = request.language or os.path.splitext(file_path)[1].lstrip(".") or "python"

        # Build messages for LLM
        system_prompt = (
            "You are an expert software engineer specializing in refactoring and precise editing.\n"
            "You will be provided with a file's original content and instructions to edit it.\n"
            "You must apply the changes and output the COMPLETE updated file content.\n"
            "Do NOT output explanations or notes, ONLY output the complete updated file content.\n"
            "Wrap your output in a markdown code block starting with ```" + language + "\n"
        )

        user_prompt = (
            f"Target File: {file_path}\n"
            f"Language: {language}\n\n"
            f"Instructions:\n{request.instructions}\n\n"
            f"Code Edit Context:\n{request.code_edit}\n\n"
            f"Original File Content:\n"
            f"```{language}\n"
            f"{original_content}\n"
            f"```\n\n"
            f"Please edit this file and return the COMPLETE updated file content inside a markdown code block."
        )

        messages = [
            LLMMessage(role=Role.SYSTEM, content=system_prompt),
            LLMMessage(role=Role.USER, content=user_prompt)
        ]

        logger.info("Sending edit request to LLM router...")
        total_calls = 1
        attempts = 0
        gemini_edit_success = False
        gemini_errors = []
        final_content = original_content

        try:
            # Complete request using LLM router (default quality_first strategy)
            response = await self.router.complete(messages=messages, strategy=RoutingStrategy.QUALITY_FIRST)
            llm_text = response.content
            
            # Extract code block content
            code_block_marker = f"```{language}"
            if code_block_marker in llm_text:
                parts = llm_text.split(code_block_marker, 1)
                content_part = parts[1]
                if "```" in content_part:
                    final_content = content_part.split("```", 1)[0]
                else:
                    final_content = content_part
            elif "```" in llm_text:
                parts = llm_text.split("```", 2)
                if len(parts) >= 2:
                    # Strip first line if it looks like a language name
                    lines = parts[1].splitlines()
                    if lines and len(lines[0].strip()) < 15 and not any(c in lines[0] for c in " =:()[]{}"):
                        final_content = "\n".join(lines[1:])
                    else:
                        final_content = parts[1]
                else:
                    final_content = llm_text
            else:
                final_content = llm_text

            final_content = final_content.strip()
            gemini_edit_success = True
        except Exception as e:
            logger.error(f"LLM editing failed: {e}")
            gemini_errors.append(str(e))

        # Save to file if edit succeeded and save requested
        if gemini_edit_success and save_to_file:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(final_content)
            logger.info(f"Saved edited file to {file_path}")

        # Run proactive guard static analysis checks
        format_success = True
        format_errors = []
        warnings = []
        
        if gemini_edit_success:
            try:
                guard_result = await self.guard.check(file_path)
                if not guard_result.is_clean:
                    format_success = False
                    if guard_result.errors:
                        format_errors.append(guard_result.errors)
                    if guard_result.warnings:
                        warnings.append(guard_result.warnings)
            except Exception as e:
                logger.warning(f"Proactive guard failed to run: {e}")
                warnings.append(f"Guard check failed: {e}")

        processing_time = time.time() - start_time

        # Create structured EditResult compatible with legacy router expects
        class EditResult:
            def __init__(self):
                self.file_path = file_path
                self.success = gemini_edit_success and format_success
                self.instructions = request.instructions
                self.summary = f"Edited successfully using {language} adapter."
                self.quality_score = 0.9 if format_success else 0.5
                self.gemini_edit_success = gemini_edit_success
                self.format_success = format_success
                self.error_correction_attempts = attempts
                self.total_gemini_calls = total_calls
                self.processing_time_seconds = processing_time
                self.original_content = original_content
                self.final_content = final_content
                self.gemini_errors = gemini_errors
                self.format_errors = format_errors
                self.warnings = warnings

        return EditResult()
