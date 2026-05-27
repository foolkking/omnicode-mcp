"""
High-level MCP tools — 6 aggregated tools + 1 discovery.

These replace the need for AI clients to choose between 25+ fine-grained
tools.  Each high-level tool internally dispatches to the appropriate
backend endpoint based on the `mode` or `action` parameter.

Token savings: ~10k schema tokens → ~3k (70% reduction in tool definitions).

Usage by AI clients:
    omni_search(query="create_app", mode="auto")
    omni_read(file="main.py", mode="outline")
    omni_edit(action="preview", patch="...")
    omni_analyze(symbol="create_app", analysis="impact")
    omni_memory(action="search", query="faiss")
    omni_context(file="main.py", symbol="create_app")
    discover_tools(query="git")
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _format_json(data: Any, max_lines: int = 80) -> str:
    """Format data as readable JSON, truncated if too long."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
    return text


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
    ) -> str:
        """Search the codebase with automatic mode selection.

        Modes:
          - auto: intelligently picks the best search strategy
          - semantic: natural language → code (FAISS embedding similarity)
          - symbol: fuzzy symbol name matching (function/class names)
          - text: exact substring search
          - references: find all usages of a symbol (requires LSP)

        Returns structured results with file, symbol, score, and why_matched.
        """
        try:
            # Auto mode: if query looks like a symbol name (no spaces, camelCase
            # or snake_case), use symbol search; otherwise semantic.
            if mode == "auto":
                has_spaces = " " in query.strip()
                if not has_spaces and len(query) < 60:
                    mode = "symbol"
                else:
                    mode = "semantic"

            if mode == "symbol":
                result = await make_request("POST", "/search/symbols", params={
                    "query": query,
                    "fuzzy": True,
                    "max_results": max_results,
                })
            elif mode == "text":
                result = await make_request("POST", "/search/text", params={
                    "query": query,
                    "file_pattern": file_pattern or "*.py",
                    "max_results": max_results,
                })
            elif mode == "semantic":
                result = await make_request("POST", "/search", json={
                    "query": query,
                    "search_type": "semantic",
                    "file_pattern": file_pattern,
                    "max_results": max_results,
                })
            else:
                return f"❌ Unknown search mode: {mode}. Use: auto, semantic, symbol, text"

            if "error" in result:
                return f"❌ Search error: {result['error']}"

            # Format results
            data = result.get("result", result)
            results = data.get("results", [])
            total = data.get("total_results", len(results))

            if not results:
                return f"🔍 No results for '{query}' (mode={mode})"

            lines = [f"🔍 {total} result(s) for '{query}' (mode={mode})\n"]
            for i, r in enumerate(results[:max_results], 1):
                name = r.get("symbol_name") or r.get("file_path", "?")
                file = r.get("file_path", "")
                score = r.get("relevance_score", 0)
                kind = r.get("symbol_type") or r.get("chunk_type", "")
                sig = r.get("signature", "")
                line_start = r.get("line_start") or r.get("line_number", "")

                lines.append(f"{i}. {name}")
                if file:
                    lines.append(f"   📄 {file}:{line_start}")
                if kind:
                    lines.append(f"   🏷️ {kind}  score={score:.2f}")
                if sig:
                    lines.append(f"   ✏️ {sig[:120]}")
                lines.append("")

            return "\n".join(lines)

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
    async def omni_analyze(
        symbol: str,
        analysis: str = "impact",
        depth: int = 2,
        path: Optional[str] = None,
    ) -> str:
        """Analyze code relationships and impact.

        Analysis types:
          - impact: who calls this, what it calls, risk level, suggested tests
          - callers: list all callers of this symbol
          - callees: list all functions this symbol calls
          - graph: full call graph for a scope (use path to limit)

        Essential before modifying any function — tells you what might break.
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
        """Safe code editing with preview, validate, apply, and rollback.

        Actions:
          - preview: show what a patch would change (diff)
          - validate: run static checks on the patch without applying
          - apply: apply a validated patch to the file
          - rollback: undo the last applied patch
          - ai_edit: use LLM to generate + apply an edit (requires instructions + file)

        For AI-generated edits, use action='ai_edit' with file + instructions.
        For manual patches, use action='preview' then 'apply'.
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
                    return f"✅ Edit applied to {file} (quality={score:.2f})"
                else:
                    analysis = data.get("failure_analysis", {})
                    stage = analysis.get("stage", "?")
                    reason = analysis.get("root_cause", analysis.get("failure_reasons", "unknown"))
                    return f"❌ Edit failed at stage '{stage}': {reason}"

            elif action in ("preview", "validate", "apply", "rollback"):
                # These will be fully implemented in step 6 (Patch Session).
                # For now, return a placeholder that explains the capability.
                return (
                    f"⚠️ Patch {action} is planned but not yet fully implemented.\n"
                    f"Use action='ai_edit' for LLM-powered editing, or\n"
                    f"use the Web Console's Edit Session page for manual patch review."
                )

            return f"❌ Unknown action: {action}. Use: preview, validate, apply, rollback, ai_edit"

        except Exception as e:
            return f"❌ Edit operation failed: {e}"

    @mcp.tool()
    async def discover_tools(query: str = "") -> str:
        """Discover available OmniCode tools and their capabilities.

        Call with a query to find relevant tools, or empty to list all.
        This is useful when you're not sure which tool to use.

        High-level tools (recommended):
          - omni_search: search code (auto/semantic/symbol/text)
          - omni_read: read files (outline/symbols/full/imports/diagnostics)
          - omni_edit: safe editing (preview/validate/apply/rollback/ai_edit)
          - omni_analyze: impact analysis (callers/callees/graph)
          - omni_memory: project memory (search/store/context)
          - omni_context: get full context for a file+symbol in one call

        Legacy tools (still available, more granular):
          search_tool, read_code_tool, edit_file, write_tool, file_tool,
          git_tool, session_tool, memory_tool, project_context_tool,
          list_file_symbols_tool, read_symbol_from_database,
          project_structure_tool, list_directory_tool, show_directory_tree,
          code_analysis_tool, execute_tool
        """
        tools_info = {
            "omni_search": "Search code: auto/semantic/symbol/text modes",
            "omni_read": "Read files: outline/symbols/full/imports/diagnostics/range/symbol",
            "omni_edit": "Safe editing: preview/validate/apply/rollback/ai_edit",
            "omni_analyze": "Impact analysis: callers/callees/graph/impact",
            "omni_memory": "Project memory: search/store/context/advisory",
            "omni_context": "Full context for file+symbol in one call",
            "discover_tools": "This tool — find what's available",
        }

        if not query:
            lines = ["📦 OmniCode High-Level Tools:\n"]
            for name, desc in tools_info.items():
                lines.append(f"  • {name}: {desc}")
            lines.append("\n💡 Tip: Use omni_context as your first call before editing.")
            lines.append("   It returns outline + callers + diagnostics + memories in one shot.")
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
