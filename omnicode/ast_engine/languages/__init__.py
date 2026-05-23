"""
Language-specific symbol extractors.

Each module exposes:
* ``get_language()`` – returns the tree-sitter Language object (or None).
* ``extract_symbols(tree, source)`` – returns a list of symbol dicts.
* ``extract_imports(tree, source)`` – returns a list of import dicts.
* ``extract_calls(tree, source)`` – returns a list of (caller, callee, line) tuples.
"""

from .go import (
    extract_calls as extract_go_calls,
)
from .go import (
    extract_imports as extract_go_imports,
)
from .go import (
    extract_symbols as extract_go_symbols,
)
from .go import (
    get_language as get_go_language,
)
from .java import (
    extract_calls as extract_java_calls,
)
from .java import (
    extract_imports as extract_java_imports,
)
from .java import (
    extract_symbols as extract_java_symbols,
)
from .java import (
    get_language as get_java_language,
)
from .rust import (
    extract_calls as extract_rust_calls,
)
from .rust import (
    extract_imports as extract_rust_imports,
)
from .rust import (
    extract_symbols as extract_rust_symbols,
)
from .rust import (
    get_language as get_rust_language,
)

__all__ = [
    "get_java_language",
    "extract_java_symbols",
    "extract_java_imports",
    "extract_java_calls",
    "get_go_language",
    "extract_go_symbols",
    "extract_go_imports",
    "extract_go_calls",
    "get_rust_language",
    "extract_rust_symbols",
    "extract_rust_imports",
    "extract_rust_calls",
]
