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
import hashlib
import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from omnicode_core.lsp.shadow import LSPShadowWorkspace

logger = logging.getLogger(__name__)

_BRIDGES: Dict[str, "LSPBridge"] = {}


class LSPTimeout(TimeoutError):
    """Structured timeout error from the LSP bridge.

    Carries enough context (method name, configured timeout, elapsed
    time before giving up) for the API layer to render a useful error
    envelope rather than the generic ``"request timed out"`` blob the
    bridge used to emit. Covers the audit's "Better error envelopes
    from the LSP bridge" 1.1 polish item.
    """

    def __init__(self, *, method: str, timeout: float, elapsed: float) -> None:
        self.method = method
        self.timeout = timeout
        self.elapsed = elapsed
        super().__init__(
            f"LSP request '{method}' timed out after "
            f"{elapsed:.1f}s (limit {timeout:.1f}s). "
            "The language server may be cold-starting on a large "
            "project, or stuck on an indexing pass. Retry once; if "
            "it persists, check the LSP server's own logs."
        )

    def to_envelope(self) -> Dict[str, Any]:
        """Serializable shape for /lsp/* error responses."""
        return {
            "error": "lsp_timeout",
            "method": self.method,
            "timeout_seconds": self.timeout,
            "elapsed_seconds": round(self.elapsed, 2),
            "message": str(self),
            "hint": (
                "Increase OMNICODE_LSP_REQUEST_TIMEOUT (seconds) if your "
                "project is large, or check the language server is "
                "actually responding (some servers stall on first index)."
            ),
        }


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
    # ----- W2-7 fleet expansion ------------------------------------------------
    # Each new language follows the same lazy-spawn rules as the existing 5:
    # the server is only started on the first request that touches its
    # extension list. Heavy JVM-based servers (jdtls, kotlin-language-server)
    # don't add startup cost when the project doesn't include those files.
    "ruby": {
        "command": ["solargraph", "stdio"],
        "install_hint": "gem install solargraph",
        "extensions": [".rb"],
    },
    "php": {
        "command": ["intelephense", "--stdio"],
        "install_hint": "npm i -g intelephense",
        "extensions": [".php"],
    },
    "java": {
        # Eclipse JDT LS — distributed as a `jdtls` wrapper script on
        # PATH (homebrew, apt) or via the official Eclipse downloads.
        "command": ["jdtls"],
        "install_hint": "brew install jdtls / npm i -g jdtls",
        "extensions": [".java"],
    },
    "kotlin": {
        "command": ["kotlin-language-server"],
        "install_hint": "brew install kotlin-language-server / "
        "https://github.com/fwcd/kotlin-language-server/releases",
        "extensions": [".kt", ".kts"],
    },
    "csharp": {
        # OmniSharp ships a `OmniSharp` script when installed via dotnet
        # tool; the new official Roslyn LS is `Microsoft.CodeAnalysis.LanguageServer`.
        # We try OmniSharp first because it has a well-known stdio mode.
        "command": ["omnisharp", "-lsp"],
        "install_hint": "dotnet tool install -g omnisharp / "
        "or use the Roslyn LS via dotnet workload",
        "extensions": [".cs"],
    },
    # ----- P2 fleet expansion (round 2) ----------------------------------
    "swift": {
        # SourceKit-LSP ships with the Swift toolchain on macOS / Linux.
        # No extra install once `swift` is on PATH.
        "command": ["sourcekit-lsp"],
        "install_hint": "Install Swift toolchain via brew install swift "
        "or https://swift.org/download",
        "extensions": [".swift"],
    },
    "scala": {
        # Metals — packaged via coursier or sbt on most setups.
        "command": ["metals"],
        "install_hint": "brew install coursier && coursier install metals "
        "(see https://scalameta.org/metals/docs/editors/overview)",
        "extensions": [".scala", ".sbt", ".sc"],
    },
    "haskell": {
        # Haskell Language Server — `haskell-language-server-wrapper`
        # is the recommended entry point (autoselects the matching LSP
        # binary for the local GHC version).
        "command": ["haskell-language-server-wrapper", "--lsp"],
        "install_hint": "brew install haskell-language-server "
        "(or https://haskell-language-server.readthedocs.io)",
        "extensions": [".hs", ".lhs"],
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

    def __init__(self, working_dir: str, state_dir: Optional[str] = None):
        self.working_dir = os.path.abspath(working_dir)
        workspace_key = hashlib.sha1(
            self.working_dir.lower().encode("utf-8", "replace")
        ).hexdigest()[:16]
        base_state = Path(
            state_dir
            or os.environ.get("OMNICODE_STATE_DIR")
            or (Path.home() / ".omnicode")
        ).resolve()
        self.state_dir = base_state / "lsp" / workspace_key
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._jvm_shadow = LSPShadowWorkspace(
            self.working_dir,
            self.state_dir / "jvm-shadow",
        )
        # Compatibility aliases for existing tests/plugins that inspected the
        # Scala-specific attribute before Java joined the same safe workspace.
        self._scala_shadow = self._jvm_shadow
        self._java_shadow = self._jvm_shadow
        self._servers: Dict[str, "_LSPConnection"] = {}
        self._binary_cache: Dict[str, tuple[float, Optional[str]]] = {}
        self._msg_id = 0

    def _detect_language(self, file_path: str) -> Optional[str]:
        """Detect language from file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        for lang, info in LSP_SERVERS.items():
            if ext in info["extensions"]:
                return lang
        return None

    def _resolve_lsp_binary(self, cmd: str) -> Optional[str]:
        """Locate an LSP server executable.

        Probe order:
          1. ``shutil.which`` (system PATH).
          2. The ``Scripts`` directory next to the running Python — this
             lets us pick up tools installed via ``pip install pyright``
             into a conda env even when the env isn't activated.
          3. On Windows we also try ``cmd + ".cmd"`` / ``".exe"``.
        """
        cached = self._binary_cache.get(cmd)
        now = time.monotonic()
        if cached and now - cached[0] <= 5.0:
            return cached[1]
        if cmd.lower() in {"java", "java.exe"}:
            java_home = (
                os.environ.get("OMNICODE_JDTLS_JAVA_HOME")
                or os.environ.get("OMNICODE_JVM_JAVA_HOME")
                or os.environ.get("JAVA_HOME")
            )
            if java_home:
                candidate = Path(java_home) / "bin" / "java.exe"
                if candidate.is_file():
                    resolved = str(candidate.resolve())
                    self._binary_cache[cmd] = (now, resolved)
                    return resolved
        found = shutil.which(cmd)
        if found:
            self._binary_cache[cmd] = (now, found)
            return found
        # Conda / venv fallback: <prefix>/Scripts (Windows) or <prefix>/bin.
        import sys
        py_dir = os.path.dirname(sys.executable)
        candidates = [
            os.path.join(py_dir, cmd),
            os.path.join(py_dir, "Scripts", cmd),
            os.path.join(py_dir, "..", "Scripts", cmd),
            os.path.join(py_dir, "bin", cmd),
        ]
        for ext in ("", ".exe", ".cmd", ".bat"):
            for c in candidates:
                full = c + ext
                if os.path.isfile(full):
                    self._binary_cache[cmd] = (now, full)
                    return full
        self._binary_cache[cmd] = (now, None)
        return None

    def _is_available(self, language: str) -> bool:
        """Check if the language server binary is installed."""
        info = LSP_SERVERS.get(language)
        if not info:
            return False
        if self._language_server_disabled(language):
            return False
        cmd = self._configured_server_command(language)[0]
        return self._resolve_lsp_binary(cmd) is not None

    @staticmethod
    def _language_server_disabled(language: str) -> bool:
        env_names = [f"OMNICODE_LSP_{language.upper()}_DISABLED"]
        if language == "java":
            env_names.append("OMNICODE_JDTLS_DISABLED")
        elif language == "scala":
            env_names.append("OMNICODE_METALS_DISABLED")
        return any(
            os.environ.get(name, "").strip().lower()
            in {"1", "true", "yes", "on"}
            for name in env_names
        )

    def _configured_server_command(self, language: str) -> List[str]:
        info = LSP_SERVERS[language]
        env_names = [
            f"OMNICODE_LSP_{language.upper()}_COMMAND",
        ]
        if language == "java":
            env_names.insert(0, "OMNICODE_JDTLS_COMMAND")
        elif language == "scala":
            env_names.insert(0, "OMNICODE_METALS_COMMAND")
        for env_name in env_names:
            raw = os.environ.get(env_name, "").strip()
            if not raw:
                continue
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list) and parsed:
                        return [str(item) for item in parsed]
                except Exception:
                    pass
            parts = [
                item.strip().strip('"')
                for item in shlex.split(raw, posix=False)
                if item.strip()
            ]
            if parts:
                return parts

        toolchain_bin = self.state_dir.parent.parent / "toolchains" / "bin"
        names = [info["command"][0]]
        for suffix in (".cmd", ".bat", ".exe", ""):
            candidate = toolchain_bin / f"{names[0]}{suffix}"
            if candidate.is_file():
                return [str(candidate), *info["command"][1:]]
        if language == "java":
            extension_roots = [
                Path.home() / ".vscode" / "extensions",
                Path.home() / ".kiro" / "extensions",
                Path.home() / ".cursor" / "extensions",
            ]
            candidates: list[Path] = []
            for root in extension_roots:
                if not root.is_dir():
                    continue
                candidates.extend(
                    root.glob("redhat.java-*/server/bin/jdtls")
                )
            if candidates:
                selected = sorted(
                    candidates,
                    key=lambda path: path.as_posix().lower(),
                    reverse=True,
                )[0]
                server_root = selected.parent.parent
                launchers = sorted(
                    (server_root / "plugins").glob(
                        "org.eclipse.equinox.launcher_*.jar"
                    )
                )
                config_dir = server_root / "config_win"
                if launchers and config_dir.is_dir():
                    return [
                        "java",
                        "-Declipse.application=org.eclipse.jdt.ls.core.id1",
                        "-Dosgi.bundles.defaultStartLevel=4",
                        "-Declipse.product=org.eclipse.jdt.ls.core.product",
                        "-Dosgi.checkConfiguration=true",
                        (
                            "-Dosgi.sharedConfiguration.area="
                            f"{config_dir}"
                        ),
                        "-Dosgi.sharedConfiguration.area.readOnly=true",
                        "-Dosgi.configuration.cascaded=true",
                        "-Xms1G",
                        "--add-modules=ALL-SYSTEM",
                        "--add-opens",
                        "java.base/java.util=ALL-UNNAMED",
                        "--add-opens",
                        "java.base/java.lang=ALL-UNNAMED",
                        "-jar",
                        str(launchers[-1]),
                    ]
        return list(info["command"])

    def _start_policy(self, language: str) -> tuple[bool, str]:
        if self._language_server_disabled(language):
            return False, f"{language}_lsp_disabled"
        if not self._is_available(language):
            return False, f"{language}_lsp_unavailable"
        if language in {"java", "scala"}:
            shadow_disabled = (
                os.environ.get(
                    "OMNICODE_LSP_SHADOW_WORKSPACE",
                    "true",
                ).strip().lower()
                in {"0", "false", "no", "off"}
            )
            if shadow_disabled:
                return False, f"{language}_shadow_workspace_disabled"
        return True, "ready"

    def _server_working_dir(self, language: str) -> str:
        if language in {"java", "scala"}:
            return str(self._jvm_shadow.workspace_root)
        return self.working_dir

    def _sync_language_workspace(
        self,
        language: str,
        file_path: Optional[str] = None,
    ) -> dict[str, Any]:
        if language not in {"java", "scala"}:
            return {"ready": True, "workspace_root": self.working_dir}
        if file_path:
            self._jvm_shadow.sync_file(file_path)
            status = self._jvm_shadow.status()
            if not status.get("ready"):
                status = self._jvm_shadow.sync_full()
            return status
        return self._jvm_shadow.sync_full()

    def _server_command(self, language: str) -> List[str]:
        resolved_cmd = self._configured_server_command(language)
        resolved_first = self._resolve_lsp_binary(resolved_cmd[0]) or resolved_cmd[0]
        resolved_cmd[0] = resolved_first
        if language == "java":
            data_dir = self.state_dir / "jdtls-data"
            data_dir.mkdir(parents=True, exist_ok=True)
            if "-data" not in resolved_cmd:
                resolved_cmd.extend(["-data", str(data_dir)])
        return resolved_cmd

    def _server_environment(self, language: str) -> Dict[str, str]:
        env: Dict[str, str] = {}
        java_home = (
            os.environ.get(f"OMNICODE_{language.upper()}_JAVA_HOME")
            or (
                os.environ.get("OMNICODE_JDTLS_JAVA_HOME")
                if language == "java"
                else os.environ.get("OMNICODE_METALS_JAVA_HOME")
                if language == "scala"
                else None
            )
            or os.environ.get("OMNICODE_JVM_JAVA_HOME")
        )
        if java_home and language in {"java", "scala"}:
            resolved = str(Path(java_home).expanduser().resolve())
            env["JAVA_HOME"] = resolved
            env["PATH"] = os.pathsep.join([
                str(Path(resolved) / "bin"),
                os.environ.get("PATH", ""),
            ])
        return env

    def _unavailable_error(self, language: str) -> Dict[str, Any]:
        allowed, reason = self._start_policy(language)
        hint = LSP_SERVERS[language]["install_hint"]
        return {
            "error": (
                f"LSP server not available for {language}: {reason}. "
                f"Install/configure: {hint}"
            ),
            "error_code": reason,
            "start_allowed": allowed,
        }

    def status_snapshot(
        self,
        languages: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        status: Dict[str, Any] = {}
        for lang, info in LSP_SERVERS.items():
            if languages is not None and lang not in languages:
                continue
            installed = self._is_available(lang)
            start_allowed, reason = self._start_policy(lang)
            conn = self._servers.get(lang)
            running = bool(conn and conn.is_alive())
            initialized = bool(conn and conn.initialized)
            status[lang] = {
                "available": installed,
                "start_allowed": start_allowed,
                "running": running,
                "initialized": initialized,
                "command": self._configured_server_command(lang)[0],
                "reason": (
                    "ready"
                    if initialized
                    else "initializing"
                    if running
                    else reason
                ),
                "state_dir": str(self.state_dir / lang),
            }
            if lang in {"java", "scala"}:
                status[lang]["shadow_workspace"] = (
                    self._jvm_shadow.status()
                )
        return status

    async def _get_server(self, language: str) -> Optional["_LSPConnection"]:
        """Get or start a language server for the given language."""
        if language in self._servers:
            conn = self._servers[language]
            if conn.is_alive():
                return conn
            # Dead server — remove and restart
            del self._servers[language]

        start_allowed, _reason = self._start_policy(language)
        if not start_allowed:
            return None

        resolved_cmd = self._server_command(language)
        try:
            self._sync_language_workspace(language)
            conn = _LSPConnection(
                resolved_cmd,
                self._server_working_dir(language),
                state_dir=str(self.state_dir / language),
                env_overrides=self._server_environment(language),
            )
            await conn.start()
            if language in {"java", "scala"}:
                try:
                    settle_seconds = float(
                        os.environ.get(
                            "OMNICODE_LSP_WORKSPACE_SETTLE_SECONDS",
                            "3",
                        )
                    )
                except ValueError:
                    settle_seconds = 3.0
                if settle_seconds > 0:
                    await asyncio.sleep(min(settle_seconds, 30.0))
            conn.initialized = True
            self._servers[language] = conn
            return conn
        except Exception as e:
            logger.warning(f"Failed to start LSP server for {language}: {e}")
            return None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def bootstrap(
        self,
        languages: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Materialize state-dir workspaces and start selected servers."""

        selected = languages or {"java", "scala"}
        results: Dict[str, Any] = {}
        for language in sorted(selected):
            if language not in LSP_SERVERS:
                results[language] = {
                    "ready": False,
                    "error_code": "unsupported_language",
                }
                continue
            start_allowed, reason = self._start_policy(language)
            if not start_allowed:
                results[language] = {
                    "ready": False,
                    "error_code": reason,
                    "status": self.status_snapshot({language}).get(language),
                }
                continue
            try:
                shadow = (
                    self._sync_language_workspace(language)
                    if language in {"java", "scala"}
                    else None
                )
                server = await self._get_server(language)
                results[language] = {
                    "ready": bool(server and server.is_alive()),
                    "status": self.status_snapshot({language}).get(language),
                    "shadow_workspace": shadow,
                }
            except Exception as exc:
                results[language] = {
                    "ready": False,
                    "error_code": "lsp_bootstrap_failed",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
        return {
            "ready": bool(results)
            and all(bool(item.get("ready")) for item in results.values()),
            "languages": results,
            "state_dir": str(self.state_dir),
        }

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
            return self._unavailable_error(language)

        await self._ensure_open(server, file_path, language)
        uri = self._file_uri(file_path, root=server.working_dir)
        result = await server.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

        return self._parse_locations(result)

    async def _ensure_open(self, server: "_LSPConnection", file_path: str, language: str) -> None:
        """Send ``textDocument/didOpen`` for ``file_path`` if not already open.

        Pyright (and many LSP servers) treat a file as unknown until the
        client explicitly opens it. Without this notification ``definition``,
        ``references`` and ``hover`` requests resolve nothing for files
        the user hasn't visited interactively.
        """
        self._sync_language_workspace(language, file_path)
        uri = self._file_uri(file_path, root=server.working_dir)
        if uri in getattr(server, "_opened_uris", set()):
            return
        full_path = os.path.join(self.working_dir, file_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return
        await server.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language,
                "version": 1,
                "text": text,
            },
        })
        if not hasattr(server, "_opened_uris"):
            server._opened_uris = set()  # type: ignore[attr-defined]
        server._opened_uris.add(uri)  # type: ignore[attr-defined]
        # Give the server a brief moment to ingest the text.
        await asyncio.sleep(0.3)

    async def find_references(
        self, file_path: str, line: int, col: int, include_declaration: bool = True
    ) -> Dict[str, Any]:
        """Find all references to the symbol at the given position."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            return self._unavailable_error(language)

        await self._ensure_open(server, file_path, language)

        uri = self._file_uri(file_path, root=server.working_dir)
        result = await server.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_declaration},
        })

        return self._parse_locations(result)

    async def call_hierarchy(
        self,
        file_path: str,
        line: int,
        col: int,
    ) -> Dict[str, Any]:
        """Return normalized incoming/outgoing call hierarchy for a symbol."""

        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}
        server = await self._get_server(language)
        if not server:
            return self._unavailable_error(language)
        await self._ensure_open(server, file_path, language)
        uri = self._file_uri(file_path, root=server.working_dir)
        prepared = await server.request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": col},
            },
        )
        items = prepared if isinstance(prepared, list) else (
            [prepared] if isinstance(prepared, dict) else []
        )
        if not items:
            return {
                "incoming": [],
                "outgoing": [],
                "prepared": False,
            }
        item = items[0]
        incoming_raw, outgoing_raw = await asyncio.gather(
            server.request("callHierarchy/incomingCalls", {"item": item}),
            server.request("callHierarchy/outgoingCalls", {"item": item}),
        )

        def _normalize(rows: Any, key: str) -> List[Dict[str, Any]]:
            normalized: List[Dict[str, Any]] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                target = row.get(key)
                if not isinstance(target, dict):
                    continue
                target_uri = str(target.get("uri") or "")
                selection = (
                    target.get("selectionRange")
                    if isinstance(target.get("selectionRange"), dict)
                    else target.get("range")
                    if isinstance(target.get("range"), dict)
                    else {}
                )
                start = (
                    selection.get("start")
                    if isinstance(selection, dict)
                    and isinstance(selection.get("start"), dict)
                    else {}
                )
                normalized.append({
                    "name": str(target.get("name") or ""),
                    "kind": target.get("kind"),
                    "file": self._uri_to_path(target_uri),
                    "line": int(start.get("line") or 0),
                    "col": int(start.get("character") or 0),
                    "detail": str(target.get("detail") or ""),
                })
            return normalized

        return {
            "incoming": _normalize(incoming_raw, "from"),
            "outgoing": _normalize(outgoing_raw, "to"),
            "prepared": True,
            "item": {
                "name": str(item.get("name") or ""),
                "detail": str(item.get("detail") or ""),
            },
        }

    async def hover(self, file_path: str, line: int, col: int) -> Dict[str, Any]:
        """Get hover information (type, documentation) at a position."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            return self._unavailable_error(language)

        await self._ensure_open(server, file_path, language)
        uri = self._file_uri(file_path, root=server.working_dir)
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

    async def rename_symbol(
        self, file_path: str, line: int, col: int, new_name: str
    ) -> Dict[str, Any]:
        """Rename the symbol at ``(line, col)`` to ``new_name`` via LSP.

        Returns a normalised ``WorkspaceEdit`` with one entry per touched
        file: ``{file_path: [{start_line, start_col, end_line, end_col,
        new_text}, ...]}``. Callers (the REST router or
        :class:`PatchManager`) can apply the changes themselves rather
        than letting the language server write to disk — keeps the
        existing snapshot/rollback story intact.
        """
        if not new_name or not new_name.strip():
            return {"error": "new_name is required and must be non-empty."}

        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            return self._unavailable_error(language)

        await self._ensure_open(server, file_path, language)
        uri = self._file_uri(file_path, root=server.working_dir)
        result = await server.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": col},
                "newName": new_name.strip(),
            },
        )

        if not result:
            return {
                "edits": {},
                "files_touched": 0,
                "note": "Language server returned no rename edits.",
            }

        # The server can answer with either ``changes`` (legacy) or
        # ``documentChanges`` (modern). Normalise both into a flat dict
        # keyed by file path.
        edits: Dict[str, List[Dict[str, Any]]] = {}

        def _add(target_uri: str, text_edits: List[Dict[str, Any]]) -> None:
            # ``file:///c%3A/...`` on Windows — strip the scheme and
            # percent-decode just enough to get a usable path. We don't
            # try to re-encode for cross-platform symlinks; this is
            # best-effort and the REST router validates afterwards.
            path = self._uri_to_path(target_uri)
            if path.startswith("/") and len(path) > 3 and path[2] == ":":
                # ``/c:/Users/...`` on Windows — drop leading slash.
                path = path[1:]
            normalised: List[Dict[str, Any]] = []
            for te in text_edits:
                rng = te.get("range", {})
                start = rng.get("start", {})
                end = rng.get("end", {})
                normalised.append(
                    {
                        "start_line": start.get("line", 0),
                        "start_col": start.get("character", 0),
                        "end_line": end.get("line", 0),
                        "end_col": end.get("character", 0),
                        "new_text": te.get("newText", ""),
                    }
                )
            if normalised:
                edits.setdefault(path, []).extend(normalised)

        if "changes" in result:
            for target_uri, text_edits in (result.get("changes") or {}).items():
                _add(target_uri, text_edits or [])

        for doc_change in result.get("documentChanges") or []:
            target_uri = (
                doc_change.get("textDocument", {}).get("uri")
                or doc_change.get("uri", "")
            )
            text_edits = doc_change.get("edits") or []
            if target_uri:
                _add(target_uri, text_edits)

        return {
            "edits": edits,
            "files_touched": len(edits),
            "new_name": new_name.strip(),
        }

    async def document_symbols(self, file_path: str) -> Dict[str, Any]:
        """Get all symbols in a document."""
        language = self._detect_language(file_path)
        if not language:
            return {"error": f"Unsupported language for {file_path}"}

        server = await self._get_server(language)
        if not server:
            return self._unavailable_error(language)

        await self._ensure_open(server, file_path, language)
        uri = self._file_uri(file_path, root=server.working_dir)
        result = await server.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

        return {"symbols": self._parse_symbols(result or [])}

    async def workspace_symbols(self, query: str) -> Dict[str, Any]:
        """Search for symbols across the workspace."""
        # Try each running server
        all_symbols = []
        for _lang, conn in self._servers.items():
            if conn.is_alive():
                try:
                    result = await conn.request("workspace/symbol", {"query": query})
                    all_symbols.extend(self._parse_symbols(result or []))
                except Exception:
                    pass

        return {"symbols": all_symbols, "total": len(all_symbols)}

    async def get_diagnostics(
        self,
        file_path: str,
        *,
        content: Optional[str] = None,
        restore_after: bool = False,
    ) -> Dict[str, Any]:
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
            return self._unavailable_error(language)

        self._sync_language_workspace(language, file_path)
        uri = self._file_uri(file_path, root=server.working_dir)

        # Open/update the document to trigger diagnostics. Callers such as
        # patch validation may provide an in-memory overlay so candidate
        # content is checked without touching the authoritative workspace.
        full_path = os.path.join(self.working_dir, file_path)
        disk_text: Optional[str] = None
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                disk_text = f.read()
        except Exception as e:
            if content is None:
                return {"error": f"Cannot read file: {e}"}
        text = content if content is not None else (disk_text or "")

        version = server.next_document_version(uri)
        server.clear_diagnostics(uri)
        if server.is_document_open(uri):
            await server.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
        else:
            await server.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": language,
                    "version": version,
                    "text": text,
                },
            })
            server.mark_document_open(uri)

        if language == "java":
            try:
                await server.request(
                    "workspace/executeCommand",
                    {
                        "command": "java.project.refreshDiagnostics",
                        "arguments": [uri],
                    },
                    timeout=5.0,
                )
            except Exception:
                pass

        # Wait for diagnostics (servers push them asynchronously). JVM
        # servers finish build import after initialize, so use a bounded poll
        # instead of a fixed two-second sleep.
        try:
            diagnostics_timeout = float(
                os.environ.get(
                    "OMNICODE_LSP_DIAGNOSTICS_TIMEOUT",
                    "8" if language in {"java", "scala"} else "2",
                )
            )
        except ValueError:
            diagnostics_timeout = 8.0 if language in {"java", "scala"} else 2.0
        deadline = time.monotonic() + max(diagnostics_timeout, 0.0)
        diags: List[Dict[str, Any]] = []
        while True:
            diags = list(server.get_diagnostics(uri))
            if diags or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)

        if (
            restore_after
            and content is not None
            and disk_text is not None
            and disk_text != content
        ):
            restore_version = server.next_document_version(uri)
            await server.notify("textDocument/didChange", {
                "textDocument": {
                    "uri": uri,
                    "version": restore_version,
                },
                "contentChanges": [{"text": disk_text}],
            })

        return {
            "diagnostics": diags,
            "file": file_path,
            "count": len(diags),
            "overlay": content is not None,
            "restored": bool(
                restore_after
                and content is not None
                and disk_text is not None
                and disk_text != content
            ),
        }

    async def get_status(self) -> Dict[str, Any]:
        """Get status of all LSP servers."""
        status = self.status_snapshot()
        for lang, info in LSP_SERVERS.items():
            status[lang]["install_hint"] = info["install_hint"]
        return status

    async def shutdown(self):
        """Shutdown all running language servers."""
        for _lang, conn in list(self._servers.items()):
            try:
                await conn.shutdown()
            except Exception:
                pass
        self._servers.clear()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _file_uri(
        self,
        file_path: str,
        *,
        root: Optional[str] = None,
    ) -> str:
        """Convert a relative file path to a file:// URI."""
        if os.path.isabs(file_path):
            abs_path = file_path
        else:
            abs_path = os.path.join(root or self.working_dir, file_path)
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
            for root in (
                self.working_dir,
                str(self._jvm_shadow.workspace_root),
            ):
                try:
                    relative = os.path.relpath(path, root)
                except ValueError:
                    continue
                if (
                    relative != ".."
                    and not relative.startswith(f"..{os.sep}")
                ):
                    return relative
            return path
        return uri


def get_lsp_bridge(
    working_dir: str,
    *,
    state_dir: Optional[str] = None,
) -> LSPBridge:
    """Return the process-local bridge for one authoritative workspace."""

    resolved_state_dir = os.path.abspath(
        state_dir
        or os.environ.get("OMNICODE_STATE_DIR")
        or str(Path.home() / ".omnicode")
    ).lower()
    key = f"{os.path.abspath(working_dir).lower()}::{resolved_state_dir}"
    bridge = _BRIDGES.get(key)
    if bridge is None:
        bridge = LSPBridge(working_dir, state_dir=state_dir)
        _BRIDGES[key] = bridge
    return bridge


def lsp_runtime_status(
    working_dir: str,
    *,
    languages: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Return a non-starting runtime snapshot for capability reporting."""

    return get_lsp_bridge(working_dir).status_snapshot(languages=languages)


class _LSPConnection:
    """Low-level JSON-RPC connection to a language server subprocess."""

    def __init__(
        self,
        command: List[str],
        working_dir: str,
        *,
        state_dir: Optional[str] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ):
        self.command = command
        self.working_dir = working_dir
        default_state = (
            Path(os.environ.get("OMNICODE_STATE_DIR") or (Path.home() / ".omnicode"))
            / "lsp"
            / "connections"
        )
        self.state_dir = os.path.abspath(
            state_dir or str(default_state)
        )
        self.env_overrides = dict(env_overrides or {})
        self.process: Optional[asyncio.subprocess.Process] = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._diagnostics: Dict[str, List[Dict]] = {}
        self._opened_uris: set[str] = set()
        self._document_versions: Dict[str, int] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: List[str] = []
        self.initialized = False

    def is_document_open(self, uri: str) -> bool:
        return uri in self._opened_uris

    def mark_document_open(self, uri: str) -> None:
        self._opened_uris.add(uri)

    def next_document_version(self, uri: str) -> int:
        version = int(self._document_versions.get(uri, 0)) + 1
        self._document_versions[uri] = version
        return version

    def clear_diagnostics(self, uri: str) -> None:
        self._diagnostics.pop(uri, None)

    async def start(self):
        """Start the language server subprocess."""
        os.makedirs(self.state_dir, exist_ok=True)
        child_env = os.environ.copy()
        child_env.setdefault("XDG_CACHE_HOME", os.path.join(self.state_dir, "cache"))
        child_env.setdefault("COURSIER_CACHE", os.path.join(self.state_dir, "coursier"))
        child_env.setdefault("BLOOP_HOME", os.path.join(self.state_dir, "bloop"))
        child_env.setdefault("METALS_LOG_DIR", os.path.join(self.state_dir, "logs"))
        child_env.update(self.env_overrides)
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            env=child_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Initialize.  Pyright (and most modern servers) require both
        # ``rootUri`` AND ``workspaceFolders``; some refuse to index the
        # workspace if only the legacy ``rootUri`` is provided.
        wd_posix = self.working_dir.replace(os.sep, "/")
        root_uri = f"file:///{wd_posix.lstrip('/')}"
        try:
            await self.request("initialize", {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "rootPath": self.working_dir,
                "workspaceFolders": [
                    {
                        "uri": root_uri,
                        "name": os.path.basename(self.working_dir)
                        or "workspace",
                    },
                ],
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                        "hover": {
                            "contentFormat": ["markdown", "plaintext"]
                        },
                        "documentSymbol": {
                            "dynamicRegistration": False
                        },
                        "publishDiagnostics": {
                            "relatedInformation": True
                        },
                    },
                    "workspace": {
                        "symbol": {"dynamicRegistration": False},
                        "workspaceFolders": True,
                        "configuration": True,
                    },
                },
            })
            await self.notify("initialized", {})
        except Exception:
            await self._force_stop()
            raise

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def request(self, method: str, params: Dict, timeout: Optional[float] = None) -> Any:
        """Send a request and wait for the response.

        Raises :class:`LSPTimeout` on timeout — carries ``method``,
        ``timeout``, ``elapsed`` so callers can render a structured
        envelope.

        ``timeout`` defaults to ``OMNICODE_LSP_REQUEST_TIMEOUT`` env
        var (seconds, decimal) or 30s when unset.
        """
        import time as _time
        if timeout is None:
            try:
                timeout = float(os.environ.get("OMNICODE_LSP_REQUEST_TIMEOUT", "30"))
            except (TypeError, ValueError):
                timeout = 30.0

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
        started = _time.monotonic()

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            elapsed = _time.monotonic() - started
            raise LSPTimeout(
                method=method, timeout=timeout, elapsed=elapsed,
            ) from exc

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
            await self.request("shutdown", {}, timeout=5.0)
            await self.notify("exit", {})
        except Exception:
            pass
        await self._force_stop(wait_for_exit=True)

    async def _force_stop(self, *, wait_for_exit: bool = False) -> None:
        process = self.process
        if process and process.returncode is None:
            if wait_for_exit:
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    pass
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except Exception:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    pass
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process and process.stdin:
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except Exception:
                pass
        self.process = None

    async def _drain_stderr(self) -> None:
        """Drain server stderr so JVM/native servers cannot block on logs."""

        if not self.process or not self.process.stderr:
            return
        try:
            while self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                del self._stderr_tail[:-100]
                logger.debug("LSP stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("LSP stderr drain stopped: %s", exc)

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
                    if "id" in message:
                        method = str(message.get("method") or "")
                        params = message.get("params") or {}
                        if method == "workspace/configuration":
                            items = (
                                params.get("items")
                                if isinstance(params, dict)
                                else []
                            )
                            result = [{} for _item in (items or [])]
                        elif method == "workspace/workspaceFolders":
                            wd_posix = self.working_dir.replace(os.sep, "/")
                            uri = f"file:///{wd_posix.lstrip('/')}"
                            result = [{
                                "uri": uri,
                                "name": os.path.basename(self.working_dir)
                                or "workspace",
                            }]
                        elif method == "workspace/applyEdit":
                            result = {
                                "applied": False,
                                "failureReason": (
                                    "OmniCode applies edits through "
                                    "PatchManager only"
                                ),
                            }
                        else:
                            # Registration, progress creation and optional UI
                            # requests are acknowledged without side effects.
                            result = None
                        await self._send({
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": result,
                        })
                    # Notification
                    elif message["method"] == "textDocument/publishDiagnostics":
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
