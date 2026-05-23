"""
Guard data models (STAGE 6)
============================
Common Pydantic models shared between language-specific guards.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class IssueSeverity(str, Enum):
    """Severity levels for guard findings."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


class GuardIssue(BaseModel):
    """A single static-analysis finding."""

    tool: str = Field(..., description="Source tool (mypy, ruff, bandit, ...)")
    code: Optional[str] = Field(None, description="Tool-specific rule code (e.g. 'E501')")
    severity: IssueSeverity = IssueSeverity.WARNING
    message: str
    line: Optional[int] = None
    column: Optional[int] = None
    file_path: Optional[str] = None

    def format(self) -> str:
        loc = ""
        if self.line is not None:
            loc = f":{self.line}"
            if self.column is not None:
                loc += f":{self.column}"
        code = f"[{self.code}] " if self.code else ""
        return f"{self.tool}{loc} {self.severity.value}: {code}{self.message}"


class GuardResult(BaseModel):
    """Aggregated guard outcome for a single file."""

    is_clean: bool = True
    errors: str = ""
    warnings: str = ""
    issues: List[GuardIssue] = Field(default_factory=list)
    tools_run: List[str] = Field(default_factory=list)
    tools_skipped: List[str] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.WARNING)

    def summary(self) -> str:
        return (
            f"{self.error_count} errors, {self.warning_count} warnings"
            f" across {len(self.tools_run)} tools"
        )
