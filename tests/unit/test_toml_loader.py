"""Unit tests for the TOML configuration loader (Wave 2, W2-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.config.toml_loader import load_toml_config


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_no_file_is_a_no_op(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OMNICODE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    out = load_toml_config(start=tmp_path)
    assert out == {}


def test_basic_mapping_applied(tmp_path: Path, monkeypatch):
    """Section/key entries become uppercase env vars."""
    cfg = _write(
        tmp_path / "omnicode.toml",
        """
        [server]
        mode = "cloud"
        port = 8765

        [workspace]
        read_only = true

        [security]
        allow_apply_patch = false
        api_key = "sk-test"
        """,
    )
    monkeypatch.delenv("OMNICODE_MODE", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)
    monkeypatch.delenv("OMNICODE_READ_ONLY", raising=False)
    monkeypatch.delenv("OMNICODE_ALLOW_APPLY_PATCH", raising=False)
    monkeypatch.delenv("OMNICODE_API_KEY", raising=False)
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))

    applied = load_toml_config()
    import os

    assert os.environ["OMNICODE_MODE"] == "cloud"
    assert os.environ["API_PORT"] == "8765"
    assert os.environ["OMNICODE_READ_ONLY"] == "true"
    assert os.environ["OMNICODE_ALLOW_APPLY_PATCH"] == "false"
    assert os.environ["OMNICODE_API_KEY"] == "sk-test"
    assert applied["OMNICODE_MODE"] == "cloud"


def test_existing_env_wins(tmp_path: Path, monkeypatch):
    """Pre-set env vars are NOT overwritten — explicit env beats TOML."""
    cfg = _write(
        tmp_path / "omnicode.toml",
        """
        [server]
        mode = "cloud"
        """,
    )
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))
    monkeypatch.setenv("OMNICODE_MODE", "local")  # user-set already

    load_toml_config()
    import os

    assert os.environ["OMNICODE_MODE"] == "local"


def test_passthrough_env_block(tmp_path: Path, monkeypatch):
    cfg = _write(
        tmp_path / "omnicode.toml",
        """
        [env]
        FOO_BAR = "baz"
        TRANSFORMERS_OFFLINE = "1"
        """,
    )
    monkeypatch.delenv("FOO_BAR", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))

    load_toml_config()
    import os

    assert os.environ["FOO_BAR"] == "baz"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_malformed_toml_does_not_raise(tmp_path: Path, monkeypatch):
    """A bad config must not block startup."""
    cfg = _write(tmp_path / "omnicode.toml", "this is not valid TOML [")
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))
    out = load_toml_config()
    assert out == {}


def test_explicit_path_via_env(tmp_path: Path, monkeypatch):
    cfg = _write(
        tmp_path / "alt.toml",
        """
        [server]
        port = 9999
        """,
    )
    monkeypatch.delenv("API_PORT", raising=False)
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))
    load_toml_config()
    import os

    assert os.environ["API_PORT"] == "9999"


def test_relative_to_start_dir(tmp_path: Path, monkeypatch):
    """When ``start`` is supplied and contains a TOML file, it wins."""
    sub = tmp_path / "project"
    sub.mkdir()
    _write(
        sub / "omnicode.toml",
        """
        [features]
        memory = false
        """,
    )
    monkeypatch.delenv("OMNICODE_CONFIG", raising=False)
    monkeypatch.delenv("OMNICODE_MEMORY", raising=False)
    load_toml_config(start=sub)
    import os

    assert os.environ["OMNICODE_MEMORY"] == "false"


def test_boolean_serialised_as_lowercase(tmp_path: Path, monkeypatch):
    cfg = _write(
        tmp_path / "omnicode.toml",
        """
        [features]
        web_console = true
        lsp = false
        """,
    )
    monkeypatch.delenv("OMNICODE_WEB_CONSOLE", raising=False)
    monkeypatch.delenv("OMNICODE_LSP", raising=False)
    monkeypatch.setenv("OMNICODE_CONFIG", str(cfg))
    load_toml_config()
    import os

    assert os.environ["OMNICODE_WEB_CONSOLE"] == "true"
    assert os.environ["OMNICODE_LSP"] == "false"


@pytest.fixture(autouse=True)
def _clean_known_env(monkeypatch):
    """Each test starts with a clean slate for the keys the loader writes."""
    keys = [
        "OMNICODE_MODE", "API_HOST", "API_PORT", "OMNICODE_MCP_REQUIRE_AUTH",
        "WORKING_DIR", "OMNICODE_READ_ONLY", "OMNICODE_WEB_CONSOLE",
        "OMNICODE_MCP_HTTP", "OMNICODE_LLM_ROUTER", "OMNICODE_LSP",
        "OMNICODE_MEMORY", "OMNICODE_SAFE_EDIT", "OMNICODE_INDEX_INCREMENTAL",
        "OMNICODE_EMBEDDING_DEVICE", "EMBEDDING_MODEL",
        "OMNICODE_REQUIRE_API_KEY", "OMNICODE_ALLOW_APPLY_PATCH",
        "OMNICODE_ALLOW_SHELL", "OMNICODE_API_KEY", "OMNICODE_MCP_TOOLS",
        "FOO_BAR", "TRANSFORMERS_OFFLINE",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
