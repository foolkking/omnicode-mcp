"""
LSP Bridge API — goto definition, find references, hover, symbols, diagnostics.

These endpoints proxy to language servers (pyright, tsserver, gopls, etc.)
via the LSP bridge.  If the required server is not installed, endpoints
return a helpful error with install instructions.
"""


from fastapi import APIRouter, Query

from core.config import get_settings
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/lsp", tags=["lsp"])

# Singleton bridge instance (lazy-initialized)
_bridge = None


def _get_bridge():
    global _bridge
    if _bridge is None:
        from omnicode_core.lsp.bridge import LSPBridge
        settings = get_settings()
        _bridge = LSPBridge(settings.WORKING_DIR)
    return _bridge


@router.get("/status")
async def lsp_status():
    """Get status of all supported language servers."""
    bridge = _get_bridge()
    status = await bridge.get_status()
    return create_success_response(status)


@router.post("/definition")
async def goto_definition(
    file: str = Query(..., description="File path (relative)"),
    line: int = Query(..., description="Line number (0-indexed)"),
    col: int = Query(0, description="Column (0-indexed)"),
):
    """Find the definition of the symbol at the given position."""
    bridge = _get_bridge()
    result = await bridge.goto_definition(file, line, col)
    if "error" in result:
        return create_error_response(result["error"], 404)
    return create_success_response(result)


@router.post("/references")
async def find_references(
    file: str = Query(..., description="File path (relative)"),
    line: int = Query(..., description="Line number (0-indexed)"),
    col: int = Query(0, description="Column (0-indexed)"),
    include_declaration: bool = Query(True),
):
    """Find all references to the symbol at the given position."""
    bridge = _get_bridge()
    result = await bridge.find_references(file, line, col, include_declaration)
    if "error" in result:
        return create_error_response(result["error"], 404)
    return create_success_response(result)


@router.post("/hover")
async def hover_info(
    file: str = Query(..., description="File path (relative)"),
    line: int = Query(..., description="Line number (0-indexed)"),
    col: int = Query(0, description="Column (0-indexed)"),
):
    """Get hover information (type, documentation) at a position."""
    bridge = _get_bridge()
    result = await bridge.hover(file, line, col)
    if "error" in result:
        return create_error_response(result["error"], 404)
    return create_success_response(result)


@router.get("/symbols/{file_path:path}")
async def document_symbols(file_path: str):
    """Get all symbols in a document via LSP."""
    bridge = _get_bridge()
    result = await bridge.document_symbols(file_path)
    if "error" in result:
        return create_error_response(result["error"], 404)
    return create_success_response(result)


@router.get("/workspace-symbols")
async def workspace_symbols(
    query: str = Query(..., description="Symbol name to search"),
):
    """Search for symbols across the entire workspace via LSP."""
    bridge = _get_bridge()
    result = await bridge.workspace_symbols(query)
    return create_success_response(result)


@router.post("/rename")
async def lsp_rename(
    file: str = Query(..., description="File path (relative)"),
    line: int = Query(..., description="Line number (0-indexed)"),
    col: int = Query(..., description="Column (0-indexed)"),
    new_name: str = Query(..., description="New symbol name"),
):
    """Rename the symbol at ``(line, col)`` across the workspace.

    Returns a structured WorkspaceEdit. The server does **not** write
    to disk — callers should review the edits and feed them through
    ``/patch/preview`` + ``/patch/apply`` to keep the snapshot /
    rollback story intact.
    """
    bridge = _get_bridge()
    result = await bridge.rename_symbol(file, line, col, new_name)
    if "error" in result:
        return create_error_response(result["error"], 400)
    return create_success_response(result)


@router.get("/diagnostics/{file_path:path}")
async def get_diagnostics(file_path: str):
    """Get LSP diagnostics for a file.

    Note: opens the file in the language server and waits ~2s for
    diagnostics to arrive.  First call for a file may be slow.
    """
    bridge = _get_bridge()
    result = await bridge.get_diagnostics(file_path)
    if "error" in result:
        return create_error_response(result["error"], 500)
    return create_success_response(result)
