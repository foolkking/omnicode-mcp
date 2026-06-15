"""Shared query planner for deterministic MCP/HTTP search contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

SearchIntent = Literal[
    "exact_symbol",
    "exact_text",
    "regex_text",
    "file_path",
    "semantic",
    "references",
    "hybrid",
]

SearchEmptyReason = Literal[
    "true_empty",
    "index_not_ready",
    "provider_unavailable",
    "filtered_out",
]

_VALID_MODES = ("auto", "semantic", "symbol", "text", "hybrid", "references")
_IDENT_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_QUOTED_RE = re.compile(r'^"[^"]+"$|^\'[^\']+\'$')
_PATH_RE = re.compile(r"^[\w./\\-]+\.[A-Za-z0-9]{1,8}$")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "be",
        "if",
        "do",
        "we",
        "it",
        "as",
        "by",
        "at",
        "no",
        "def",
        "class",
        "let",
        "var",
        "fn",
        "func",
        "return",
        "from",
        "import",
        "use",
        "pub",
    }
)
_SINGLE_TOKEN_TEXT_LITERALS = frozenset(
    {
        "before",
        "after",
        "true",
        "false",
        "none",
        "null",
        "yes",
        "no",
        "on",
        "off",
        "enabled",
        "disabled",
        "pass",
        "fail",
        "passed",
        "failed",
        "todo",
        "done",
    }
)


@dataclass(frozen=True)
class SearchPlan:
    intent: SearchIntent
    requested_mode: str
    resolved_mode: str
    providers: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    fallback_capabilities: list[str] = field(default_factory=list)
    freshness_required: bool = False

    def to_dict(self, *, providers: Sequence[str] | None = None) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "requested_mode": self.requested_mode,
            "resolved_mode": self.resolved_mode,
            "providers": list(providers) if providers is not None else list(self.providers),
            "required_capabilities": list(self.required_capabilities),
            "fallback_capabilities": list(self.fallback_capabilities),
            "freshness_required": bool(self.freshness_required),
        }


def strip_outer_quotes(query: str) -> str:
    q = str(query or "")
    if len(q) >= 2 and q[0] == q[-1] and q[0] in ("\"", "'"):
        return q[1:-1]
    return q


def detect_search_mode(query: str) -> str:
    """Resolve ``auto`` search mode from query shape.

    Keep this deterministic and conservative: exact code-looking strings should
    go to text, identifiers to symbol, and long natural language to semantic.
    """
    q = str(query or "").strip()
    if not q:
        return "semantic"
    if _QUOTED_RE.fullmatch(q):
        return "text"
    q = strip_outer_quotes(q).strip()
    if not q:
        return "semantic"
    if re.match(
        r"^\s*(async\s+def|def|class|interface|enum|trait|object|case\s+class|"
        r"import|from|package)\b",
        q,
    ):
        return "text"
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
    if _PATH_RE.fullmatch(q) and ("/" in q or "\\" in q):
        return "text"
    if _IDENT_RE.fullmatch(q) and len(q) <= 60:
        return "symbol"
    word_count = len(q.split())
    if word_count <= 3 and len(q) <= 40:
        return "hybrid"
    return "semantic"


def _intent_for_mode(
    *,
    query: str,
    resolved_mode: str,
    use_regex: bool = False,
) -> SearchIntent:
    if resolved_mode == "text":
        return "regex_text" if use_regex else "exact_text"
    if resolved_mode in {"symbol", "symbol_exact", "fuzzy_symbol"}:
        return "exact_symbol"
    if resolved_mode == "references":
        return "references"
    if resolved_mode == "hybrid":
        return "hybrid"
    if resolved_mode == "semantic":
        q = strip_outer_quotes(str(query or "").strip())
        if _PATH_RE.fullmatch(q) and ("/" in q or "\\" in q):
            return "file_path"
        return "semantic"
    return "semantic"


def build_search_plan(
    *,
    query: str,
    requested_mode: str = "auto",
    resolved_mode: str | None = None,
    use_regex: bool = False,
    freshness_required: bool = False,
) -> SearchPlan:
    requested = (requested_mode or "auto").strip().lower()
    resolved = resolved_mode or (detect_search_mode(query) if requested == "auto" else requested)
    resolved = resolved.strip().lower()
    intent = _intent_for_mode(query=query, resolved_mode=resolved, use_regex=use_regex)

    if intent == "exact_symbol":
        providers = ["local_exact_index", "cloud_exact_symbols", "parser_scan"]
        required = ["search.symbol_exact"]
        fallbacks = ["read.symbol", "search.text_exact", "search.semantic"]
    elif intent in {"exact_text", "regex_text", "file_path"}:
        providers = [
            "exact_line_fts",
            "ripgrep_fallback",
            "python_grep_fallback",
            "cloud_snapshot_grep",
        ]
        required = ["search.text_exact"]
        fallbacks = ["search.regex"]
    elif intent == "references":
        providers = ["lsp", "indexed_refs", "text_refs"]
        required = ["search.references"]
        fallbacks = ["search.text_exact", "search.symbol_exact"]
    elif intent == "hybrid":
        providers = [
            "local_exact_index",
            "exact_line_fts",
            "cloud_exact_symbols",
            "semantic_vector",
        ]
        required = ["search.symbol_exact", "search.text_exact"]
        fallbacks = ["search.semantic"]
    else:
        providers = ["semantic_vector"]
        required = ["search.semantic"]
        fallbacks = ["search.symbol_exact", "search.text_exact"]

    return SearchPlan(
        intent=intent,
        requested_mode=requested,
        resolved_mode=resolved,
        providers=providers,
        required_capabilities=required,
        fallback_capabilities=fallbacks,
        freshness_required=freshness_required,
    )


def empty_reason_for_unavailable(
    *,
    index_ready: bool = True,
    provider_available: bool = True,
    filtered: bool = False,
) -> SearchEmptyReason:
    if not index_ready:
        return "index_not_ready"
    if not provider_available:
        return "provider_unavailable"
    if filtered:
        return "filtered_out"
    return "true_empty"


__all__ = [
    "SearchEmptyReason",
    "SearchIntent",
    "SearchPlan",
    "build_search_plan",
    "detect_search_mode",
    "empty_reason_for_unavailable",
    "strip_outer_quotes",
]
