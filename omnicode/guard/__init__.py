from .analyzer import ProactiveGuard
from .models import GuardIssue, GuardResult, IssueSeverity
from .tools.python_guard import PythonGuard


def __getattr__(name: str):
    if name == "GuardFeedbackLoop":
        from .feedback_loop import GuardFeedbackLoop

        return GuardFeedbackLoop
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ProactiveGuard",
    "GuardResult",
    "GuardIssue",
    "IssueSeverity",
    "GuardFeedbackLoop",
    "PythonGuard",
]
