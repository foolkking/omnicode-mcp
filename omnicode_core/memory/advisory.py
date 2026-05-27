"""
Auto Memory Advisory — proactively recall relevant memories before edits.

When a user or AI requests a code modification, this module automatically
searches the memory system for relevant past lessons, mistakes, conventions,
and architecture decisions — without requiring an explicit memory search.

Trigger signals:
  - Current file path
  - Symbol being modified
  - Git diff context
  - Error messages
  - Task description
  - Related dependency names

Output: a concise advisory (300-800 tokens) injected into the edit context.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MemoryAdvisor:
    """Generates automatic memory advisories for code modification tasks."""

    def __init__(self, memory_manager):
        """
        Args:
            memory_manager: An initialized MemoryManager instance.
        """
        self.memory_manager = memory_manager

    async def generate_advisory(
        self,
        file_path: Optional[str] = None,
        symbol: Optional[str] = None,
        task: Optional[str] = None,
        error_message: Optional[str] = None,
        git_diff: Optional[str] = None,
        max_memories: int = 5,
        max_tokens: int = 800,
    ) -> Dict[str, Any]:
        """Generate a concise advisory from relevant memories.

        Searches multiple angles and deduplicates results.

        Returns:
            {
                "advisory": "...",  # Human-readable text (300-800 tokens)
                "memories_used": [...],  # IDs of memories referenced
                "confidence": 0.0-1.0,
                "signals_matched": ["file_path", "symbol", ...]
            }
        """
        from memory_system.models import MemorySearchRequest

        queries = []
        signals_matched = []

        # Build search queries from available signals
        if symbol:
            queries.append(symbol)
            signals_matched.append("symbol")

        if file_path:
            # Search by filename (without full path)
            filename = file_path.replace("\\", "/").split("/")[-1]
            queries.append(filename)
            signals_matched.append("file_path")

        if task:
            queries.append(task)
            signals_matched.append("task")

        if error_message:
            # Extract key terms from error
            error_short = error_message[:100]
            queries.append(error_short)
            signals_matched.append("error")

        if git_diff:
            # Extract function names from diff
            import re
            funcs = re.findall(r"^[+-]\s*(?:def|async def|function|class)\s+(\w+)", git_diff, re.MULTILINE)
            if funcs:
                queries.append(" ".join(funcs[:5]))
                signals_matched.append("git_diff")

        if not queries:
            return {
                "advisory": "",
                "memories_used": [],
                "confidence": 0.0,
                "signals_matched": [],
            }

        # Search memories with each query, collect unique results
        seen_ids = set()
        all_results = []

        for query in queries[:5]:  # Cap at 5 queries
            try:
                request = MemorySearchRequest(
                    query=query,
                    max_results=3,
                    min_score=0.3,
                )
                results = await self.memory_manager.search_memories(request)
                for r in results:
                    mem_id = r.memory.id
                    if mem_id not in seen_ids:
                        seen_ids.add(mem_id)
                        all_results.append(r)
            except Exception as e:
                logger.debug(f"Advisory search failed for '{query}': {e}")

        if not all_results:
            return {
                "advisory": "",
                "memories_used": [],
                "confidence": 0.0,
                "signals_matched": signals_matched,
            }

        # Sort by relevance and take top N
        all_results.sort(key=lambda r: r.relevance_score or 0, reverse=True)
        top = all_results[:max_memories]

        # Format advisory
        lines = ["📝 Relevant past lessons:\n"]
        memories_used = []
        total_chars = 0

        for i, r in enumerate(top, 1):
            mem = r.memory
            content = (mem.content or "").strip()
            category = mem.category.value if hasattr(mem.category, "value") else str(mem.category)

            # Truncate individual memories to fit budget
            if len(content) > 200:
                content = content[:197] + "..."

            line = f"{i}. [{category}] {content}"
            if total_chars + len(line) > max_tokens * 4:  # rough char→token ratio
                break

            lines.append(line)
            memories_used.append(mem.id)
            total_chars += len(line)

        # Confidence based on best score
        best_score = top[0].relevance_score if top else 0.0
        confidence = min(1.0, best_score * 1.2)  # slight boost

        advisory_text = "\n".join(lines)

        return {
            "advisory": advisory_text,
            "memories_used": memories_used,
            "confidence": round(confidence, 3),
            "signals_matched": signals_matched,
            "memory_count": len(memories_used),
        }
