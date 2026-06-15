"""Language capability matrix for MCP tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CapabilityState = Literal["ready", "partial", "optional", "unsupported", "not_performed"]


@dataclass(frozen=True)
class LanguageCapabilities:
    language: str
    read: CapabilityState
    symbol: CapabilityState
    text_search: CapabilityState
    diagnostics: CapabilityState
    validate: CapabilityState
    graph: CapabilityState

    def to_dict(self) -> dict[str, str]:
        return {
            "language": self.language,
            "read": self.read,
            "symbol": self.symbol,
            "text_search": self.text_search,
            "diagnostics": self.diagnostics,
            "validate": self.validate,
            "graph": self.graph,
        }


LANGUAGE_CAPABILITY_MATRIX: dict[str, LanguageCapabilities] = {
    "python": LanguageCapabilities(
        language="python",
        read="ready",
        symbol="ready",
        text_search="ready",
        diagnostics="partial",
        validate="ready",
        graph="partial",
    ),
    "java": LanguageCapabilities(
        language="java",
        read="ready",
        symbol="partial",
        text_search="ready",
        diagnostics="optional",
        validate="optional",
        graph="partial",
    ),
    "scala": LanguageCapabilities(
        language="scala",
        read="ready",
        symbol="partial",
        text_search="ready",
        diagnostics="unsupported",
        validate="unsupported",
        graph="unsupported",
    ),
    "unknown": LanguageCapabilities(
        language="unknown",
        read="ready",
        symbol="partial",
        text_search="ready",
        diagnostics="unsupported",
        validate="not_performed",
        graph="unsupported",
    ),
}


_EXTENSION_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".scala": "scala",
    ".sc": "scala",
}


def language_for_path(path: str) -> str:
    return _EXTENSION_LANGUAGE.get(Path(path).suffix.lower(), "unknown")


def capabilities_for_path(path: str) -> LanguageCapabilities:
    return LANGUAGE_CAPABILITY_MATRIX.get(
        language_for_path(path),
        LANGUAGE_CAPABILITY_MATRIX["unknown"],
    )


def capability_matrix_payload() -> dict[str, dict[str, str]]:
    return {
        name: caps.to_dict()
        for name, caps in sorted(LANGUAGE_CAPABILITY_MATRIX.items())
    }


__all__ = [
    "LANGUAGE_CAPABILITY_MATRIX",
    "LanguageCapabilities",
    "capabilities_for_path",
    "capability_matrix_payload",
    "language_for_path",
]
