"""AST-aware code chunker.

Produces ``CodeChunk`` objects whose ``symbol_name`` field is populated
from the parser, so the chunks can later be searched by symbol name via
``SemanticSearchEngine`` (the ``symbol`` / ``fuzzy_symbol`` branch in
``omnicode/search/engine.py`` does a SQL ``LIKE`` over the JSON-serialized
metadata column to find matches).

Languages we recognise are mapped to the same set ``UnifiedASTParser``
supports — Python / JavaScript / TypeScript / C / C++ / Java / Go / Rust.
For files whose language we cannot parse, we fall back to a single
file-level chunk so semantic search still has *something* to embed.
"""

import logging
import re
from typing import List, Optional

from pydantic import BaseModel

from .parser import UnifiedASTParser

logger = logging.getLogger(__name__)

CHUNKER_VERSION = "ast-chunker.v1"


# ---------------------------------------------------------------------------
# Language inference — maps file extensions to the names ``UnifiedASTParser``
# expects.  Anything we don't know about returns ``None`` and triggers the
# whole-file fallback chunker.
# ---------------------------------------------------------------------------
_EXT_LANG_MAP = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "c": "c",
    "h": "c",
    "cc": "cpp",
    "cpp": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "hh": "cpp",
    "java": "java",
    "go": "go",
    "rs": "rust",
}


def _normalize_language(value: str) -> Optional[str]:
    if not value:
        return None
    v = value.lower().lstrip(".")
    return _EXT_LANG_MAP.get(v, v if v in {
        "python", "javascript", "typescript", "c", "cpp", "java", "go", "rust",
    } else None)


# ---------------------------------------------------------------------------
class CodeChunk(BaseModel):
    chunk_id: str
    file_path: str
    chunk_type: str
    content: str
    start_line: int
    end_line: int
    symbol_name: Optional[str] = None
    signature: Optional[str] = None
    docstring: Optional[str] = None


_DOCSTRING_RE = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', re.DOTALL)


class ASTChunker:
    """Chunk source into per-symbol slices with proper symbol metadata."""

    def __init__(self, parser: UnifiedASTParser):
        self.parser = parser

    # --------------------------------------------------------------- public
    def chunk_file(self, content: str, file_path: str, language: str) -> List[CodeChunk]:
        lang = _normalize_language(language)
        if not lang:
            logger.debug("Unknown language %r for %s — using whole-file chunk", language, file_path)
            return self._basic_chunking(content, file_path, chunk_type="file")

        try:
            symbols = self.parser.extract_symbols(content, lang) or []
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("AST extract failed for %s (%s): %s", file_path, lang, exc)
            return self._basic_chunking(content, file_path, chunk_type="parse_error")

        lines = content.splitlines()
        total_lines = len(lines)
        chunks: List[CodeChunk] = [
            CodeChunk(
                chunk_id=f"{file_path}:overview",
                file_path=file_path,
                chunk_type="file_overview",
                content=self._make_overview(content, file_path, symbols),
                start_line=1,
                end_line=max(1, total_lines),
            )
        ]

        if not symbols:
            # Parsable but contained no functions/classes — keep the overview
            # chunk and add the whole file so we can still do semantic search.
            chunks.extend(self._basic_chunking(content, file_path, chunk_type="module_body"))
            return chunks

        for idx, sym in enumerate(symbols):
            name = sym.get("name") or "<anonymous>"
            stype = sym.get("type") or "symbol"
            sline = sym.get("line_start") or sym.get("start_line") or 1
            eline = sym.get("line_end") or sym.get("end_line") or sline
            sline = max(1, int(sline))
            eline = max(sline, int(eline))
            slice_lines = lines[sline - 1: eline]
            chunk_text = "\n".join(slice_lines)
            if not chunk_text.strip():
                continue

            chunks.append(
                CodeChunk(
                    chunk_id=f"{file_path}:{stype}:{name}:{sline}:{idx}",
                    file_path=file_path,
                    chunk_type=stype,
                    content=chunk_text,
                    start_line=sline,
                    end_line=eline,
                    symbol_name=name,
                    signature=self._extract_signature(slice_lines),
                    docstring=self._extract_docstring(chunk_text, lang),
                )
            )
        return chunks

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _make_overview(content: str, file_path: str, symbols: list) -> str:
        names = [s.get("name") for s in symbols if s.get("name")]
        head = "\n".join(content.splitlines()[:20])
        listing = ", ".join(names[:30]) if names else "no top-level symbols"
        return f"File: {file_path}\nSymbols: {listing}\n---\n{head}"

    @staticmethod
    def _extract_signature(slice_lines: list[str]) -> str:
        for ln in slice_lines:
            stripped = ln.strip()
            if stripped:
                return stripped[:200]
        return ""

    @staticmethod
    def _extract_docstring(chunk_text: str, lang: str) -> str:
        if lang != "python":
            return ""
        m = _DOCSTRING_RE.search(chunk_text)
        if not m:
            return ""
        return (m.group(1) or m.group(2) or "").strip()[:500]

    def _basic_chunking(
        self,
        content: str,
        file_path: str,
        *,
        chunk_type: str = "fallback",
    ) -> List[CodeChunk]:
        """Fallback chunker for files we can't parse with Tree-sitter."""
        lines = content.splitlines() or [""]
        return [
            CodeChunk(
                chunk_id=f"{file_path}:{chunk_type}",
                file_path=file_path,
                chunk_type=chunk_type,
                content=content[:4000] if len(content) > 4000 else content,
                start_line=1,
                end_line=len(lines),
            )
        ]
