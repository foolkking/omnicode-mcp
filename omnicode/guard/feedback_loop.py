import logging
from typing import Optional

from omnicode.llm.base import LLMMessage, Role
from omnicode.llm.router import LLMRouter, RoutingStrategy

logger = logging.getLogger(__name__)

class GuardFeedbackLoop:
    """
    Handles auto-correction of code based on static analysis guard errors.
    """
    def __init__(self, router: LLMRouter):
        self.router = router

    async def auto_fix(self, file_path: str, code: str, errors: str, language: str) -> Optional[str]:
        """
        Attempt to automatically fix guard errors by querying the LLM.
        Returns the fixed code if successful, otherwise None.
        """
        logger.info(f"Attempting auto-fix for {file_path}")

        system_prompt = (
            "You are an expert developer and bug fixer.\n"
            f"The following {language} code failed static analysis.\n"
            "Your task is to fix the errors and return ONLY the corrected code.\n"
            "Do not include any explanations, markdown wrapping, or anything else."
        )

        user_prompt = (
            f"Original Code:\n{code}\n\n"
            f"Static Analysis Errors:\n{errors}\n\n"
            "Fix the code and output the full corrected code."
        )

        messages = [
            LLMMessage(role=Role.SYSTEM, content=system_prompt),
            LLMMessage(role=Role.USER, content=user_prompt)
        ]

        try:
            response = await self.router.complete(messages=messages, strategy=RoutingStrategy.QUALITY_FIRST)
            fixed_code = response.content.strip()

            # Simple cleanup of markdown if LLM disobeyed
            if fixed_code.startswith(f"```{language}"):
                fixed_code = fixed_code[len(f"```{language}"):]
            if fixed_code.startswith("```"):
                fixed_code = fixed_code[3:]
            if fixed_code.endswith("```"):
                fixed_code = fixed_code[:-3]

            return fixed_code.strip()
        except Exception as e:
            logger.error(f"Auto-fix attempt failed: {e}")
            return None
