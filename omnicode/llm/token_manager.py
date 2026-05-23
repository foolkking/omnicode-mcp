"""
Smart Token Compressor (STAGE 4)
================================
Implements four cooperating strategies that work together to maximise the
information density of every token sent to an LLM:

  1. CommentStripper   – strips decorative comments while preserving
                         semantic markers (TODO, FIXME, HACK, NOTE, XXX, BUG).
                         Supports Python (AST), C-family / JS-family /
                         Rust / Go / Java (regex), plus a generic fallback.

  2. FunctionFolder    – collapses unrelated function bodies into single-line
                         signatures (``def foo(...): ...``).

  3. ContextPruner     – performs priority-driven dynamic pruning so that
                         the most important context survives the budget.
                         Strategies are applied in escalating order:
                         keep-as-is ➜ strip ➜ fold ➜ truncate ➜ drop.

  4. CostGuard         – enforces a hard upper bound and offers chunked
                         dispatch for super-long requests.

A thin :class:`TokenManager` façade stitches the four pieces together so the
pipelines (Edit/Write) can simply call ``compress_for_llm(...)`` and forget
about the details.
"""

from __future__ import annotations

import ast
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import tiktoken

from .base import BaseLLMProvider, LLMMessage, Role

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Markers that should NEVER be stripped from comments
# ----------------------------------------------------------------------------
PRESERVED_MARKERS: Tuple[str, ...] = (
    "TODO",
    "FIXME",
    "HACK",
    "NOTE",
    "XXX",
    "BUG",
    "WARNING",
    "DEPRECATED",
    "SECURITY",
)

# ----------------------------------------------------------------------------
# Language family classification
# ----------------------------------------------------------------------------
_PYTHON_LANGS = {"python", "py"}
_C_FAMILY = {"c", "h", "cpp", "c++", "cc", "hpp", "hxx", "cxx", "java", "go", "rust", "rs", "kotlin", "kt", "swift", "scala", "cs"}
_JS_FAMILY = {"javascript", "js", "typescript", "ts", "jsx", "tsx"}
_HASH_FAMILY = {"ruby", "rb", "yaml", "yml", "toml", "shell", "bash", "sh", "perl", "pl", "r"}
_HTML_FAMILY = {"html", "xml", "vue", "svelte"}
_SQL_FAMILY = {"sql"}


def _normalize_language(language: Optional[str]) -> str:
    if not language:
        return "python"
    return language.strip().lower().lstrip(".")


# ============================================================================
# CommentStripper — multi-language comment removal with marker preservation
# ============================================================================
class CommentStripper:
    """Strips decorative comments while keeping TODO/FIXME/etc."""

    @staticmethod
    def _line_contains_marker(line: str) -> bool:
        upper = line.upper()
        return any(marker in upper for marker in PRESERVED_MARKERS)

    # --------------------------------------------------------- Python (AST)
    @staticmethod
    def _strip_python(code: str) -> str:
        """Strip docstrings (via AST line locations) and hash-comments (line-by-line),
        keeping comments that contain preserved markers like ``TODO``/``FIXME``."""
        # 1) Identify docstring lines we want to drop.
        drop_lines: set = set()
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module),
                ):
                    body = getattr(node, "body", None)
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        ds_node = body[0]
                        if not CommentStripper._line_contains_marker(ds_node.value.value):
                            start = (ds_node.lineno or 1) - 1
                            end = (getattr(ds_node, "end_lineno", ds_node.lineno) or ds_node.lineno) - 1
                            for i in range(start, end + 1):
                                drop_lines.add(i)
        except Exception:
            # If parsing fails just keep the source untouched
            pass

        # 2) Strip docstring lines + hash-comments that don't carry a marker.
        out_lines: List[str] = []
        for i, line in enumerate(code.splitlines()):
            if i in drop_lines:
                continue
            if "#" in line:
                idx = CommentStripper._find_comment_index(line, "#")
                if idx >= 0 and not CommentStripper._line_contains_marker(line[idx:]):
                    line = line[:idx].rstrip()
                    if not line:
                        continue
            out_lines.append(line)
        return "\n".join(out_lines)

    # ----------------------------------------------------- C-family / JS / Rust / Go / Java
    @staticmethod
    def _strip_c_like(code: str) -> str:
        # Block comments /* ... */
        def _block_repl(match: re.Match) -> str:
            content = match.group(0)
            return content if CommentStripper._line_contains_marker(content) else ""

        code = re.sub(r"/\*[\s\S]*?\*/", _block_repl, code)

        # Line comments //
        out_lines: List[str] = []
        for line in code.splitlines():
            idx = CommentStripper._find_comment_index(line, "//")
            if idx >= 0 and not CommentStripper._line_contains_marker(line[idx:]):
                line = line[:idx].rstrip()
                if not line:
                    continue
            out_lines.append(line)
        return "\n".join(out_lines)

    # ----------------------------------------------------- HTML / XML
    @staticmethod
    def _strip_html(code: str) -> str:
        def _repl(match: re.Match) -> str:
            content = match.group(0)
            return content if CommentStripper._line_contains_marker(content) else ""

        return re.sub(r"<!--[\s\S]*?-->", _repl, code)

    # ----------------------------------------------------- SQL
    @staticmethod
    def _strip_sql(code: str) -> str:
        out_lines: List[str] = []
        for line in code.splitlines():
            idx = CommentStripper._find_comment_index(line, "--")
            if idx >= 0 and not CommentStripper._line_contains_marker(line[idx:]):
                line = line[:idx].rstrip()
                if not line:
                    continue
            out_lines.append(line)
        return "\n".join(out_lines)

    # ----------------------------------------------------- Hash family (Ruby, YAML, shell)
    @staticmethod
    def _strip_hash_family(code: str) -> str:
        out_lines: List[str] = []
        for line in code.splitlines():
            idx = CommentStripper._find_comment_index(line, "#")
            if idx >= 0 and not CommentStripper._line_contains_marker(line[idx:]):
                line = line[:idx].rstrip()
                if not line:
                    continue
            out_lines.append(line)
        return "\n".join(out_lines)

    @staticmethod
    def _find_comment_index(line: str, marker: str) -> int:
        """Return the index of ``marker`` outside string literals, or -1."""
        in_single = False
        in_double = False
        escape = False
        i = 0
        m_len = len(marker)
        while i < len(line):
            ch = line[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double and line[i : i + m_len] == marker:
                return i
            i += 1
        return -1

    # ------------------------------------------------------------------ Public
    @classmethod
    def strip(cls, code: str, language: Optional[str] = None) -> str:
        """Strip non-essential comments from ``code`` for the given language."""
        if not code:
            return code
        lang = _normalize_language(language)
        try:
            if lang in _PYTHON_LANGS:
                return cls._strip_python(code)
            if lang in _C_FAMILY or lang in _JS_FAMILY:
                return cls._strip_c_like(code)
            if lang in _HTML_FAMILY:
                return cls._strip_html(code)
            if lang in _SQL_FAMILY:
                return cls._strip_sql(code)
            if lang in _HASH_FAMILY:
                return cls._strip_hash_family(code)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Comment stripping failed for %s: %s", lang, exc)
        # Generic fallback: try all common single-line markers
        return cls._strip_c_like(code)


# ============================================================================
# FunctionFolder — collapses function bodies to signatures
# ============================================================================
class FunctionFolder:
    """Folds function/method bodies into single-line signatures."""

    # Regex for C-like single-line function signatures + opening brace
    _C_LIKE_FN_RE = re.compile(
        r"""
        ^(?P<indent>[ \t]*)                     # leading indentation
        (?P<sig>                                 # signature (everything up to '{')
            (?:[A-Za-z_][\w<>:&*\s,\[\]]*\s+)?   # optional return type
            [A-Za-z_][\w]*                       # function name
            \s*\([^;{}]*\)                       # (...) param list
            (?:\s*(?:const|noexcept|throws[^{}]*))? # cv-qualifiers / throws
        )\s*\{                                   # opening brace
        """,
        re.VERBOSE | re.MULTILINE,
    )

    # ---------------------------------------------------- Python
    @staticmethod
    def _fold_python(
        code: str, keep: Optional[Iterable[str]] = None
    ) -> str:
        keep_set = set(keep or [])
        try:
            tree = ast.parse(code)
        except Exception:
            return code

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in keep_set:
                    continue
                # Replace body with a single ``pass`` while keeping the docstring
                first = node.body[0] if node.body else None
                if (
                    first is not None
                    and isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    node.body = [first, ast.Pass()]
                else:
                    node.body = [ast.Pass()]
        try:
            return ast.unparse(tree)
        except Exception:
            return code

    # ---------------------------------------------------- C-family / JS-like
    @classmethod
    def _fold_c_like(cls, code: str, keep: Optional[Iterable[str]] = None) -> str:
        keep_set = set(keep or [])
        result_chunks: List[str] = []
        idx = 0
        for match in cls._C_LIKE_FN_RE.finditer(code):
            sig = match.group("sig")
            indent = match.group("indent")
            # Should we keep this one?
            keep_this = any(name and name in sig for name in keep_set)
            # Locate matching closing brace
            brace_start = match.end() - 1  # index of '{'
            close = cls._find_matching_brace(code, brace_start)
            if close == -1:
                continue
            # Append text between previous index and match.start()
            result_chunks.append(code[idx:match.start()])
            if keep_this:
                # Keep as-is
                result_chunks.append(code[match.start() : close + 1])
            else:
                # Replace with signature; ...; placeholder
                result_chunks.append(f"{indent}{sig.strip()} {{ /* … */ }}")
            idx = close + 1
        result_chunks.append(code[idx:])
        return "".join(result_chunks)

    @staticmethod
    def _find_matching_brace(code: str, brace_start: int) -> int:
        depth = 0
        in_single = False
        in_double = False
        escape = False
        i = brace_start
        while i < len(code):
            ch = code[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif not in_single and not in_double:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return i
                elif ch == "'":
                    in_single = True
                elif ch == '"':
                    in_double = True
            else:
                if in_single and ch == "'":
                    in_single = False
                elif in_double and ch == '"':
                    in_double = False
            i += 1
        return -1

    # ------------------------------------------------------------------ Public
    @classmethod
    def fold(
        cls,
        code: str,
        language: Optional[str] = None,
        keep_symbols: Optional[Iterable[str]] = None,
    ) -> str:
        """Fold function bodies (signatures kept).

        :param keep_symbols: collection of symbol names that should NOT be folded
                             (e.g. the function the user is currently editing).
        """
        if not code:
            return code
        lang = _normalize_language(language)
        try:
            if lang in _PYTHON_LANGS:
                return cls._fold_python(code, keep_symbols)
            if lang in _C_FAMILY or lang in _JS_FAMILY:
                return cls._fold_c_like(code, keep_symbols)
        except Exception as exc:  # pragma: no cover
            logger.debug("Function folding failed for %s: %s", lang, exc)
        return code


# ============================================================================
# Priority-driven dynamic context pruner
# ============================================================================
@dataclass(order=False)
class ContextItem:
    """A single piece of context that may be pruned/compressed/dropped."""

    content: str
    priority: int = 0  # higher = more important
    role: str = "context"  # 'instruction' | 'target' | 'context' | 'comment'
    language: str = "python"
    label: Optional[str] = None  # e.g. file path
    keep_symbols: List[str] = field(default_factory=list)


class ContextPruner:
    """Applies escalating compression strategies to fit a token budget.

    Strategies (in escalating order):

    1.  Keep as-is (no transformation).
    2.  Strip non-essential comments.
    3.  Fold unrelated function bodies.
    4.  Truncate to leading + trailing slices around an ellipsis marker.
    5.  Drop the item entirely (lowest-priority items are dropped first).
    """

    def __init__(
        self,
        provider: BaseLLMProvider,
        usable_ratio: float = 0.85,
        min_window: int = 1024,
    ) -> None:
        self.provider = provider
        try:
            ctx = provider.get_context_window() or 8192
        except Exception:
            ctx = 8192
        self.max_window = max(min_window, int(ctx))
        self.usable_window = max(min_window, int(self.max_window * usable_ratio))

    # ------------------------------------------------------------- Token utility
    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        try:
            return self.provider.count_tokens(text)
        except Exception:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
                return len(enc.encode(text))
            except Exception:
                # Fallback: 4-chars-per-token
                return max(1, math.ceil(len(text) / 4))

    # ------------------------------------------------------------- Public API
    def prune(
        self,
        items: List[ContextItem],
        reserved_tokens: int = 0,
    ) -> Tuple[List[ContextItem], Dict[str, Any]]:
        """Return a pruned list of items plus diagnostic metadata."""
        budget = max(0, self.usable_window - max(0, reserved_tokens))
        # Sort by priority desc — high priority gets first crack at the budget
        sorted_items = sorted(items, key=lambda x: x.priority, reverse=True)

        kept: List[ContextItem] = []
        remaining = budget
        report: Dict[str, Any] = {
            "budget": budget,
            "usable_window": self.usable_window,
            "max_window": self.max_window,
            "actions": [],
        }

        for item in sorted_items:
            tok = self.count_tokens(item.content)
            action: Dict[str, Any] = {
                "label": item.label or item.role,
                "priority": item.priority,
                "tokens_in": tok,
            }
            # 1) keep as-is
            if tok <= remaining:
                kept.append(item)
                remaining -= tok
                action.update(strategy="keep", tokens_out=tok)
                report["actions"].append(action)
                continue
            # 2) strip comments
            stripped = CommentStripper.strip(item.content, item.language)
            stripped_tok = self.count_tokens(stripped)
            if stripped_tok <= remaining:
                item.content = stripped
                kept.append(item)
                remaining -= stripped_tok
                action.update(strategy="strip", tokens_out=stripped_tok)
                report["actions"].append(action)
                continue
            # 3) fold function bodies
            folded = FunctionFolder.fold(stripped, item.language, item.keep_symbols)
            folded_tok = self.count_tokens(folded)
            if folded_tok <= remaining:
                item.content = folded
                kept.append(item)
                remaining -= folded_tok
                action.update(strategy="fold", tokens_out=folded_tok)
                report["actions"].append(action)
                continue
            # 4) truncate (head + tail) if we still have budget
            if remaining >= 64:
                truncated = self._truncate(folded, remaining)
                trunc_tok = self.count_tokens(truncated)
                item.content = truncated
                kept.append(item)
                remaining -= trunc_tok
                action.update(strategy="truncate", tokens_out=trunc_tok)
                report["actions"].append(action)
                continue
            # 5) drop
            action.update(strategy="drop", tokens_out=0)
            report["actions"].append(action)

        report["tokens_remaining"] = remaining
        report["items_in"] = len(items)
        report["items_kept"] = len(kept)
        # Restore original order based on input list
        order = {id(it): idx for idx, it in enumerate(items)}
        kept.sort(key=lambda it: order.get(id(it), 0))
        return kept, report

    # ------------------------------------------------------------- Helpers
    def _truncate(self, text: str, target_tokens: int) -> str:
        """Keep head + tail joined by an ellipsis marker."""
        # Keep proportionally — half head, half tail
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return text[: target_tokens * 4]
        tokens = enc.encode(text)
        if len(tokens) <= target_tokens:
            return text
        head = tokens[: max(1, target_tokens // 2 - 4)]
        tail = tokens[-(max(1, target_tokens - len(head) - 4)) :]
        return enc.decode(head) + "\n# ... (truncated by context pruner) ...\n" + enc.decode(tail)


# ============================================================================
# CostGuard — hard upper bound + automatic chunked dispatch
# ============================================================================
class CostGuard:
    """Enforces a hard token cap and offers chunked dispatch helpers."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        hard_cap_tokens: Optional[int] = None,
        per_request_cost_cap_usd: float = 1.00,
        soft_warn_ratio: float = 0.9,
    ) -> None:
        self.provider = provider
        ctx = 0
        try:
            ctx = provider.get_context_window() or 0
        except Exception:
            ctx = 0
        self.hard_cap = hard_cap_tokens or max(8192, ctx)
        self.per_request_cost_cap_usd = per_request_cost_cap_usd
        self.soft_warn_ratio = soft_warn_ratio

    def check_messages(self, messages: List[LLMMessage]) -> Dict[str, Any]:
        total = 0
        for msg in messages:
            try:
                total += self.provider.count_tokens(msg.content)
            except Exception:
                total += max(1, len(msg.content) // 4)
        result = {"total_tokens": total, "hard_cap": self.hard_cap, "ok": True, "warning": None}
        if total > self.hard_cap:
            result["ok"] = False
            result["warning"] = (
                f"Request size {total} > hard cap {self.hard_cap}; chunked dispatch required."
            )
        elif total > int(self.hard_cap * self.soft_warn_ratio):
            result["warning"] = (
                f"Request size {total} approaching cap {self.hard_cap}; consider compressing."
            )
        return result

    def chunk_text(self, text: str, max_chunk_tokens: Optional[int] = None) -> List[str]:
        """Split very long text into chunks that each fit under the cap."""
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Char-based fallback
            cap = max_chunk_tokens or self.hard_cap // 2
            char_cap = cap * 4
            return [text[i : i + char_cap] for i in range(0, len(text), char_cap)]
        cap = max_chunk_tokens or max(512, self.hard_cap // 2)
        tokens = enc.encode(text)
        chunks: List[str] = []
        for i in range(0, len(tokens), cap):
            chunks.append(enc.decode(tokens[i : i + cap]))
        return chunks


# ============================================================================
# Façade — TokenManager
# ============================================================================
class TokenManager:
    """Front door for pipelines.  Bundles all four strategies."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        usable_ratio: float = 0.85,
        per_request_cost_cap_usd: float = 1.00,
    ) -> None:
        self.provider = provider
        self.pruner = ContextPruner(provider, usable_ratio=usable_ratio)
        self.guard = CostGuard(
            provider, per_request_cost_cap_usd=per_request_cost_cap_usd
        )

    # ----------------------------------------------------- factory: role-aware
    @classmethod
    def for_role(
        cls,
        router: Any,
        role: Optional[str] = None,
        strategy: Any = None,
        task: Optional[str] = None,
        usable_ratio: float = 0.85,
        per_request_cost_cap_usd: float = 1.00,
    ) -> "TokenManager":
        """Build a TokenManager that targets the LLM that will actually run.

        ``router`` is an :class:`LLMRouter` (or anything with ``get_provider_for``).
        We pick the live provider for the given (role, strategy, task) tuple
        so the context window we trim against matches the model that's about
        to receive the prompt — instead of using a default 8 K window for
        everyone.

        Falls back gracefully when ``router`` does not expose
        ``get_provider_for`` (older code paths) by using whatever provider
        the router exposes.
        """
        provider: Optional[BaseLLMProvider] = None
        try:
            getter = getattr(router, "get_provider_for", None)
            if callable(getter):
                if strategy is not None:
                    provider = getter(role=role, strategy=strategy, task=task)
                else:
                    provider = getter(role=role, task=task)
            if provider is None and hasattr(router, "providers"):
                # Last resort: pick any provider so we still produce a manager.
                provider = next(iter(router.providers.values()), None)
        except Exception as exc:  # pragma: no cover
            logger.debug("TokenManager.for_role fallback: %s", exc)
        if provider is None:
            raise ValueError("Router has no providers; cannot build TokenManager")
        return cls(
            provider,
            usable_ratio=usable_ratio,
            per_request_cost_cap_usd=per_request_cost_cap_usd,
        )

    # ----------------------------------------------------- diagnostic
    def budget_info(self) -> Dict[str, Any]:
        """Snapshot of the current budget settings for the UI / logs."""
        return {
            "model": getattr(self.provider, "model_name", "unknown"),
            "max_window": self.pruner.max_window,
            "usable_window": self.pruner.usable_window,
            "hard_cap": self.guard.hard_cap,
        }

    # ------------------------------------------------------------- counting
    def count_tokens(self, text: str) -> int:
        return self.pruner.count_tokens(text)

    # ------------------------------------------------------------- strip
    def strip_comments(self, code: str, language: str) -> str:
        return CommentStripper.strip(code, language)

    # ------------------------------------------------------------- fold
    def fold_functions(
        self, code: str, language: str, keep_symbols: Optional[Iterable[str]] = None
    ) -> str:
        return FunctionFolder.fold(code, language, keep_symbols)

    # ------------------------------------------------------------- prune
    def compress_context(
        self,
        items: List[Dict[str, Any]],
        query: str,
        language: str = "python",
    ) -> List[Dict[str, str]]:
        """Backward-compatible legacy API used by older code.

        Returns a list of ``{'content': str, ...}`` dicts.
        """
        ctx_items: List[ContextItem] = []
        for it in items:
            ctx_items.append(
                ContextItem(
                    content=it.get("content", ""),
                    priority=int(it.get("priority", 0)),
                    role=it.get("role", "context"),
                    language=it.get("language", language),
                    label=it.get("id") or it.get("label"),
                    keep_symbols=list(it.get("keep_symbols") or []),
                )
            )
        reserved = self.count_tokens(query)
        kept, _report = self.pruner.prune(ctx_items, reserved_tokens=reserved)
        out: List[Dict[str, str]] = []
        for item, src in zip(kept, items[: len(kept)]):
            new = dict(src)
            new["content"] = item.content
            out.append(new)
        return out

    # ------------------------------------------------------------- pipeline helper
    def compress_for_llm(
        self,
        items: List[ContextItem],
        reserved_tokens: int = 0,
    ) -> Tuple[List[ContextItem], Dict[str, Any]]:
        """Single-call wrapper used by Edit/Write pipelines."""
        return self.pruner.prune(items, reserved_tokens=reserved_tokens)

    # ------------------------------------------------------------- guard
    def check_messages_cost(self, messages: List[LLMMessage]) -> Dict[str, Any]:
        return self.guard.check_messages(messages)

    def split_long_text(self, text: str, max_chunk_tokens: Optional[int] = None) -> List[str]:
        return self.guard.chunk_text(text, max_chunk_tokens)


# ============================================================================
# Re-export common Role for convenience
# ============================================================================
__all__ = [
    "PRESERVED_MARKERS",
    "CommentStripper",
    "FunctionFolder",
    "ContextItem",
    "ContextPruner",
    "CostGuard",
    "TokenManager",
    "Role",
]
