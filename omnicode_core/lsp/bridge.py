"""
LSP Bridge — subprocess-based Language Server Protocol client.

Spawns a language server (pyright, tsserver, gopls, etc.) as a child process
and communicates via JSON-RPC over stdio.  Provides high-level methods:

    goto_definition(file, line, col)
    find_references(file, line, col)
    hover(file, line, col)
    document_symbols(file)
    workspace_symbols(query)
    get_diagnostics(file)

The bridge is lazy — it only starts the language server on first use.
If the required server binary is not installed, methods return graceful
errors instead of crashing.

Supported servers (auto-detected by language):
    Python:     pyright-langserver (pip install pyright)
    TypeScript: typescript-language-server (npm i -g typescript-language-server)
    Go:         gopls
    Rust:       rust-analyzer
    C/C++:      clangd

Usage:
    bridge = LSPBridge(working_dir="/path/to/project")
    result = await bridge.goto_definition("main.py", 23, 4)
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Language → server command mapping
LSP_SERVERS: Dict[str, Dict[str, Any]] = {
    "python": {
        "command": ["pyright-langserver", "--stdio"],
        "install_hint": "pip install pyright",
        "extensions": [".py"],
    },
    "typescript": {
        "command": ["typescript-language-server", "--stdio"],
        "install_hint": "npm i -g typescript-language-server typescript",
        "extensions": [".ts", ".tsx", ".js", ".jsx"],
    },
    "go": {
        "command": ["gopls", "serve"],
        "install_hint": "go install golang.org/x/tools/gopls@latest",
        "extensions": [".go"],
    },
    "rust": {
        "command": ["rust-analyzer"],
        "install_hint": "rustup component add rust-analyzer",
        "extensions": [".rs"],
    },
    "cpp": {
        "command": ["clangd"],
        "install_hint": "apt install clangd / brew install llvm",
        "extensions": [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"],
    },
}


@dataclass
class LSPLocation:
    """A location in a source file."""
    file: str
    line: int  # 0-indexed
    col: int   # 0-indexed
    end_line: Optional[int] = None
    end_col: Optional[int] = None


@dataclass
class LSPSymbol:
    """A symbol in a document."""
    name: str
    kind: str
    line: int
    end_line: int
    container: Optional[str] = None


@dataclass
class LSPHoverInfo:
    """Hover information for a position."""
    content: str
    language: Optional[str] = None


@dataclass
class LSPDiagnostic:
    """A diagnostic (error/warning) from the language server."""
    message: str
    severity: str  # "error" | "warning" | "info" | "hint"
    line: int
    col: int
    source: Optional[str] = None
    code: Optional[str] = None


class LSPBridge:
    """High-level LSP client that manages language server subprocesses.

    One bridge instance per working directory.  Servers are started lazily
    on first request for a given language.
    """

    def __init__(self, working_dir: str):
        self.working_dir = os.path.abspath(working_dir)
        self._servers: Dict[str, "_LSPConnection"] = {}
        self._msg_id = 0

    def _detect_language(self, file_path: str) -> Optional[str]:
        """Detect language from file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        for lang, info in LSP_SERVERS.items():
            if ext in info["extensions"]:
                return lang
        return None

    def _is_available(self, language: str) -> bool:
        """Check if the language server binary is installed."""
        info = LSP_SERVERS.get(language)
        if not info:
            return False
        cmd = info["command"][0]
        return shutil.which(cmd) is not None

    async def _get_server(self, language: str) -> Optional["_LSPConnection"]:
        """Get or start a language server for the given language."""
        if language in self._servers:
            conn = self._servers[language]
            if conn.is_alive():
                return conn
            # Dead server — remove and restart
            del self._servers[language]

        if not self._is_available(language):
            return None

        info = LSP_SERVERS[language]
        try:
            conn = _LSPConnection(info["command"], self.working_dir)
            await conn.start()
            self._servers[language] = conn
            return conn
        except Exception as e:
            logger.warning(f"Failed to start LSP server for {language}: {e}")
            return None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def goto_definition(
        self, file_path: str, line: int, col: int
    ) -> Dict[str, Any]:
        """Find the definition of the symbol at the given position.

        Returns: {"locations": [{"file": ..., "line": ..., "col": ...}]}
        """
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            hint = LSP_SERVERS[language]["install_hint"]
            return {"error": f"LSP server not available for {language}. Install: {hint}"}

        uri = self._file_uri(file_path)
        result = await server.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

        return self._parse_locations(result)

    async def find_references(
        self, file_path: str, line: int, col: int, include_declaration: bool = True
    ) -> Dict[str, Any]:
        """Find all references to the symbol at the given position."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            hint = LSP_SERVERS[language]["install_hint"]
            return {"error": f"LSP server not available for {language}. Install: {hint}"}

        uri = self._file_uri(file_path)
        result = await server.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_declaration},
        })

        return self._parse_locations(result)

    async def hover(self, file_path: str, line: int, col: int) -> Dict[str, Any]:
        """Get hover information (type, documentation) at a position."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            hint = LSP_SERVERS[language]["install_hint"]
            return {"error": f"LSP server not available for {language}. Install: {hint}"}

        uri = self._file_uri(file_path)
        result = await server.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

        if not result:
            return {"content": "", "language": None}

        contents = result.get("contents", "")
        if isinstance(contents, dict):
            return {
                "content": contents.get("value", ""),
                "language": contents.get("language"),
            }
        elif isinstance(contents, list):
            parts = []
            for c in contents:
                if isinstance(c, dict):
                    parts.append(c.get("value", ""))
                else:
                    parts.append(str(c))
            return {"content": "\n".join(parts), "language": None}
        return {"content": str(contents), "language": None}

    async def document_symbols(self, file_path: str) -> Dict[str, Any]:
        """Get all symbols in a document."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            hint = LSP_SERVERS[language]["install_hint"]
            return {"error": f"LSP server not available for {language}. Install: {hint}"}

        uri = self._file_uri(file_path)
        result = await server.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

        return {"symbols": self._parse_symbols(result or [])}

    async def workspace_symbols(self, query: str) -> Dict[str, Any]:
        """Search for symbols across the workspace."""
        # Try each running server
        all_symbols = []
        for lang, conn in self._servers.items():
            if conn.is_alive():
                try:
                    result = await conn.request("workspace/symbol", {"query": query})
                    all_symbols.extend(self._parse_symbols(result or []))
                except Exception:
                    pass

        return {"symbols": all_symbols, "total": len(all_symbols)}

    async def get_diagnostics(self, file_path: str) -> Dict[str, Any]:
        """Get diagnostics for a file.

        Note: Most LSP servers push diagnostics asynchronously via
        notifications.  This method opens the file and waits briefly
        for diagnostics to arrive.
        """
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            hint = LSP_SERVERS[language]["install_hint"]
            return {"error": f"LSP server not available for {language}. Install: {hint}"}

        uri = self._file_uri(file_path)

        # Open the document to trigger diagnostics
        full_path = os.path.join(self.working_dir, file_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            return {"error": f"Cannot read file: {e}"}

        await server.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language,
                "version": 1,
                "text": text,
            },
        })

        # Wait for diagnostics (servers push them asynchronously)
        await asyncio.sleep(2.0)
        diags = server.get_diagnostics(uri)

        return {"diagnostics": diags, "file": file_path, "count": len(diags)}

    async def get_status(self) -> Dict[str, Any]:
        """Get status of all LSP servers."""
        status = {}
        for lang, info in LSP_SERVERS.items():
            available = self._is_available(lang)
            running = lang in self._servers and self._servers[lang].is_alive()
            status[lang] = {
                "available": available,
                "running": running,
                "command": info["command"][0],
                "install_hint": info["install_hint"],
            }
        return status

    async def shutdown(self):
        """Shutdown all running language servers."""
        for lang, conn in list(self._servers.items()):
            try:
                await conn.shutdown()
            except Exception:
                pass
        self._servers.clear()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _file_uri(self, file_path: str) -> str:
        """Convert a relative file path to a file:// URI."""
        if os.path.isabs(file_path):
            abs_path = file_path
        else:
            abs_path = os.path.join(self.working_dir, file_path)
        # Windows: file:///C:/path/to/file
        abs_path = abs_path.replace("\\", "/")
        if not abs_path.startswith("/"):
            abs_path = "/" + abs_path
        return f"file://{abs_path}"

    def _parse_locations(self, result: Any) -> Dict[str, Any]:
        """Parse LSP Location/LocationLink responses."""
        if not result:
            return {"locations": []}

        if isinstance(result, dict):
            result = [result]

        locations = []
        for item in result:
            if "targetUri" in item:
                # LocationLink
                uri = item["targetUri"]
                range_ = item.get("targetRange", item.get("targetSelectionRange", {}))
            elif "uri" in item:
                # Location
                uri = item["uri"]
                range_ = item.get("range", {})
            else:
                continue

            file = self._uri_to_path(uri)
            start = range_.get("start", {})
            end = range_.get("end", {})
            locations.append({
                "file": file,
                "line": start.get("line", 0),
                "col": start.get("character", 0),
                "end_line": end.get("line", 0),
                "end_col": end.get("character", 0),
            })

        return {"locations": locations, "count": len(locations)}

    def _parse_symbols(self, result: Any) -> List[Dict[str, Any]]:
        """Parse DocumentSymbol or SymbolInformation responses."""
        if not result:
            return []

        symbols = []
        SYMBOL_KINDS = {
            1: "file", 2: "module", 3: "namespace", 4: "package",
            5: "class", 6: "method", 7: "property", 8: "field",
            9: "constructor", 10: "enum", 11: "interface", 12: "function",
            13: "variable", 14: "constant", 15: "string", 16: "number",
            17: "boolean", 18: "array", 19: "object", 20: "key",
            21: "null", 22: "enum_member", 23: "struct", 24: "event",
            25: "operator", 26: "type_parameter",
        }

        def _walk(items, container=None):
            for item in items:
                kind_num = item.get("kind", 0)
                kind = SYMBOL_KINDS.get(kind_num, f"kind_{kind_num}")

                if "range" in item:
                    # DocumentSymbol
                    range_ = item["range"]
                    symbols.append({
                        "name": item.get("name", "?"),
                        "kind": kind,
                        "line": range_.get("start", {}).get("line", 0),
                        "end_line": range_.get("end", {}).get("line", 0),
                        "container": container,
                    })
                    # Recurse into children
                    if "children" in item:
                        _walk(item["children"], item.get("name"))
                elif "location" in item:
                    # SymbolInformation
                    loc = item["location"]
                    range_ = loc.get("range", {})
                    symbols.append({
                        "name": item.get("name", "?"),
                        "kind": kind,
                        "line": range_.get("start", {}).get("line", 0),
                        "end_line": range_.get("end", {}).get("line", 0),
                        "container": item.get("containerName"),
                        "file": self._uri_to_path(loc.get("uri", "")),
                    })

        _walk(result)
        return symbols

    def _uri_to_path(self, uri: str) -> str:
        """Convert file:// URI to relative path."""
        if uri.startswith("file://"):
            path = uri[7:]
            # Windows: file:///C:/... → C:/...
            if len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            path = path.replace("/", os.sep)
            # Make relative to working dir
            try:
                return os.path.relpath(path, self.working_dir)
            except ValueError:
                return path
        return uri


class _LSPConnection:
    """Low-level JSON-RPC connection to a language server subprocess."""

    def __init__(self, command: List[str], working_dir: str):
        self.command = command
        self.working_dir = working_dir
        self.process: Optional[asyncio.subprocess.Process] = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._diagnostics: Dict[str, List[Dict]] = {}
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the language server subprocess."""
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize
        await self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file:///{self.working_dir.replace(os.sep, '/')}",
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {
                    "symbol": {"dynamicRegistration": False},
                },
            },
        })
        await self.notify("initialized", {})

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def request(self, method: str, params: Dict) -> Any:
        """Send a request and wait for the response."""
        self._msg_id += 1
        msg_id = self._msg_id

        message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        await self._send(message)

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"LSP request {method} timed out")

    async def notify(self, method: str, params: Dict):
        """Send a notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send(message)

    def get_diagnostics(self, uri: str) -> List[Dict]:
        """Get cached diagnostics for a URI."""
        return self._diagnostics.get(uri, [])

    async def shutdown(self):
        """Gracefully shutdown the server."""
        try:
            await self.request("shutdown", {})
            await self.notify("exit", {})
        except Exception:
            pass
        if self.process:
            self.process.kill()
            await self.process.wait()
        if self._reader_task:
            self._reader_task.cancel()

    async def _send(self, message: Dict):
        """Send a JSON-RPC message."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("LSP server not running")

        body = json.dumps(message)
        header = f"Content-Length: {len(body.encode())}\r\n\r\n"
        self.process.stdin.write(header.encode() + body.encode())
        await self.process.stdin.drain()

    async def _read_loop(self):
        """Read responses from the server."""
        try:
            while self.process and self.process.returncode is None:
                # Read header
                header = b""
                while True:
                    line = await self.process.stdout.readline()
                    if not line:
                        return
                    header += line
                    if header.endswith(b"\r\n\r\n"):
                        break

                # Parse content length
                content_length = 0
                for h in header.decode().split("\r\n"):
                    if h.lower().startswith("content-length:"):
                        content_length = int(h.split(":")[1].strip())

                if content_length == 0:
                    continue

                # Read body
                body = await self.process.stdout.readexactly(content_length)
                message = json.loads(body)

                # Dispatch
                if "id" in message and "method" not in message:
                    # Response
                    msg_id = message["id"]
                    future = self._pending.pop(msg_id, None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(message["error"].get("message", "LSP error"))
                            )
                        else:
                            future.set_result(message.get("result"))
                elif "method" in message:
                    # Notification
                    if message["method"] == "textDocument/publishDiagnostics":
                        params = message.get("params", {})
                        uri = params.get("uri", "")
                        diags = []
                        for d in params.get("diagnostics", []):
                            severity_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
                            diags.append({
                                "message": d.get("message", ""),
                                "severity": severity_map.get(d.get("severity", 3), "info"),
                                "line": d.get("range", {}).get("start", {}).get("line", 0),
                                "col": d.get("range", {}).get("start", {}).get("character", 0),
                                "source": d.get("source"),
                                "code": str(d.get("code", "")),
                            })
                        self._diagnostics[uri] = diags

        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            logger.debug(f"LSP read loop error: {e}")
