from typing import List, Dict, Any, Optional
import tiktoken
import logging
from .base import BaseLLMProvider
import ast # Just for basic Python comment stripping for MVP

logger = logging.getLogger(__name__)

class TokenManager:
    """
    Manages tokens, strips comments, and truncates context to fit within windows.
    """
    def __init__(self, provider: BaseLLMProvider):
        self.provider = provider
        self.max_context = provider.get_context_window()
        # Keep some buffer for the system prompt and the response
        self.usable_context = int(self.max_context * 0.8)

    def count_tokens(self, text: str) -> int:
        return self.provider.count_tokens(text)

    def strip_comments(self, code: str, language: str) -> str:
        """
        Strip comments from code to save tokens.
        (Simplified implementation for MVP - real one would use tree-sitter)
        """
        if language in ["python", "py"]:
            try:
                # Basic docstring removal using AST unparse
                parsed = ast.parse(code)
                for node in ast.walk(parsed):
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef, ast.Module)):
                        if ast.get_docstring(node):
                            node.body.pop(0) # Remove docstring (simplistic)
                return ast.unparse(parsed)
            except Exception:
                return code # Fallback
        return code

    def compress_context(self, context_items: List[Dict[str, str]], query: str, language: str = "python") -> List[Dict[str, str]]:
        """
        Compress context to fit within the usable context window.
        Prioritizes:
        1. Query (must fit)
        2. High priority context
        3. Stripping comments from lower priority context
        4. Dropping lowest priority context entirely
        """
        query_tokens = self.count_tokens(query)
        if query_tokens > self.usable_context:
            logger.warning("Query alone exceeds usable context!")
            return [] # Can't do much if query is too large

        available_tokens = self.usable_context - query_tokens
        
        compressed_items = []
        
        # Sort items by some priority if available (assuming priority key exists, else 0)
        sorted_items = sorted(context_items, key=lambda x: x.get('priority', 0), reverse=True)
        
        for item in sorted_items:
            content = item.get('content', '')
            tokens = self.count_tokens(content)
            
            if tokens <= available_tokens:
                compressed_items.append(item)
                available_tokens -= tokens
            else:
                # Try stripping comments
                stripped = self.strip_comments(content, language)
                stripped_tokens = self.count_tokens(stripped)
                
                if stripped_tokens <= available_tokens:
                    item['content'] = stripped
                    compressed_items.append(item)
                    available_tokens -= stripped_tokens
                else:
                    logger.debug(f"Context item {item.get('id', 'unknown')} dropped due to token limit.")
                    
        return compressed_items
