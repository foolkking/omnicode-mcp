"""
Guard endpoints (STAGE 6)
=========================
Run proactive static analysis (mypy + ruff + bandit for Python) against any
working-directory file and return structured findings.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from core.config import get_settings
from omnicode.guard.analyzer import ProactiveGuard
from utils import create_error_response, create_success_response, validate_file_path

router = APIRouter(prefix="/guard", tags=["guard"])
guard = ProactiveGuard()


@router.post("/check")
async def check_file(
    file_path: str = Query(..., description="Path to file to analyze"),
):
    """Run proactive guard checks on a specific file."""
    try:
        settings = get_settings()
        await validate_file_path(file_path, settings.WORKING_DIR)

        result = await guard.check(file_path)

        return create_success_response(
            {
                "file_path": file_path,
                "is_clean": result.is_clean,
                "summary": result.summary(),
                "errors": result.errors,
                "warnings": result.warnings,
                "error_count": result.error_count,
                "warning_count": result.warning_count,
                "tools_run": result.tools_run,
                "tools_skipped": result.tools_skipped,
                "issues": [i.dict() for i in result.issues],
            }
        )
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover - defensive
        return create_error_response(f"Guard check failed: {str(e)}", 500)
