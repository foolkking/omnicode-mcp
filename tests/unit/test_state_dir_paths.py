from __future__ import annotations

from pathlib import Path


def test_provider_db_uses_omnicode_state_dir_even_when_project_db_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from omnicode.config import settings as settings_mod

    workspace = tmp_path / "repo"
    project_data = workspace / ".data"
    project_data.mkdir(parents=True)
    (project_data / "providers.db").write_text("legacy", encoding="utf-8")
    state_dir = tmp_path / "state"

    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("CODEBASE_MCP_USER_DIR", raising=False)
    monkeypatch.delenv("PROVIDER_DB_PATH", raising=False)
    settings_mod.get_settings.cache_clear()
    try:
        resolved = settings_mod.resolve_provider_db_path(str(workspace))
    finally:
        settings_mod.get_settings.cache_clear()

    assert Path(resolved) == state_dir / "providers.db"
    assert Path(resolved).parent.exists()


def test_user_data_dir_uses_state_dir_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from omnicode.config import settings as settings_mod

    state_dir = tmp_path / "state"
    monkeypatch.setenv("OMNICODE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("CODEBASE_MCP_USER_DIR", raising=False)

    assert settings_mod._user_data_dir() == state_dir / "user"
