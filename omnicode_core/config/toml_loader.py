"""TOML configuration file loader (Wave 2, W2-1).

Loads ``omnicode.toml`` (or the path in ``OMNICODE_CONFIG``) and folds
the values into the process environment **before** Pydantic ``Settings``
is instantiated. This way the existing settings layer doesn't need a
new schema — it just sees env vars like before.

Precedence (highest wins):
    1. CLI flags (e.g. ``omnicode serve --mode cloud``).
    2. Pre-existing process env vars.
    3. TOML file values.
    4. Pydantic defaults.

Section / key mapping (mirrors the prompt in architecture-v2.md §11):

```
[server]
mode = "cloud"            -> OMNICODE_MODE
host = "0.0.0.0"          -> API_HOST
port = 8765               -> API_PORT
auth = true               -> OMNICODE_MCP_REQUIRE_AUTH

[workspace]
root = "/srv/.../proj-a"  -> WORKING_DIR
read_only = false         -> OMNICODE_READ_ONLY

[features]
web_console = true        -> OMNICODE_WEB_CONSOLE
mcp_http = true           -> OMNICODE_MCP_HTTP
llm_router = false        -> OMNICODE_LLM_ROUTER
lsp = true                -> OMNICODE_LSP
memory = true             -> OMNICODE_MEMORY
safe_edit = true          -> OMNICODE_SAFE_EDIT

[index]
incremental = true        -> OMNICODE_INDEX_INCREMENTAL
embedding_device = "cpu"  -> OMNICODE_EMBEDDING_DEVICE
embedding_model = "bge-small-en" -> EMBEDDING_MODEL

[security]
require_api_key = true    -> OMNICODE_API_KEY (must also be set explicitly)
allow_apply_patch = false -> OMNICODE_ALLOW_APPLY_PATCH
allow_shell = false       -> OMNICODE_ALLOW_SHELL
api_key = "sk-..."        -> OMNICODE_API_KEY
```

Unknown keys are kept in a passthrough block ``[env]`` for ad-hoc
overrides:

```
[env]
TRANSFORMERS_OFFLINE = "1"
HF_HUB_OFFLINE = "1"
```
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Map TOML (section, key) → env-var name.
_SECTION_KEY_MAP: Dict[tuple[str, str], str] = {
    ("server", "mode"): "OMNICODE_MODE",
    ("server", "host"): "API_HOST",
    ("server", "port"): "API_PORT",
    ("server", "auth"): "OMNICODE_MCP_REQUIRE_AUTH",
    ("workspace", "root"): "WORKING_DIR",
    ("workspace", "read_only"): "OMNICODE_READ_ONLY",
    ("features", "web_console"): "OMNICODE_WEB_CONSOLE",
    ("features", "mcp_http"): "OMNICODE_MCP_HTTP",
    ("features", "llm_router"): "OMNICODE_LLM_ROUTER",
    ("features", "lsp"): "OMNICODE_LSP",
    ("features", "memory"): "OMNICODE_MEMORY",
    ("features", "safe_edit"): "OMNICODE_SAFE_EDIT",
    ("index", "incremental"): "OMNICODE_INDEX_INCREMENTAL",
    ("index", "embedding_device"): "OMNICODE_EMBEDDING_DEVICE",
    ("index", "embedding_model"): "EMBEDDING_MODEL",
    ("security", "require_api_key"): "OMNICODE_REQUIRE_API_KEY",
    ("security", "allow_apply_patch"): "OMNICODE_ALLOW_APPLY_PATCH",
    ("security", "allow_shell"): "OMNICODE_ALLOW_SHELL",
    ("security", "api_key"): "OMNICODE_API_KEY",
    ("security", "mcp_tools"): "OMNICODE_MCP_TOOLS",
}


def _stringify(value: Any) -> str:
    """Serialize a TOML value to the string env-var convention.

    Booleans → "true"/"false"; everything else → ``str()``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resolve_path(start: Optional[str | Path] = None) -> Optional[Path]:
    """Find the active TOML file, in order:

    1. ``OMNICODE_CONFIG`` env var (absolute or relative to ``cwd``).
    2. ``<start>/omnicode.toml`` if ``start`` is given.
    3. ``<cwd>/omnicode.toml``.

    Returns None when no file is found — TOML is optional.
    """
    explicit = os.environ.get("OMNICODE_CONFIG", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None

    if start is not None:
        p = Path(start) / "omnicode.toml"
        if p.is_file():
            return p

    cwd_file = Path.cwd() / "omnicode.toml"
    return cwd_file if cwd_file.is_file() else None


def load_toml_config(start: Optional[str | Path] = None) -> Dict[str, str]:
    """Read the TOML file and apply env vars (using ``setdefault``).

    Pre-existing env vars are NOT overwritten — that keeps the
    precedence rule "explicit env > TOML" intact.

    Returns a dict of ``{env_name: value}`` showing what the loader
    actually applied, useful for diagnostics. Empty when no file found
    or when ``tomllib`` isn't available (Python < 3.11).
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - codebase pins 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.warning(
                "TOML loader: tomllib/tomli not available; skipping omnicode.toml."
            )
            return {}

    path = _resolve_path(start)
    if path is None:
        logger.debug("TOML loader: no omnicode.toml found.")
        return {}

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        logger.warning("TOML loader: failed to parse %s — %s", path, exc)
        return {}

    applied: Dict[str, str] = {}

    # Mapped section/key entries
    for (section, key), env_name in _SECTION_KEY_MAP.items():
        sect = data.get(section, {})
        if isinstance(sect, dict) and key in sect:
            value = _stringify(sect[key])
            if env_name not in os.environ:
                os.environ[env_name] = value
                applied[env_name] = value

    # [env] passthrough — verbatim env vars for things we don't model.
    raw_env = data.get("env", {})
    if isinstance(raw_env, dict):
        for name, value in raw_env.items():
            if name not in os.environ:
                os.environ[name] = _stringify(value)
                applied[name] = _stringify(value)

    if applied:
        logger.info(
            "TOML loader: applied %d settings from %s.", len(applied), path
        )
    else:
        logger.info(
            "TOML loader: %s parsed but every key was already in env.", path
        )
    return applied


__all__ = ["load_toml_config"]
