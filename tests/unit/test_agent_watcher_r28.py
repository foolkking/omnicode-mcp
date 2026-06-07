"""r28 tests for large-repo initial sync observability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnicode_adapters.agent.client import AgentResult
from omnicode_adapters.agent.watcher import Watcher, _initial_walk


def _write_files(root: Path, count: int) -> None:
    for idx in range(count):
        path = root / f"file_{idx}.py"
        path.write_text(f"VALUE = {idx}\n", encoding="utf-8")


def test_initial_walk_default_has_no_file_count_cap(tmp_path: Path) -> None:
    _write_files(tmp_path, 3)

    walk = _initial_walk(tmp_path)

    assert len(walk.paths) == 3
    assert walk.files_seen == 3
    assert walk.truncated is False
    assert walk.cap is None


def test_initial_walk_cap_is_explicit_and_observable(tmp_path: Path) -> None:
    _write_files(tmp_path, 3)

    walk = _initial_walk(tmp_path, max_files=2)

    assert len(walk.paths) == 2
    assert walk.files_seen == 2
    assert walk.truncated is True
    assert walk.cap == 2


def test_initial_sync_sends_truncation_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_files(tmp_path, 3)
    captured: dict[str, Any] = {}

    class _Client:
        def push_batch(self, paths, *, metadata=None):
            captured["paths"] = list(paths)
            captured["metadata"] = metadata
            return AgentResult(pushed=len(captured["paths"]))

    monkeypatch.setenv("OMNICODE_AGENT_MAX_INITIAL_FILES", "2")
    watcher = Watcher(
        client=_Client(),  # type: ignore[arg-type]
        workspace=tmp_path,
        printer=lambda _msg: None,
    )

    result = watcher.initial_sync()

    assert result.pushed == 2
    assert result.files_seen == 2
    assert result.initial_sync_truncated is True
    assert result.initial_sync_cap == 2
    assert captured["metadata"] == {
        "phase": "initial_sync",
        "files_seen": 2,
        "files_pushed": 2,
        "truncated": True,
        "cap": 2,
    }
