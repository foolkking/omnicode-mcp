from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class SearchRequest(BaseModel):
    """
    Search request model for API compatibility.
    """
    query: str
    search_type: str = "semantic"  # semantic, text, symbol_exact, fuzzy_symbol
    file_pattern: Optional[str] = None
    symbol_type: Optional[str] = None
    use_regex: bool = False
    case_sensitive: bool = False
    fuzzy: bool = True
    min_score: float = 0.5
    max_results: int = 10
    metadata_filter: Optional[Dict[str, Any]] = None
