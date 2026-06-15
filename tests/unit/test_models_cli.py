from __future__ import annotations

import json
import sys

import pytest

from omnicode_adapters.cli.commands import models_cmd


def test_models_pull_json_suppresses_third_party_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_pull_model(*_args, **_kwargs):
        print("download progress that would break JSON", file=sys.stderr)
        return {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "dimension": 384,
            "available": True,
        }

    monkeypatch.setattr(models_cmd, "pull_model", fake_pull_model)

    models_cmd.run(
        "pull",
        model="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir="C:/tmp/model-cache",
        json_output=True,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is True
    assert payload["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert payload["diagnostics"] == ["download progress that would break JSON"]


def test_models_pull_json_missing_model_is_structured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        models_cmd.run("pull", json_output=True)

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "MISSING_MODEL"


def test_models_pull_json_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_pull_model(*_args, **_kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(models_cmd, "pull_model", fake_pull_model)

    with pytest.raises(SystemExit) as exc:
        models_cmd.run(
            "pull",
            model="sentence-transformers/all-MiniLM-L6-v2",
            json_output=True,
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "RuntimeError"
    assert payload["error"] == "network unavailable"
