import tree_sitter
from typing import List, Dict, Optional, Any
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

class Symbol(BaseModel):
    name: str
    symbol_type: str
    start_line: int
    end_line: int
    docstring: Optional[str] = None
    signature: Optional[str] = None
    
class Import(BaseModel):
    module: str
    names: List[str]
    start_line: int

class CallGraph(BaseModel):
    caller: str
    callee: str
    line: int

class UnifiedASTParser:
    """
    Unified AST Parser using Tree-sitter.
    Supports multiple languages.
    """
    def __init__(self):
        self.parsers = {}
        self._initialize_parsers()

    def _initialize_parsers(self):
        try:
            import tree_sitter_python
            import tree_sitter_javascript
            import tree_sitter_typescript
            import tree_sitter_cpp
            
            py_parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))
            self.parsers["python"] = py_parser
            
            js_parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_javascript.language()))
            self.parsers["javascript"] = js_parser
            
            ts_parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_typescript.language_typescript()))
            self.parsers["typescript"] = ts_parser
            
            cpp_parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_cpp.language()))
            self.parsers["cpp"] = cpp_parser
            
            
        except ImportError as e:
            logger.warning(f"Failed to load some tree-sitter languages: {e}. Make sure they are installed.")

    def get_parser(self, language: str) -> Optional[tree_sitter.Parser]:
        # Map common extensions to languages if needed, but assuming language string is direct here
        lang_map = {
            "py": "python",
            "python": "python",
            "js": "javascript",
            "javascript": "javascript",
            "ts": "typescript",
            "typescript": "typescript",
            "cpp": "cpp",
            "c++": "cpp"
        }
        mapped_lang = lang_map.get(language.lower(), language.lower())
        return self.parsers.get(mapped_lang)

    def parse(self, code: str, language: str) -> Optional[tree_sitter.Tree]:
        """Parse code into an AST tree."""
        parser = self.get_parser(language)
        if not parser:
            logger.error(f"No parser available for language: {language}")
            return None
            
        if isinstance(code, str):
            code_bytes = code.encode('utf-8')
        else:
            code_bytes = code
            
        return parser.parse(code_bytes)

    # Note: Full implementations for extract_symbols, extract_imports, etc. 
    # would use tree-sitter Queries specific to each language.
    # This is the foundational structure.
