"""
High-level MCP tools — the eight-tool core surface.

These eight tools are designed so an external AI editor can map the
"what do I want to do" question to a single tool with one ``mode`` /
``action`` parameter — no need to navigate 25 fine-grained tools and
spend 10k tokens on schema descriptions.

Token savings: ~10k schema tokens → ~3.5k. Tool descriptions kept short.

Tool roster:

  omni_search       — semantic / symbol / text / hybrid / references
  omni_read         — outline / symbols / full / range / imports / diagnostics
  omni_impact       — callers / callees / risk / related tests
  omni_diagnostics  — lint / type / security checks for a file or workspace
  omni_context      — composer: outline + impact + memory + git in one call
  omni_memory       — store / search / advisory
  omni_patch        — preview / validate / apply / rollback (safe edit)
  discover_tools    — discovery + capability listing

Backwards compatibility: ``omni_analyze``, ``omni_edit``,
``omni_intelligence`` are kept as deprecated aliases that delegate to
the new tools, so older MCP configs don't break.
"""

import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _format_json(data: Any, max_lines: int = 80) -> str:
    """Format data as readable JSON, truncated if too long."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
    return text


# ---------------------------------------------------------------------------
# omni_search helpers
# ---------------------------------------------------------------------------

# Default globs when the caller doesn't pass file_pattern. Mirrors the
# backend default in omnicode_core.search.text_grep.
_DEFAULT_TEXT_GLOBS = (
    "*.py,*.js,*.jsx,*.ts,*.tsx,*.go,*.rs,*.java,*.cpp,*.c,*.h,"
    "*.rb,*.php,*.kt,*.cs,*.md,*.toml,*.yaml,*.yml,*.json"
)

_IDENT_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_QUOTED_RE = re.compile(r'^"[^"]+"$|^\'[^\']+\'$')


def _detect_mode(query: str) -> str:
    """Pick a sensible default mode from the query shape.

    Heuristics, ordered by confidence:

    1. Exact ALL_CAPS_IDENTIFIER → ``text``  (env vars, constants).
    2. Quoted literal ``"foo bar"`` → ``text``.
    3. Dotted / underscored identifier (no spaces, < 60 chars) → ``symbol``.
    4. Short natural-language query (≤ 3 words) → ``hybrid``.
    5. Anything else → ``semantic``.
    """
    q = query.strip()
    if not q:
        return "semantic"

    if _CONST_RE.fullmatch(q):
        return "text"

    if _QUOTED_RE.fullmatch(q):
        return "text"

    if _IDENT_RE.fullmatch(q) and len(q) <= 60:
        return "symbol"

    word_count = len(q.split())
    if word_count <= 3 and len(q) <= 40:
        return "hybrid"

    return "semantic"


async def _run_semantic(
    make_request, query: str, file_pattern: Optional[str], max_results: int, rerank: bool
) -> Tuple[List[Dict[str, Any]], int]:
    """Pure semantic (FAISS) search. Optional cross-encoder rerank."""
    payload: Dict[str, Any] = {
        "query": query,
        "search_type": "semantic",
        "max_results": max(max_results, 10),
    }
    if file_pattern:
        payload["file_pattern"] = file_pattern.split(",")[0].strip()

    raw = await make_request("POST", "/search", json=payload)
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    results = list(data.get("results", []))

    if rerank:
        # The backend already invokes the reranker when OMNICODE_RERANKER=true,
        # but we add the tag here so the tool surface still mentions reranking
        # is on by default at the MCP layer (informational only).
        for r in results:
            why = list(r.get("why_matched", []) or [])
            if "reranker:requested" not in why:
                why.append("reranker:requested")
            r["why_matched"] = why

    return results, data.get("total_results", len(results))


async def _run_symbol(
    make_request, query: str, file_pattern: Optional[str], max_results: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Fuzzy symbol-name search."""
    params = {"query": query, "fuzzy": True, "max_results": max_results}
    if file_pattern:
        # symbol_search backend takes a single glob, take the first.
        params["file_pattern"] = file_pattern.split(",")[0].strip()

    raw = await make_request("POST", "/search/symbols", params=params)
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    results = list(data.get("results", []))
    return results, data.get("total_results", len(results))


async def _run_text(
    make_request, query: str, file_pattern: Optional[str], max_results: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Line-level grep across the workspace."""
    params = {
        "query": query,
        "file_pattern": file_pattern or _DEFAULT_TEXT_GLOBS,
        "max_results": max_results,
        "context_lines": 2,
    }
    raw = await make_request("POST", "/search/text", params=params)
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    results = list(data.get("results", []))
    return results, data.get("total_results", len(results))


async def _run_hybrid(
    make_request, query: str, file_pattern: Optional[str], max_results: int, rerank: bool
) -> Tuple[List[Dict[str, Any]], int]:
    """Run symbol + semantic searches in parallel, fuse with RRF."""
    import asyncio

    # Over-fetch each side so RRF has material to work with.
    overfetch = max(max_results * 3, 15)
    sym_task = _run_symbol(make_request, query, file_pattern, overfetch)
    sem_task = _run_semantic(make_request, query, file_pattern, overfetch, rerank)
    sym_pair, sem_pair = await asyncio.gather(sym_task, sem_task)

    sym_results, _ = sym_pair
    sem_results, _ = sem_pair

    fused = _rrf_fuse([sym_results, sem_results], labels=["symbol", "semantic"])
    return fused[:max_results], len(fused)


def _rrf_fuse(
    lists: List[List[Dict[str, Any]]], labels: List[str], k: int = 60
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple result lists.

    Each item is keyed by ``(file_path, line_start_or_line_number,
    symbol_name)`` so the same hit found by both sources merges.
    """
    scores: Dict[Tuple[str, Any, str], float] = {}
    items: Dict[Tuple[str, Any, str], Dict[str, Any]] = {}
    sources: Dict[Tuple[str, Any, str], List[str]] = {}

    for lst, label in zip(lists, labels, strict=False):
        for rank, item in enumerate(lst):
            key = (
                item.get("file_path", ""),
                item.get("line_start") or item.get("line_number") or 0,
                item.get("symbol_name", ""),
            )
            if key not in scores:
                scores[key] = 0.0
                items[key] = dict(item)
                sources[key] = []
            scores[key] += 1.0 / (k + rank + 1)
            if label not in sources[key]:
                sources[key].append(label)

    fused = []
    for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        item = items[key]
        item["relevance_score"] = score
        why = list(item.get("why_matched", []) or [])
        for label in sources[key]:
            tag = f"hybrid:{label}"
            if tag not in why:
                why.append(tag)
        item["why_matched"] = why
        fused.append(item)
    return fused


async def _run_references(
    make_request, query: str, max_results: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Find every usage of a symbol via the LSP bridge.

    The LSP ``references`` endpoint takes a position, not a name, so we
    first look up the symbol via ``/lsp/workspace-symbols`` to anchor
    on a real declaration, then ask LSP for the usages.
    """
    # Step 1: locate the declaration via workspace-symbols.
    raw = await make_request("GET", "/lsp/workspace-symbols", params={"query": query})
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    locations = data.get("symbols", []) or data.get("locations", [])
    if not locations:
        # Fall back to the AST symbol search to find a candidate.
        sym_results, _ = await _run_symbol(make_request, query, None, 1)
        if not sym_results:
            return [], 0
        first = sym_results[0]
        anchor_file = first.get("file_path", "")
        anchor_line = (first.get("line_start") or 1) - 1
        anchor_col = 0
    else:
        anchor = locations[0]
        anchor_file = (
            anchor.get("file_path")
            or anchor.get("file")
            or anchor.get("uri", "").replace("file://", "")
        )
        loc = anchor.get("location") or anchor
        rng = loc.get("range", {}).get("start", {}) if isinstance(loc, dict) else {}
        anchor_line = rng.get("line", 0)
        anchor_col = rng.get("character", 0)

    if not anchor_file:
        return [], 0

    # Step 2: ask LSP for references at that position.
    raw = await make_request(
        "POST",
        "/lsp/references",
        params={
            "file": anchor_file,
            "line": anchor_line,
            "col": anchor_col,
            "include_declaration": True,
        },
    )
    data = raw.get("result", raw) if isinstance(raw, dict) else {}
    refs = data.get("locations") or data.get("references") or []

    results = []
    for ref in refs[:max_results]:
        file = (
            ref.get("file_path")
            or ref.get("file")
            or ref.get("uri", "").replace("file://", "")
        )
        rng = ref.get("range", {}).get("start", {}) if isinstance(ref, dict) else {}
        line = rng.get("line", 0) + 1  # LSP is 0-indexed; show 1-indexed
        results.append(
            {
                "file_path": file,
                "line_number": line,
                "symbol_name": query,
                "match_type": "reference",
                "relevance_score": 1.0,
                "why_matched": ["lsp:references"],
            }
        )

    return results, len(refs)


def _rerank_by_proximity(
    results: List[Dict[str, Any]], around_file: str
) -> List[Dict[str, Any]]:
    """Bump results that sit close to ``around_file`` in the file tree.

    Same-file > same-directory > parent-directory > rest.
    """
    anchor = PurePosixPath(around_file.replace("\\", "/"))

    def proximity_score(path: str) -> int:
        if not path:
            return 0
        p = PurePosixPath(path.replace("\\", "/"))
        if p == anchor:
            return 100
        if p.parent == anchor.parent:
            return 50
        # one directory above
        if str(p.parent).startswith(str(anchor.parent.parent)):
            return 20
        return 0

    enriched = []
    for r in results:
        bump = proximity_score(r.get("file_path", ""))
        if bump:
            r["relevance_score"] = float(r.get("relevance_score", 0)) + bump / 100
            why = list(r.get("why_matched", []) or [])
            why.append(f"proximity:{bump}")
            r["why_matched"] = why
        enriched.append(r)

    enriched.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return enriched


def _no_results_message(query: str, resolved_mode: str, requested_mode: str) -> str:
    """Render an actionable hint when search came back empty."""
    suggestions = []
    if resolved_mode != "text":
        suggestions.append("• `mode=text`   — line-level grep on the literal string")
    if resolved_mode != "symbol":
        suggestions.append("• `mode=symbol` — fuzzy match on function/class names")
    if resolved_mode != "semantic":
        suggestions.append("• `mode=semantic` — natural-language → code")
    if resolved_mode != "hybrid":
        suggestions.append("• `mode=hybrid` — fuse symbol + semantic via RRF")

    # Lightweight query-rewrite suggestions.
    rewrites: List[str] = []
    q = query.strip()
    parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[0-9]+", q.replace("_", " "))
    if parts and " ".join(parts).lower() != q.lower():
        rewrites.append(f"`{' '.join(parts).lower()}`  (split identifier into words)")
    if "_" in q and " " not in q:
        rewrites.append(f"`{q.replace('_', ' ')}`  (treat underscores as spaces)")
    if len(q) > 30:
        rewrites.append(f"`{' '.join(q.split()[:3])}…`  (shorter, focus on core terms)")

    if requested_mode == "auto":
        header = f"🔍 No results for '{query}' (mode=auto→{resolved_mode})"
    else:
        header = f"🔍 No results for '{query}' (mode={resolved_mode})"

    lines = [header, ""]
    if suggestions:
        lines.append("Try a different mode:")
        lines.extend(suggestions)
        lines.append("")
    if rewrites:
        lines.append("Or rewrite the query:")
        for r in rewrites:
            lines.append(f"• {r}")
        lines.append("")
    lines.append("Common misses: case sensitivity, language filter (try `file_pattern='*'`).")
    return "\n".join(lines)


def _approx_token_count(text: str) -> int:
    """Rough estimator — every 4 chars is ~1 token. Good enough for budget gates."""
    return max(1, len(text) // 4)


def _render_results(
    *,
    query: str,
    requested_mode: str,
    resolved_mode: str,
    results: List[Dict[str, Any]],
    total: int,
    token_budget: int,
) -> str:
    """Render search results as plain text.

    When ``token_budget > 0`` we render in two passes: first a verbose
    pass with snippet + context, then if the total would blow the
    budget we fall back to a compact pass that drops snippets.
    """
    header_mode = (
        f"mode={requested_mode}" if requested_mode == resolved_mode
        else f"mode={requested_mode}→{resolved_mode}"
    )
    header = f"🔍 {total} result(s) for '{query}' ({header_mode})\n"

    verbose = [header]
    for i, r in enumerate(results, 1):
        verbose.extend(_render_one_result(i, r, with_snippet=True))

    full = "\n".join(verbose)

    if token_budget <= 0 or _approx_token_count(full) <= token_budget:
        return full

    # Compact pass — drop snippets / context.
    compact = [header]
    for i, r in enumerate(results, 1):
        compact.extend(_render_one_result(i, r, with_snippet=False))
    compact.append(f"\n(token-budget {token_budget} exceeded full render; snippets dropped)")
    return "\n".join(compact)


def _render_one_result(idx: int, r: Dict[str, Any], *, with_snippet: bool) -> List[str]:
    """Format a single hit — works for symbol / semantic / text / reference."""
    file = r.get("file_path", "")
    line_no = (
        r.get("line_number")
        or r.get("line_start")
        or 0
    )
    name = r.get("symbol_name") or file or "?"
    kind = r.get("symbol_type") or r.get("chunk_type") or r.get("match_type") or ""
    score = r.get("relevance_score", 0)
    why = r.get("why_matched") or []
    sig = r.get("signature", "")
    line_content = r.get("line_content", "")
    ctx_before = r.get("context_before") or []
    ctx_after = r.get("context_after") or []

    out = [f"{idx}. {name}"]
    if file:
        out.append(f"   📄 {file}:{line_no}")
    if kind or why:
        tail = f"score={score:.2f}" if isinstance(score, (int, float)) else ""
        if why:
            tail += f"  why={','.join(why[:4])}"
        out.append(f"   🏷️ {kind}  {tail}".rstrip())

    if not with_snippet:
        out.append("")
        return out

    if line_content:
        # Text-mode hit: render ±N lines context.
        merged_extra = r.get("merged_lines") or []
        if merged_extra:
            out.append(
                f"   📜 snippet (+ {len(merged_extra)} adjacent match"
                f"{'es' if len(merged_extra) != 1 else ''}):"
            )
        else:
            out.append("   📜 snippet:")
        start_line = line_no - len(ctx_before)
        merged_set = set(merged_extra)
        for offset, raw_line in enumerate(ctx_before + [line_content] + ctx_after):
            n = start_line + offset
            if n == line_no or n in merged_set:
                marker = "►"
            else:
                marker = " "
            out.append(f"      {marker} {n:>4} | {raw_line}")
    elif sig:
        # Symbol/semantic hit: render the signature.
        out.append(f"   ✏️ {sig[:160]}")
    out.append("")
    return out


def register_high_level_tools(mcp, make_request):
    """Register the 6+1 high-level tools on the given FastMCP instance.

    Args:
        mcp: FastMCP instance
        make_request: async function(method, endpoint, **kwargs) -> dict
    """

    @mcp.tool()
    async def omni_search(
        query: str,
        mode: str = "auto",
        file_pattern: Optional[str] = None,
        max_results: int = 10,
        rerank: bool = True,
        token_budget: int = 0,
        around_file: Optional[str] = None,
    ) -> str:
        """Search the codebase with adaptive mode selection.

        Modes:
          - auto:       rule-based pick across {symbol, text, hybrid, semantic}
          - hybrid:     run symbol + semantic in parallel, fuse with RRF
          - semantic:   natural language → code (FAISS bi-encoder + optional rerank)
          - symbol:     fuzzy symbol-name matching across functions/classes/methods
          - text:       line-level grep (returns real line numbers + ±2 lines context)
          - references: find every usage of a symbol via the LSP bridge

        Other parameters:
          - file_pattern:  comma-separated globs ("*.py,*.md"); applies to text mode
                           (and is forwarded as a filter where supported).
          - rerank:        request cross-encoder rerank for semantic/hybrid (W2-9).
                           Effective only when ``OMNICODE_RERANKER=true`` in the
                           server env; otherwise the parameter is a no-op.
          - token_budget:  if > 0, trim the rendered output to roughly this many
                           tokens by dropping snippets/context first.
          - around_file:   bias results toward this file's neighbourhood (callgraph
                           hops + same directory). Other files still appear, just
                           lower in rank.

        Returns plain text in a structured layout: per-result file + line, kind,
        score, why_matched tags, and (when budget allows) ±2 lines of code.

        On 0 hits the tool suggests alternative modes and broader queries.
        """
        try:
            resolved_mode = _detect_mode(query) if mode == "auto" else mode

            if resolved_mode == "hybrid":
                results, total = await _run_hybrid(
                    make_request, query, file_pattern, max_results, rerank
                )
            elif resolved_mode == "semantic":
                results, total = await _run_semantic(
                    make_request, query, file_pattern, max_results, rerank
                )
            elif resolved_mode == "symbol":
                results, total = await _run_symbol(
                    make_request, query, file_pattern, max_results
                )
            elif resolved_mode == "text":
                results, total = await _run_text(
                    make_request, query, file_pattern, max_results
                )
            elif resolved_mode == "references":
                results, total = await _run_references(make_request, query, max_results)
            else:
                return (
                    f"❌ Unknown search mode: {mode}.\n"
                    f"   Use one of: auto, hybrid, semantic, symbol, text, references"
                )

            # Bias results toward `around_file`'s neighbourhood when asked.
            if around_file and results:
                results = _rerank_by_proximity(results, around_file)

            if not results:
                return _no_results_message(query, resolved_mode, mode)

            return _render_results(
                query=query,
                requested_mode=mode,
                resolved_mode=resolved_mode,
                results=results[:max_results],
                total=total,
                token_budget=token_budget,
            )

        except Exception as e:
            return f"❌ Search failed: {e}"

    @mcp.tool()
    async def omni_read(
        file: str,
        mode: str = "outline",
        symbol: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> str:
        """Read a file with token-efficient mode selection.

        Modes:
          - outline: signatures + first docstring line (~90% token savings)
          - symbols: just the symbol list (name, kind, lines)
          - full: complete file content
          - imports: only import/require statements
          - diagnostics: only lint issues for this file
          - range: specific line range (use start_line + end_line)
          - symbol: read a specific symbol by name

        Default is 'outline' — use 'full' only when you need every line.
        """
        try:
            params: Dict[str, Any] = {"file_path": file, "with_line_numbers": True}

            if mode == "range" and start_line:
                params["start_line"] = start_line
                params["end_line"] = end_line or (start_line + 50)
                params["mode"] = "full"
            elif mode == "symbol" and symbol:
                params["symbol_name"] = symbol
                params["mode"] = "full"
            else:
                params["mode"] = mode

            result = await make_request("POST", "/read", params=params)

            if "error" in result:
                return f"❌ Read error: {result['error']}"

            data = result.get("result", result)

            # For outline/symbols mode, format nicely
            if mode in ("outline", "symbols") and "symbols" in data:
                symbols = data.get("symbols", [])
                lang = data.get("language", "")
                total = data.get("total_lines", "?")
                lines = [f"📄 {file} ({total} lines, {lang})\n"]

                for s in symbols:
                    name = s.get("name", "?")
                    kind = s.get("kind", "")
                    sl, el = s.get("lines", [0, 0]) if "lines" in s else [s.get("line_start", 0), s.get("line_end", 0)]
                    sig = s.get("signature", "")
                    doc = s.get("doc", "")
                    parent = s.get("parent", "")

                    prefix = "  └─ " if parent else ""
                    lines.append(f"{prefix}{kind} {name}  [L{sl}-{el}]")
                    if sig and mode == "outline":
                        lines.append(f"     {sig[:150]}")
                    if doc and mode == "outline":
                        lines.append(f"     📝 {doc[:100]}")

                return "\n".join(lines)

            # For other modes, return content directly
            content = data.get("content", "")
            if content:
                return f"📄 {file}\n\n{content}"

            return _format_json(data)

        except Exception as e:
            return f"❌ Read failed: {e}"

    @mcp.tool()
    async def omni_impact(
        symbol: str,
        depth: int = 2,
        max_files: int = 200,
    ) -> str:
        """Assess the blast radius of changing a symbol — required reading
        before any non-trivial edit.

        Returns:
          • risk level (low / medium / high) with the reasons,
          • direct callers and callees,
          • files affected,
          • recommended tests to run after the change.

        Combines /graph/impact + /graph/risk + /graph/related-tests in
        parallel so the AI gets one consolidated payload.
        """
        try:
            import asyncio

            params = {"symbol": symbol, "depth": depth, "max_files": max_files}
            risk_task = make_request("GET", "/graph/risk", params={
                "symbol": symbol, "max_files": max_files,
            })
            impact_task = make_request("GET", "/graph/impact", params=params)
            tests_task = make_request("GET", "/graph/related-tests", params={
                "symbol": symbol, "max_files": max_files,
            })
            risk_raw, impact_raw, tests_raw = await asyncio.gather(
                risk_task, impact_task, tests_task, return_exceptions=True,
            )

            def _safe(r):
                if isinstance(r, Exception):
                    return {"error": str(r)}
                return r.get("result", r) if isinstance(r, dict) else {}

            risk = _safe(risk_raw)
            impact = _safe(impact_raw)
            tests = _safe(tests_raw)

            risk_level = risk.get("risk", "unknown")
            risk_reasons = risk.get("reasons", []) or []
            badge = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk_level, "⚪")

            lines = [f"💥 Impact: {symbol}\n"]
            lines.append(f"   {badge} Risk: {risk_level}")
            for reason in risk_reasons[:6]:
                lines.append(f"      • {reason}")

            affected = impact.get("affected", []) or []
            dependents = impact.get("dependents", []) or []
            files_count = impact.get("files_count", 0)

            lines.append("")
            lines.append(f"   ⬇️  Callees affected:  {len(affected)}")
            for n in affected[:8]:
                lines.append(f"      → {n}")
            lines.append(f"   ⬆️  Callers depending: {len(dependents)}")
            for n in dependents[:8]:
                lines.append(f"      ← {n}")
            lines.append(f"   📁  Files in blast radius: {files_count}")

            test_files = tests.get("test_files", []) or []
            if test_files:
                lines.append("")
                lines.append(f"   🧪 Suggested tests ({len(test_files)}):")
                for t in test_files[:6]:
                    lines.append(f"      • {t}")
                cmds = tests.get("suggested_commands", []) or []
                for cmd in cmds[:3]:
                    lines.append(f"      $ {cmd}")

            return "\n".join(lines)

        except Exception as e:
            return f"❌ omni_impact failed: {e}"

    @mcp.tool()
    async def omni_diagnostics(
        file: Optional[str] = None,
        severity: str = "all",
        sources: str = "guard,lsp",
    ) -> str:
        """Get structured lint / type / static-analysis diagnostics.

        Parameters:
          - file:     workspace-relative path. If None, returns a summary
                      across the recently-edited files (TBD; for now
                      ``file`` is required).
          - severity: 'all' | 'error' | 'warning'. Default 'all'.
          - sources:  comma-separated list of 'guard,lsp'. Guard runs ruff
                      + mypy + bandit (Python) or eslint + tsc (JS/TS).
                      LSP returns the language-server diagnostics for the
                      file.

        Returns a structured per-line list so the AI can decide what to
        fix without parsing tool stdout.
        """
        if not file:
            return (
                "❌ omni_diagnostics requires a file path.\n"
                "   Workspace-wide aggregation is on the roadmap — for "
                "now scope to a single file."
            )
        try:
            import asyncio

            wanted = {s.strip() for s in sources.split(",") if s.strip()}

            tasks = []
            labels = []
            if "guard" in wanted:
                tasks.append(make_request(
                    "POST", "/guard/check", params={"file_path": file},
                ))
                labels.append("guard")
            if "lsp" in wanted:
                tasks.append(make_request(
                    "GET", f"/lsp/diagnostics/{file}",
                ))
                labels.append("lsp")

            if not tasks:
                return f"❌ Unknown sources '{sources}'. Use: guard, lsp"

            raws = await asyncio.gather(*tasks, return_exceptions=True)

            all_issues: List[Dict[str, Any]] = []

            for label, raw in zip(labels, raws, strict=False):
                if isinstance(raw, Exception):
                    all_issues.append({
                        "source": label,
                        "severity": "info",
                        "line": None,
                        "rule": "tool_unavailable",
                        "message": f"{label} call failed: {raw}",
                    })
                    continue
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                if label == "guard":
                    issues = data.get("issues", []) or []
                    for it in issues:
                        all_issues.append({
                            "source": it.get("tool") or "guard",
                            "severity": it.get("severity") or "warning",
                            "line": it.get("line"),
                            "column": it.get("column"),
                            "rule": it.get("code") or "",
                            "message": it.get("message") or "",
                        })
                    # legacy text fallback if backend didn't structure
                    if not issues:
                        for txt in (data.get("errors") or "").splitlines():
                            if txt.strip():
                                all_issues.append({
                                    "source": "guard",
                                    "severity": "error",
                                    "line": None,
                                    "rule": "",
                                    "message": txt,
                                })
                elif label == "lsp":
                    diags = data.get("diagnostics", []) or []
                    for d in diags:
                        rng = d.get("range", {}).get("start", {}) if isinstance(d, dict) else {}
                        all_issues.append({
                            "source": "lsp",
                            "severity": d.get("severity") or "warning",
                            "line": rng.get("line"),
                            "column": rng.get("character"),
                            "rule": d.get("code") or "",
                            "message": d.get("message") or "",
                        })

            # Filter by severity
            sev = severity.lower().strip()
            if sev not in ("all", ""):
                wanted_set = {sev}
                if sev == "error":
                    wanted_set = {"error"}
                elif sev == "warning":
                    wanted_set = {"warning", "warn"}
                all_issues = [
                    i for i in all_issues
                    if (i.get("severity") or "").lower() in wanted_set
                ]

            if not all_issues:
                return f"✅ {file} — no diagnostics ({severity}, sources={sources})"

            # Sort: errors first, then by line
            sev_rank = {"error": 0, "warning": 1, "warn": 1, "info": 2, "hint": 3}
            all_issues.sort(key=lambda i: (
                sev_rank.get((i.get("severity") or "").lower(), 4),
                i.get("line") or 0,
            ))

            lines = [f"🩺 Diagnostics for {file} ({len(all_issues)} issues)\n"]
            for it in all_issues[:25]:
                emoji = {
                    "error": "❌", "warning": "⚠️", "warn": "⚠️",
                    "info": "ℹ️", "hint": "💡",
                }.get((it.get("severity") or "").lower(), "•")
                line_no = it.get("line")
                col = it.get("column")
                anchor = f"L{line_no}" if line_no else "?"
                if col is not None:
                    anchor += f":{col}"
                rule = f" [{it.get('rule')}]" if it.get("rule") else ""
                lines.append(
                    f"  {emoji} {anchor:<10} {it['source']}{rule}: {it['message']}"
                )
            if len(all_issues) > 25:
                lines.append(f"\n  ... ({len(all_issues) - 25} more issues, increase max if needed)")
            return "\n".join(lines)

        except Exception as e:
            return f"❌ omni_diagnostics failed: {e}"

    @mcp.tool()
    async def omni_patch(
        action: str = "preview",
        file: Optional[str] = None,
        content: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Safe edit operations — never let an LLM write to disk directly.

        Actions:
          - preview:   show a unified diff of what would change
          - validate:  run static checks on the proposed content
          - apply:     write to disk + create snapshot + record EditSession
          - rollback:  restore the file from a previous EditSession's snapshot
          - sessions:  list recent EditSessions

        The recommended flow before any AI-driven edit is:
        preview → validate → apply, then keep the returned session_id
        in case you need to rollback.
        """
        try:
            if action == "preview":
                if not file or content is None:
                    return "❌ omni_patch preview needs both file and content."
                raw = await make_request("POST", "/patch/preview", json={
                    "file_path": file, "content": content,
                })
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                if not data.get("success", True):
                    return f"❌ Preview failed: {data.get('message', 'unknown')}"
                added = data.get("lines_added", 0)
                removed = data.get("lines_removed", 0)
                diff = data.get("diff", "")
                # Cap diff at 80 lines for the MCP response so we don't
                # blow the AI's context with megabytes of changes.
                diff_lines = diff.splitlines()
                if len(diff_lines) > 80:
                    diff = "\n".join(diff_lines[:80]) + (
                        f"\n... ({len(diff_lines) - 80} more diff lines)"
                    )
                return (
                    f"📋 Preview: {file}\n"
                    f"   +{added} / -{removed} lines\n\n"
                    f"{diff}"
                )

            if action == "validate":
                if not file or content is None:
                    return "❌ omni_patch validate needs both file and content."
                raw = await make_request("POST", "/patch/validate", json={
                    "file_path": file, "content": content,
                })
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = data.get("success", False)
                checks = data.get("checks", []) or []
                msg = data.get("message", "")
                lines = [
                    f"{'✅' if ok else '❌'} Validate: {file}",
                    f"   {msg}" if msg else "",
                ]
                for chk in checks[:10]:
                    lines.append(f"   • {chk}")
                return "\n".join(line for line in lines if line)

            if action == "apply":
                if not file or content is None:
                    return "❌ omni_patch apply needs both file and content."
                raw = await make_request("POST", "/patch/apply", json={
                    "file_path": file, "content": content,
                })
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                if not data.get("success", False):
                    return f"❌ Apply failed: {data.get('message', 'unknown')}"
                sid = data.get("session_id")
                added = data.get("lines_added", 0)
                removed = data.get("lines_removed", 0)
                rb = data.get("rollback_available", True)
                return (
                    f"✅ Applied: {file}\n"
                    f"   +{added} / -{removed} lines\n"
                    f"   session_id: {sid}\n"
                    f"   rollback_available: {rb}\n"
                    f"\n   To undo: omni_patch(action='rollback', session_id='{sid}')"
                )

            if action == "rollback":
                if not session_id:
                    return "❌ omni_patch rollback needs session_id."
                raw = await make_request(
                    "POST", "/patch/rollback",
                    params={"session_id": session_id},
                )
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = data.get("success", False)
                msg = data.get("message", "")
                return f"{'✅' if ok else '❌'} Rollback: {msg}"

            if action == "sessions":
                raw = await make_request("GET", "/patch/sessions", params={"limit": 20})
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                sessions = data.get("sessions", []) or []
                if not sessions:
                    return "📜 No recent EditSessions."
                lines = [f"📜 Recent EditSessions ({len(sessions)}):\n"]
                for s in sessions[:20]:
                    sid = s.get("session_id", "?")
                    fp = s.get("file_path", "?")
                    ts = s.get("timestamp", "?")
                    src = s.get("source", "?")
                    a = s.get("lines_added", 0)
                    r = s.get("lines_removed", 0)
                    lines.append(
                        f"  {sid}  {ts}  {fp}  +{a}/-{r}  ({src})"
                    )
                return "\n".join(lines)

            return (
                f"❌ Unknown omni_patch action: {action}.\n"
                f"   Use: preview, validate, apply, rollback, sessions"
            )

        except Exception as e:
            return f"❌ omni_patch failed: {e}"

    # -------------------------------------------------------------------
    # Backwards-compat alias — omni_analyze delegates to omni_impact for
    # the most common case ("impact" analysis). Kept so older MCP configs
    # don't break, but new clients should use omni_impact directly.
    # -------------------------------------------------------------------
    @mcp.tool()
    async def omni_analyze(
        symbol: str,
        analysis: str = "impact",
        depth: int = 2,
        path: Optional[str] = None,
    ) -> str:
        """[deprecated alias] Use omni_impact for impact analysis.

        Analysis types:
          - impact (default): delegates to omni_impact
          - callers / callees / graph: low-level call-graph queries

        Kept for backwards compatibility with older MCP configs.
        """
        try:
            if analysis in ("callers", "callees", "impact"):
                direction = "both" if analysis == "impact" else analysis
                payload = {
                    "symbol": symbol,
                    "direction": direction,
                    "max_files": 200,
                }
                if path:
                    payload["path"] = path

                result = await make_request("POST", "/search/symbols/relations", json=payload)

                if "error" in result:
                    return f"❌ Analysis error: {result['error']}"

                data = result.get("result", result)
                callers = data.get("callers", {})
                callees = data.get("callees", {})

                lines = [f"🔍 Impact analysis: {symbol}\n"]
                lines.append(f"  Total edges in scope: {data.get('total_edges', '?')}")

                if callers:
                    lines.append(f"\n  ⬆️ Callers ({callers.get('count', 0)}):")
                    for name in (callers.get("names") or [])[:15]:
                        lines.append(f"    ← {name}")

                if callees:
                    lines.append(f"\n  ⬇️ Callees ({callees.get('count', 0)}):")
                    for name in (callees.get("names") or [])[:15]:
                        lines.append(f"    → {name}")

                # Risk assessment
                caller_count = callers.get("count", 0) if callers else 0
                risk = "high" if caller_count > 10 else "medium" if caller_count > 3 else "low"
                lines.append(f"\n  ⚠️ Risk: {risk} ({caller_count} direct callers)")

                return "\n".join(lines)

            elif analysis == "graph":
                params = {"max_files": 50, "max_nodes": 30}
                if path:
                    params["path"] = path
                result = await make_request("GET", "/search/symbols/graph", params=params)
                if "error" in result:
                    return f"❌ Graph error: {result['error']}"
                data = result.get("result", result)
                summary = data.get("summary", {})
                return (
                    f"📊 Call graph{' for ' + path if path else ''}\n"
                    f"  Edges: {summary.get('total_edges', 0)}\n"
                    f"  Callers: {summary.get('total_callers', 0)}\n"
                    f"  Callees: {summary.get('total_callees', 0)}"
                )

            return f"❌ Unknown analysis type: {analysis}. Use: impact, callers, callees, graph"

        except Exception as e:
            return f"❌ Analysis failed: {e}"

    @mcp.tool()
    async def omni_memory(
        action: str = "search",
        query: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
        importance: int = 3,
        tags: Optional[str] = None,
    ) -> str:
        """Interact with the project memory system.

        Actions:
          - search: find relevant memories (requires query)
          - store: save a new memory (requires content + category)
          - context: get startup context (recent progress, key learnings)
          - advisory: get auto-recalled memories for current task

        Categories: solution, learning, preference, mistake, architecture,
                   integration, debug, progress
        """
        try:
            if action == "search":
                if not query:
                    return "❌ query is required for memory search"
                result = await make_request("POST", "/memory/search", json={
                    "query": query,
                    "category": category,
                    "max_results": 10,
                    "min_score": 0.3,
                })
                if "error" in result:
                    return f"❌ Memory search error: {result['error']}"
                data = result.get("result", result)
                results = data.get("results", [])
                if not results:
                    return f"🧠 No memories found for '{query}'"
                lines = [f"🧠 {len(results)} memory(ies) for '{query}'\n"]
                for r in results:
                    mem = r.get("memory", {})
                    score = r.get("relevance_score", 0)
                    reason = r.get("match_reason", "")
                    lines.append(f"  [{mem.get('category', '?')}] (score={score:.2f})")
                    lines.append(f"  {mem.get('content', '')[:200]}")
                    if reason:
                        lines.append(f"  📍 {reason}")
                    lines.append("")
                return "\n".join(lines)

            elif action == "store":
                if not content or not category:
                    return "❌ content and category are required for memory store"
                tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
                result = await make_request("POST", "/memory/store", json={
                    "category": category,
                    "content": content,
                    "importance": importance,
                    "tags": tag_list,
                    "related_files": [],
                    "context": {},
                })
                if "error" in result:
                    return f"❌ Memory store error: {result['error']}"
                return f"✅ Memory stored (category={category}, importance={importance})"

            elif action == "context":
                result = await make_request("GET", "/memory/context")
                if "error" in result:
                    return f"❌ Memory context error: {result['error']}"
                return _format_json(result.get("result", result))

            return f"❌ Unknown action: {action}. Use: search, store, context"

        except Exception as e:
            return f"❌ Memory operation failed: {e}"

    @mcp.tool()
    async def omni_context(
        file: str,
        symbol: Optional[str] = None,
        task: Optional[str] = None,
    ) -> str:
        """Get comprehensive context for a file/symbol in one call.

        Returns (in a single response):
          1. File outline (signatures)
          2. Symbol callers + callees (if symbol provided)
          3. Related diagnostics
          4. Recent git changes
          5. Related memories

        This is the recommended first call before modifying any code —
        gives the AI everything it needs in ~500-800 tokens instead of
        requiring 5+ separate tool calls.
        """
        try:
            sections = []

            # 1. Outline
            outline_result = await make_request("POST", "/read", params={
                "file_path": file, "mode": "outline", "with_line_numbers": True,
            })
            outline_data = (outline_result.get("result") or {})
            symbols = outline_data.get("symbols", [])
            total_lines = outline_data.get("total_lines", "?")
            lang = outline_data.get("language", "")

            sections.append(f"📄 {file} ({total_lines} lines, {lang})")
            sections.append(f"   {len(symbols)} symbols")
            for s in symbols[:20]:
                name = s.get("name", "?")
                kind = s.get("kind", "")
                sl = s.get("lines", [0])[0] if "lines" in s else 0
                sig = s.get("signature", "")
                sections.append(f"   {kind} {name} [L{sl}] {sig[:80]}")

            # 2. Callers/callees (if symbol provided)
            if symbol:
                rel_result = await make_request("POST", "/search/symbols/relations", json={
                    "symbol": symbol, "direction": "both", "max_files": 100,
                })
                rel_data = rel_result.get("result", {})
                callers = rel_data.get("callers", {})
                callees = rel_data.get("callees", {})
                if callers and callers.get("count"):
                    sections.append(f"\n   ⬆️ Callers of {symbol}: {', '.join((callers.get('names') or [])[:8])}")
                if callees and callees.get("count"):
                    sections.append(f"   ⬇️ Callees of {symbol}: {', '.join((callees.get('names') or [])[:8])}")

            # 3. Diagnostics
            diag_result = await make_request("POST", "/read", params={
                "file_path": file, "mode": "diagnostics", "with_line_numbers": True,
            })
            diag_data = diag_result.get("result", {})
            diags = diag_data.get("diagnostics", [])
            if diags:
                sections.append(f"\n   ⚠️ {len(diags)} diagnostic(s)")

            # 4. Related memories
            if task or symbol:
                mem_query = task or symbol or file
                mem_result = await make_request("POST", "/memory/search", json={
                    "query": mem_query, "max_results": 3, "min_score": 0.3,
                })
                mem_data = mem_result.get("result", {})
                memories = mem_data.get("results", [])
                if memories:
                    sections.append(f"\n   🧠 {len(memories)} related memory(ies):")
                    for m in memories[:3]:
                        content = m.get("memory", {}).get("content", "")[:100]
                        sections.append(f"      • {content}")

            return "\n".join(sections)

        except Exception as e:
            return f"❌ Context gathering failed: {e}"

    @mcp.tool()
    async def omni_edit(
        action: str = "preview",
        file: Optional[str] = None,
        patch: Optional[str] = None,
        instructions: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """[deprecated alias] Use omni_patch for safe edits.

        Actions:
          - preview / validate / apply / rollback: delegate to omni_patch
          - ai_edit: LLM-driven edit (only when OMNICODE_LLM_ROUTER=true)

        Kept so older MCP configs don't break.
        """
        try:
            if action == "ai_edit":
                if not file or not instructions:
                    return "❌ file and instructions are required for ai_edit"
                result = await make_request("POST", "/edit", json={
                    "target_file": file,
                    "instructions": instructions,
                    "code_edit": patch or "#",
                    "save_to_file": True,
                })
                if "error" in result:
                    return f"❌ Edit error: {result['error']}"
                data = result.get("result", result)
                success = data.get("success", False)
                if success:
                    score = data.get("quality_score", 0)
                    sid = data.get("edit_session_id")
                    line = f"✅ Edit applied to {file} (quality={score:.2f})"
                    if sid:
                        line += (
                            f"\n   session_id: {sid}\n"
                            f"   To undo: omni_patch(action='rollback', "
                            f"session_id='{sid}')"
                        )
                    return line
                else:
                    analysis = data.get("failure_analysis", {})
                    stage = analysis.get("stage", "?")
                    reason = analysis.get("root_cause", analysis.get("failure_reasons", "unknown"))
                    return f"❌ Edit failed at stage '{stage}': {reason}"

            # Forward preview / validate / apply / rollback to omni_patch
            if action in ("preview", "validate", "apply", "rollback"):
                # Re-invoke through the same make_request chain — content
                # comes in via the legacy ``patch`` param so we map it.
                content = patch
                if action == "preview":
                    if not file or content is None:
                        return "❌ omni_edit preview needs both file and patch."
                    raw = await make_request("POST", "/patch/preview", json={
                        "file_path": file, "content": content,
                    })
                elif action == "validate":
                    if not file or content is None:
                        return "❌ omni_edit validate needs both file and patch."
                    raw = await make_request("POST", "/patch/validate", json={
                        "file_path": file, "content": content,
                    })
                elif action == "apply":
                    if not file or content is None:
                        return "❌ omni_edit apply needs both file and patch."
                    raw = await make_request("POST", "/patch/apply", json={
                        "file_path": file, "content": content,
                    })
                else:  # rollback
                    if not session_id:
                        return "❌ omni_edit rollback needs session_id."
                    raw = await make_request(
                        "POST", "/patch/rollback",
                        params={"session_id": session_id},
                    )
                data = raw.get("result", raw) if isinstance(raw, dict) else {}
                ok = data.get("success", False)
                msg = data.get("message", "")
                return (
                    f"{'✅' if ok else '❌'} {action}: {msg}\n"
                    f"   (omni_edit is a deprecated alias — prefer omni_patch)"
                )

            return f"❌ Unknown action: {action}. Use: preview, validate, apply, rollback, ai_edit"

        except Exception as e:
            return f"❌ Edit operation failed: {e}"

    @mcp.tool()
    async def omni_intelligence(
        task: Optional[str] = None,
        file: Optional[str] = None,
        symbol: Optional[str] = None,
        query: Optional[str] = None,
        token_budget: int = 4096,
        impact_depth: int = 2,
        max_search_results: int = 5,
    ) -> str:
        """Single-call multi-capability composer (Intelligence Layer).

        Combines code understanding + search + impact + memory + git
        history into one structured payload that fits the requested
        token budget. Use when the editor needs broad context for an
        unfamiliar file or symbol — far cheaper than chaining the six
        single-purpose tools by hand.

        Returns JSON-formatted IntelligenceContext with capability
        status, results from each capability that ran, and a flat
        ``advisories`` list summarising what to watch out for.
        """
        try:
            payload = {
                "task": task,
                "file_path": file,
                "symbol": symbol,
                "query": query,
                "token_budget": token_budget,
                "impact_depth": impact_depth,
                "max_search_results": max_search_results,
            }
            res = await make_request(
                "POST", "/intelligence/context", json=payload
            )
            if not res.get("success"):
                return f"❌ Intelligence call failed: {res.get('error')}"
            ctx = res.get("result", {})
            # Compact rendering for the LLM caller — preserves structure
            # but trims keys that don't help the editor decide what to do.
            import json as _json

            return _json.dumps(
                {
                    "elapsed_ms": ctx.get("elapsed_ms"),
                    "token_estimate": ctx.get("token_estimate"),
                    "token_budget": ctx.get("token_budget"),
                    "advisories": ctx.get("advisories", []),
                    "capability_status": ctx.get("capability_status", []),
                    "code_understanding": ctx.get("code_understanding", {}),
                    "search": ctx.get("search", {}),
                    "impact": ctx.get("impact", {}),
                    "memory": ctx.get("memory", {}),
                    "git_history": ctx.get("git_history", {}),
                    "errors": ctx.get("errors", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            return f"❌ omni_intelligence failed: {exc}"

    @mcp.tool()
    async def discover_tools(query: str = "") -> str:
        """Discover available OmniCode tools and their capabilities.

        Call with a query to find relevant tools, or empty to list all.
        This is useful when you're not sure which tool to use.

        Eight core tools (recommended):
          - omni_search:      search code (auto/semantic/symbol/text/hybrid/references)
          - omni_read:        read files (outline/symbols/full/imports/diagnostics/range)
          - omni_impact:      blast radius — callers / callees / risk / related tests
          - omni_diagnostics: lint / type / static analysis for a file
          - omni_context:     composer — outline + impact + memory + git in one call
          - omni_memory:      project memory (search/store/advisory)
          - omni_patch:       safe edit (preview / validate / apply / rollback)
          - discover_tools:   this tool

        Deprecated aliases (still work, prefer the named replacements):
          - omni_analyze   → omni_impact
          - omni_edit      → omni_patch
          - omni_intelligence → omni_context
        """
        tools_info = {
            "omni_search": "Search code (auto/semantic/symbol/text/hybrid/references)",
            "omni_read": "Read files (outline/symbols/full/imports/diagnostics/range)",
            "omni_impact": "Blast radius — callers/callees/risk/suggested tests",
            "omni_diagnostics": "Lint / type / static-analysis diagnostics for a file",
            "omni_context": "Composer — outline + impact + memory + git in one call",
            "omni_memory": "Project memory (search/store/advisory)",
            "omni_patch": "Safe edit (preview / validate / apply / rollback)",
            "discover_tools": "This tool — find what's available",
        }

        if not query:
            lines = ["📦 OmniCode tools:\n"]
            for name, desc in tools_info.items():
                lines.append(f"  • {name:<18} {desc}")
            lines.append("")
            lines.append(
                "💡 Recommended flow before any edit:\n"
                "   1. omni_context(file=…)                 — get the lay of the land\n"
                "   2. omni_impact(symbol=…)                — check blast radius\n"
                "   3. omni_diagnostics(file=…)             — see existing issues\n"
                "   4. omni_patch(action='preview', …)      — render the diff\n"
                "   5. omni_patch(action='validate', …)     — run static checks\n"
                "   6. omni_patch(action='apply', …)        — write + create rollback hook"
            )
            return "\n".join(lines)

        # Filter by query
        q = query.lower()
        matches = [(n, d) for n, d in tools_info.items() if q in n.lower() or q in d.lower()]
        if matches:
            lines = [f"🔍 Tools matching '{query}':\n"]
            for name, desc in matches:
                lines.append(f"  • {name}: {desc}")
            return "\n".join(lines)

        return f"No tools matching '{query}'. Call discover_tools() with no query to see all."
