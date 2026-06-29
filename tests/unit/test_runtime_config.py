"""RuntimeConfig composition tests for local / hybrid MCP sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicode_core.config.runtime import build_runtime_config


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_runtime_config_reads_workspace_toml_from_any_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write(
        workspace / "omnicode.toml",
        """
        [workspace]
        id = "repo-a"
        root = "."

        [mcp]
        executor = "hybrid"

        [cloud]
        url = "http://cloud:6789"

        [sync]
        mode = "strict"
        debounce_ms = 1500
        """,
    )
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    cfg = build_runtime_config(start=workspace, environ={})

    assert cfg.workspace_root == workspace.resolve()
    assert cfg.workspace_id == "repo-a"
    assert cfg.executor == "hybrid"
    assert cfg.backend_url == "http://cloud:6789"
    assert cfg.sync_mode == "strict"
    assert cfg.debounce_ms == 1500
    assert cfg.batch_max_files == 500
    assert cfg.batch_max_bytes == 8_000_000
    assert cfg.sources["workspace_root"] == "toml"


def test_runtime_config_precedence_cli_env_toml_default(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write(
        workspace / "omnicode.toml",
        """
        [workspace]
        id = "from-toml"

        [mcp]
        executor = "hybrid"

        [sync]
        mode = "strict"
        """,
    )
    env = {
        "OMNICODE_WORKSPACE_ID": "from-env",
        "OMNICODE_EXECUTOR_MODE": "remote",
    }

    cfg = build_runtime_config(
        start=workspace,
        environ=env,
        cli_overrides={
            "workspace_id": "from-cli",
            "sync_mode": "watch",
        },
    )

    assert cfg.workspace_id == "from-cli"
    assert cfg.sources["workspace_id"] == "cli"
    assert cfg.executor == "remote"
    assert cfg.sources["executor"] == "env"
    assert cfg.sync_mode == "watch"
    assert cfg.sources["sync_mode"] == "cli"
    assert cfg.transport == "stdio"
    assert cfg.sources["transport"] == "default"


def test_runtime_config_as_env_contains_existing_mcp_names(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    cfg = build_runtime_config(
        start=workspace,
        environ={},
        cli_overrides={
            "workspace_id": "repo-a",
            "executor": "hybrid",
            "backend_url": "http://cloud:6789/",
            "llm_mode": "off",
            "embedding_mode": "cloud",
        },
    )
    env = cfg.as_env()

    assert env["WORKING_DIR"] == str(workspace.resolve())
    assert env["OMNICODE_WORKSPACE_ROOT"] == str(workspace.resolve())
    assert env["OMNICODE_WORKSPACE_ID"] == "repo-a"
    assert env["OMNICODE_EXECUTOR_MODE"] == "hybrid"
    assert env["OMNICODE_REMOTE"] == "http://cloud:6789"
    assert env["OMNICODE_FASTAPI_BASE_URL"] == "http://cloud:6789"
    assert env["OMNICODE_LLM_MODE"] == "off"
    assert env["OMNICODE_EMBEDDING_MODE"] == "cloud"


def test_runtime_config_rejects_invalid_modes(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(ValueError, match="executor must be one of"):
        build_runtime_config(
            start=workspace,
            environ={},
            cli_overrides={"executor": "sideways"},
        )
