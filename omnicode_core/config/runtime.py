"""Runtime configuration composer for local / hybrid MCP sessions.

This module is intentionally small and dependency-light.  The existing
``toml_loader`` keeps the old "TOML -> env vars" compatibility path; this
composer builds the structured object that the local workspace, sync queue,
and hybrid router can share.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from omnicode_core.config.toml_loader import read_toml_config

_DEFAULT_IGNORE_PATHS = (
    ".git/",
    ".data/",
    "node_modules/",
    ".venv/",
    "dist/",
    "build/",
)

_VALID_EXECUTORS = {"local", "hybrid", "remote", "auto"}
_VALID_SYNC_MODES = {"off", "watch", "smart", "strict"}
_VALID_AGENT_MODES = {"auto", "external", "off"}
_VALID_LLM_MODES = {"off", "local", "remote", "auto"}
_VALID_EMBEDDING_MODES = {"off", "local", "cloud"}
_VALID_DIAGNOSTICS_MODES = {"local-first", "local", "remote", "off"}


@dataclass(frozen=True)
class RuntimeConfig:
    workspace_root: Path
    workspace_id: str
    executor: str = "remote"
    transport: str = "stdio"
    backend_url: Optional[str] = None
    cloud_auth_mode: str = "off"
    cloud_token_env: str = "OMNICODE_API_KEY"
    sync_mode: str = "smart"
    agent_mode: str = "auto"
    debounce_ms: int = 1200
    max_file_bytes: int = 1_000_000
    batch_max_files: int = 500
    batch_max_bytes: int = 8_000_000
    llm_mode: str = "off"
    embedding_mode: str = "cloud"
    diagnostics_mode: str = "local-first"
    ignore_paths: tuple[str, ...] = _DEFAULT_IGNORE_PATHS
    sources: dict[str, str] = field(default_factory=dict)

    def as_env(self) -> dict[str, str]:
        """Return env vars that mirror this runtime config.

        These names are deliberately aligned with the existing MCP server
        process globals, so adding RuntimeConfig does not force a router
        rewrite in the same change.
        """
        env = {
            "WORKING_DIR": str(self.workspace_root),
            "OMNICODE_WORKSPACE_ROOT": str(self.workspace_root),
            "OMNICODE_WORKSPACE_ID": self.workspace_id,
            "OMNICODE_EXECUTOR_MODE": self.executor,
            "OMNICODE_MCP_TRANSPORT": self.transport,
            "OMNICODE_SYNC_MODE": self.sync_mode,
            "OMNICODE_AGENT_MODE": self.agent_mode,
            "OMNICODE_AGENT_DEBOUNCE_MS": str(self.debounce_ms),
            "OMNICODE_SYNC_MAX_FILE_BYTES": str(self.max_file_bytes),
            "OMNICODE_SYNC_BATCH_MAX_FILES": str(self.batch_max_files),
            "OMNICODE_SYNC_BATCH_MAX_BYTES": str(self.batch_max_bytes),
            "OMNICODE_LLM_MODE": self.llm_mode,
            "OMNICODE_EMBEDDING_MODE": self.embedding_mode,
            "OMNICODE_DIAGNOSTICS_MODE": self.diagnostics_mode,
            "OMNICODE_CLOUD_AUTH_MODE": self.cloud_auth_mode,
            "OMNICODE_CLOUD_TOKEN_ENV": self.cloud_token_env,
            "OMNICODE_IGNORE_PATHS": ",".join(self.ignore_paths),
        }
        if self.backend_url:
            env["OMNICODE_REMOTE"] = self.backend_url
            env["OMNICODE_FASTAPI_BASE_URL"] = self.backend_url
        return env


def _toml_get(data: Mapping[str, Any], section: str, key: str) -> Any:
    sect = data.get(section, {})
    if isinstance(sect, Mapping):
        return sect.get(key)
    return None


def _env_get(environ: Mapping[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def _coerce_int(value: Any, *, default: int, field_name: str) -> int:
    if value is None or value == "":
        return default
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if out < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return out


def _coerce_paths(value: Any) -> tuple[str, ...]:
    if value is None:
        return _DEFAULT_IGNORE_PATHS
    if isinstance(value, str):
        return tuple(p.strip() for p in value.split(",") if p.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(p).strip() for p in value if str(p).strip())
    raise ValueError("ignore.paths must be a list of strings")


def _select(
    *,
    key: str,
    default: Any,
    toml_value: Any,
    env_value: Any,
    cli_value: Any,
    sources: dict[str, str],
) -> Any:
    if cli_value is not None:
        sources[key] = "cli"
        return cli_value
    if env_value is not None:
        sources[key] = "env"
        return env_value
    if toml_value is not None:
        sources[key] = "toml"
        return toml_value
    sources[key] = "default"
    return default


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    return cleaned


def build_runtime_config(
    *,
    start: Optional[str | Path] = None,
    cli_overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimeConfig:
    """Build a RuntimeConfig with CLI > env > TOML > defaults precedence."""
    cli = dict(cli_overrides or {})
    env = os.environ if environ is None else environ
    toml = read_toml_config(start=start)
    sources: dict[str, str] = {}

    raw_root = _select(
        key="workspace_root",
        default=start or ".",
        toml_value=_toml_get(toml, "workspace", "root"),
        env_value=_env_get(env, "OMNICODE_WORKSPACE_ROOT", "WORKING_DIR"),
        cli_value=cli.get("workspace") or cli.get("workspace_root"),
        sources=sources,
    )
    workspace_root = Path(str(raw_root)).expanduser()
    if not workspace_root.is_absolute():
        anchor = Path(start).expanduser() if start is not None else Path.cwd()
        workspace_root = anchor / workspace_root
    workspace_root = workspace_root.resolve()

    workspace_id = str(_select(
        key="workspace_id",
        default=workspace_root.name or "workspace",
        toml_value=_toml_get(toml, "workspace", "id"),
        env_value=_env_get(env, "OMNICODE_WORKSPACE_ID"),
        cli_value=cli.get("workspace_id"),
        sources=sources,
    )).strip()
    if not workspace_id:
        raise ValueError("workspace_id cannot be empty")

    executor = _validate_choice(str(_select(
        key="executor",
        default="remote",
        toml_value=_toml_get(toml, "mcp", "executor"),
        env_value=_env_get(env, "OMNICODE_EXECUTOR_MODE"),
        cli_value=cli.get("executor"),
        sources=sources,
    )), _VALID_EXECUTORS, "executor")

    transport = str(_select(
        key="transport",
        default="stdio",
        toml_value=_toml_get(toml, "mcp", "transport"),
        env_value=_env_get(env, "OMNICODE_MCP_TRANSPORT"),
        cli_value=cli.get("transport"),
        sources=sources,
    )).strip() or "stdio"

    backend_url = _select(
        key="backend_url",
        default=None,
        toml_value=_toml_get(toml, "cloud", "url"),
        env_value=_env_get(env, "OMNICODE_FASTAPI_BASE_URL", "OMNICODE_REMOTE"),
        cli_value=cli.get("backend_url"),
        sources=sources,
    )
    backend_url = str(backend_url).rstrip("/") if backend_url else None

    cloud_auth_mode = str(_select(
        key="cloud_auth_mode",
        default="off",
        toml_value=_toml_get(toml, "cloud", "auth_mode"),
        env_value=_env_get(env, "OMNICODE_CLOUD_AUTH_MODE"),
        cli_value=cli.get("cloud_auth_mode"),
        sources=sources,
    )).strip().lower()
    if cloud_auth_mode not in {"off", "token"}:
        raise ValueError("cloud_auth_mode must be one of: off, token")

    cloud_token_env = str(_select(
        key="cloud_token_env",
        default="OMNICODE_API_KEY",
        toml_value=_toml_get(toml, "cloud", "token_env"),
        env_value=_env_get(env, "OMNICODE_CLOUD_TOKEN_ENV"),
        cli_value=cli.get("cloud_token_env"),
        sources=sources,
    )).strip() or "OMNICODE_API_KEY"

    sync_mode = _validate_choice(str(_select(
        key="sync_mode",
        default="smart",
        toml_value=_toml_get(toml, "sync", "mode"),
        env_value=_env_get(env, "OMNICODE_SYNC_MODE"),
        cli_value=cli.get("sync_mode"),
        sources=sources,
    )), _VALID_SYNC_MODES, "sync_mode")

    agent_mode = _validate_choice(str(_select(
        key="agent_mode",
        default="auto",
        toml_value=_toml_get(toml, "sync", "agent"),
        env_value=_env_get(env, "OMNICODE_AGENT_MODE"),
        cli_value=cli.get("agent_mode") or cli.get("agent"),
        sources=sources,
    )), _VALID_AGENT_MODES, "agent_mode")

    debounce_ms = _coerce_int(_select(
        key="debounce_ms",
        default=1200,
        toml_value=_toml_get(toml, "sync", "debounce_ms"),
        env_value=_env_get(env, "OMNICODE_AGENT_DEBOUNCE_MS"),
        cli_value=cli.get("debounce_ms"),
        sources=sources,
    ), default=1200, field_name="debounce_ms")

    max_file_bytes = _coerce_int(_select(
        key="max_file_bytes",
        default=1_000_000,
        toml_value=_toml_get(toml, "sync", "max_file_bytes"),
        env_value=_env_get(env, "OMNICODE_SYNC_MAX_FILE_BYTES"),
        cli_value=cli.get("max_file_bytes"),
        sources=sources,
    ), default=1_000_000, field_name="max_file_bytes")

    batch_max_files = _coerce_int(_select(
        key="batch_max_files",
        default=500,
        toml_value=_toml_get(toml, "sync", "batch_max_files"),
        env_value=_env_get(env, "OMNICODE_SYNC_BATCH_MAX_FILES"),
        cli_value=cli.get("batch_max_files"),
        sources=sources,
    ), default=500, field_name="batch_max_files")

    batch_max_bytes = _coerce_int(_select(
        key="batch_max_bytes",
        default=8_000_000,
        toml_value=_toml_get(toml, "sync", "batch_max_bytes"),
        env_value=_env_get(env, "OMNICODE_SYNC_BATCH_MAX_BYTES"),
        cli_value=cli.get("batch_max_bytes"),
        sources=sources,
    ), default=8_000_000, field_name="batch_max_bytes")

    llm_mode = _validate_choice(str(_select(
        key="llm_mode",
        default="off",
        toml_value=_toml_get(toml, "capabilities", "llm_mode"),
        env_value=_env_get(env, "OMNICODE_LLM_MODE"),
        cli_value=cli.get("llm_mode"),
        sources=sources,
    )), _VALID_LLM_MODES, "llm_mode")

    embedding_mode = _validate_choice(str(_select(
        key="embedding_mode",
        default="cloud",
        toml_value=_toml_get(toml, "capabilities", "embedding_mode"),
        env_value=_env_get(env, "OMNICODE_EMBEDDING_MODE"),
        cli_value=cli.get("embedding_mode"),
        sources=sources,
    )), _VALID_EMBEDDING_MODES, "embedding_mode")

    diagnostics_mode = _validate_choice(str(_select(
        key="diagnostics_mode",
        default="local-first",
        toml_value=_toml_get(toml, "capabilities", "diagnostics_mode"),
        env_value=_env_get(env, "OMNICODE_DIAGNOSTICS_MODE"),
        cli_value=cli.get("diagnostics_mode"),
        sources=sources,
    )), _VALID_DIAGNOSTICS_MODES, "diagnostics_mode")

    ignore_paths = _coerce_paths(_select(
        key="ignore_paths",
        default=_DEFAULT_IGNORE_PATHS,
        toml_value=_toml_get(toml, "ignore", "paths"),
        env_value=_env_get(env, "OMNICODE_IGNORE_PATHS"),
        cli_value=cli.get("ignore_paths"),
        sources=sources,
    ))

    return RuntimeConfig(
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        executor=executor,
        transport=transport,
        backend_url=backend_url,
        cloud_auth_mode=cloud_auth_mode,
        cloud_token_env=cloud_token_env,
        sync_mode=sync_mode,
        agent_mode=agent_mode,
        debounce_ms=debounce_ms,
        max_file_bytes=max_file_bytes,
        batch_max_files=batch_max_files,
        batch_max_bytes=batch_max_bytes,
        llm_mode=llm_mode,
        embedding_mode=embedding_mode,
        diagnostics_mode=diagnostics_mode,
        ignore_paths=ignore_paths,
        sources=sources,
    )


__all__ = ["RuntimeConfig", "build_runtime_config"]
