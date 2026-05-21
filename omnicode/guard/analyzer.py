import asyncio
import logging
from typing import List, Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class GuardResult(BaseModel):
    is_clean: bool
    errors: str
    warnings: str

class ProactiveGuard:
    """
    Runs static analysis on code to provide a safety net.
    """
    def __init__(self):
        pass

    async def _run_subprocess(self, cmd: List[str], cwd: str = None) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(), stderr.decode()

    async def check_python(self, file_path: str) -> GuardResult:
        """Run mypy and ruff on a Python file"""
        # Run Ruff (Linting)
        ruff_code, ruff_out, ruff_err = await self._run_subprocess(["ruff", "check", file_path])
        
        # Run Mypy (Type checking) - assuming mypy is installed
        try:
            mypy_code, mypy_out, mypy_err = await self._run_subprocess(["mypy", file_path])
        except FileNotFoundError:
            logger.warning("mypy not found, skipping type check")
            mypy_code, mypy_out, mypy_err = 0, "", ""

        is_clean = (ruff_code == 0) and (mypy_code == 0)
        
        errors = ""
        warnings = ""
        
        if ruff_code != 0:
            errors += f"Ruff Errors:\n{ruff_out}\n"
        if mypy_code != 0:
            errors += f"Mypy Errors:\n{mypy_out}\n"
            
        return GuardResult(
            is_clean=is_clean,
            errors=errors,
            warnings=warnings
        )

    async def check(self, file_path: str) -> GuardResult:
        """Run appropriate checks based on file extension"""
        if file_path.endswith(".py"):
            return await self.check_python(file_path)
        elif file_path.endswith((".js", ".ts", ".jsx", ".tsx")):
            # Placeholder for JS/TS checks (eslint, tsc)
            return GuardResult(is_clean=True, errors="", warnings="JS/TS checking not yet implemented")
        else:
            return GuardResult(is_clean=True, errors="", warnings=f"No checks available for {file_path}")

class FeedbackLoop:
    """
    Implements the auto-correction loop using the LLM and the Guard.
    """
    def __init__(self, llm_provider, guard: ProactiveGuard):
        self.llm = llm_provider
        self.guard = guard
        self.max_retries = 3

    async def generate_and_fix(self, instructions: str, file_path: str, context: str = "") -> str:
        """
        Generate code, check it, and ask LLM to fix if there are errors.
        (Simplified MVP implementation)
        """
        # In a full implementation, this would save to a temp file, check it, and loop
        pass
