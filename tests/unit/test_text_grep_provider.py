from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from omnicode_core.search import text_grep


def test_grep_workspace_prefers_ripgrep_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "a.py").write_text("class BaseHandler:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(text_grep.shutil, "which", lambda name: "rg" if name == "rg" else None)

    def fake_run(*_args, **_kwargs):
        event = {
            "type": "match",
            "data": {
                "path": {"text": "pkg/a.py"},
                "lines": {"text": "class BaseHandler:\n"},
                "line_number": 1,
                "submatches": [{"start": 0, "end": 18, "match": {"text": "class BaseHandler:"}}],
            },
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.setattr(text_grep.subprocess, "run", fake_run)

    result = text_grep.grep_workspace_with_provider(
        tmp_path,
        "class BaseHandler:",
        file_patterns=["*.py"],
        max_results=5,
        case_sensitive=True,
    )

    assert result.provider == "ripgrep_fallback"
    assert result.provider_chain == ["ripgrep_fallback"]
    assert result.rg_available is True
    assert result.fallback_used is False
    assert result.hits[0].file_path == "pkg/a.py"
    assert result.hits[0].line_number == 1


def test_ripgrep_provider_command_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "a.py").write_text("needle\n", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(text_grep.shutil, "which", lambda name: "rg" if name == "rg" else None)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        event = {
            "type": "match",
            "data": {
                "path": {"text": "pkg/a.py"},
                "lines": {"text": "needle\n"},
                "line_number": 1,
                "submatches": [{"start": 0, "end": 6}],
            },
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.setattr(text_grep.subprocess, "run", fake_run)

    result = text_grep.grep_workspace_with_provider(
        tmp_path,
        "needle",
        file_patterns=["*.py"],
        max_results=2,
        timeout_seconds=0.25,
        max_file_bytes=32,
    )

    cmd = captured["cmd"]
    assert result.provider == "ripgrep_fallback"
    assert captured["cwd"] == tmp_path
    assert captured["timeout"] == 0.25
    assert "--json" in cmd
    assert "--fixed-strings" in cmd
    assert "--max-filesize" in cmd
    assert "32" in cmd
    assert "--glob" in cmd
    assert "*.py" in cmd
    assert "--no-ignore" not in cmd


def test_grep_workspace_falls_back_to_python_when_rg_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "a.py").write_text("class ReplicaManager:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(text_grep.shutil, "which", lambda _name: None)

    result = text_grep.grep_workspace_with_provider(
        tmp_path,
        "class ReplicaManager:",
        file_patterns=["*.py"],
        max_results=5,
        case_sensitive=True,
    )

    assert result.provider == "python_grep_fallback"
    assert result.provider_chain == ["ripgrep_fallback", "python_grep_fallback"]
    assert result.fallback_used is True
    assert result.fallback_reason == "ripgrep_not_found"
    assert result.hits[0].file_path == "pkg/a.py"


def test_grep_workspace_falls_back_to_python_on_rg_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "a.py").write_text("timeout-marker\n", encoding="utf-8")

    monkeypatch.setattr(text_grep.shutil, "which", lambda name: "rg" if name == "rg" else None)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="rg", timeout=0.1)

    monkeypatch.setattr(text_grep.subprocess, "run", fake_run)

    result = text_grep.grep_workspace_with_provider(
        tmp_path,
        "timeout-marker",
        file_patterns=["*.py"],
        max_results=5,
        timeout_seconds=0.1,
    )

    assert result.provider == "python_grep_fallback"
    assert result.fallback_reason == "ripgrep_timeout"
    assert result.timed_out is True
    assert result.hits[0].file_path == "pkg/a.py"


def test_python_fallback_respects_max_file_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "big.py").write_text(
        "x = '" + ("a" * 128) + "needle'\n",
        encoding="utf-8",
    )
    (source / "small.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(text_grep.shutil, "which", lambda _name: None)

    result = text_grep.grep_workspace_with_provider(
        tmp_path,
        "needle",
        file_patterns=["*.py"],
        max_results=10,
        max_file_bytes=32,
    )

    assert result.provider == "python_grep_fallback"
    assert result.max_file_bytes == 32
    assert [hit.file_path for hit in result.hits] == ["pkg/small.py"]
