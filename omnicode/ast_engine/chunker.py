import logging
from typing import List, Optional

from pydantic import BaseModel

from .parser import UnifiedASTParser

logger = logging.getLogger(__name__)

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

class ASTChunker:
    """
    Chunks code based on AST structure rather than regex.
    """
    def __init__(self, parser: UnifiedASTParser):
        self.parser = parser

    def chunk_file(self, content: str, file_path: str, language: str) -> List[CodeChunk]:
        tree = self.parser.parse(content, language)
        if not tree:
            logger.warning(f"Falling back to basic chunking for {file_path}")
            return self._basic_chunking(content, file_path)

        chunks = []

        # Simplified example of chunking based on root nodes
        # A full implementation would use Tree-sitter Queries to find functions/classes
        root_node = tree.root_node

        # We always want a file overview chunk
        chunks.append(CodeChunk(
            chunk_id=f"{file_path}:overview",
            file_path=file_path,
            chunk_type="file_overview",
            content=f"Overview of {file_path}", # Would extract imports and signatures
            start_line=1,
            end_line=root_node.end_point[0] + 1
        ))

        # Iterate top-level nodes for basic chunking
        for i, child in enumerate(root_node.children):
            if child.type in ['function_definition', 'class_definition', 'function_declaration', 'method_definition']:
                # Extract the text
                start_byte = child.start_byte
                end_byte = child.end_byte
                chunk_text = content.encode('utf-8')[start_byte:end_byte].decode('utf-8')

                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}:node_{i}",
                    file_path=file_path,
                    chunk_type=child.type,
                    content=chunk_text,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                ))

        return chunks

    def _basic_chunking(self, content: str, file_path: str) -> List[CodeChunk]:
        """Fallback chunker if AST parsing fails"""
        lines = content.splitlines()
        return [CodeChunk(
            chunk_id=f"{file_path}:fallback",
            file_path=file_path,
            chunk_type="fallback",
            content=content[:1000] if len(content) > 1000 else content,
            start_line=1,
            end_line=len(lines)
        )]
