from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.routers import patch as patch_router
from omnicode_core.edit.patch import PatchManager


def test_apply_rejects_when_file_changed_after_preview(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "tmp_conflict.py"
    target.parent.mkdir()
    target.write_text('VALUE = "base"\n', encoding="utf-8")
    manager = PatchManager(str(tmp_path))

    preview = manager.preview_patch(
        "tests/tmp_conflict.py",
        'VALUE = "patch"\n',
    )
    assert preview.success is True

    target.write_text('VALUE = "external"\n', encoding="utf-8")
    applied = manager.apply_patch(
        "tests/tmp_conflict.py",
        'VALUE = "patch"\n',
    )

    assert applied.success is False
    assert "Apply conflict" in applied.message
    assert target.read_text(encoding="utf-8") == 'VALUE = "external"\n'
    assert not list((tmp_path / ".data" / "edit_sessions").glob("*.json"))


def test_repreview_refreshes_conflict_baseline(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "tmp_conflict.py"
    target.parent.mkdir()
    target.write_text('VALUE = "base"\n', encoding="utf-8")
    manager = PatchManager(str(tmp_path))

    manager.preview_patch("tests/tmp_conflict.py", 'VALUE = "patch"\n')
    target.write_text('VALUE = "external"\n', encoding="utf-8")
    manager.preview_patch("tests/tmp_conflict.py", 'VALUE = "patch"\n')
    applied = manager.apply_patch("tests/tmp_conflict.py", 'VALUE = "patch"\n')

    assert applied.success is True
    assert applied.rollback_available is True
    assert target.read_text(encoding="utf-8") == 'VALUE = "patch"\n'


def test_patch_apply_conflict_returns_structured_http_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "tests" / "tmp_conflict.py"
    target.parent.mkdir()
    target.write_text('VALUE = "base"\n', encoding="utf-8")

    monkeypatch.setattr(
        patch_router,
        "get_settings",
        lambda: SimpleNamespace(
            WORKING_DIR=str(tmp_path),
            OMNICODE_READ_ONLY=False,
            OMNICODE_ALLOW_APPLY_PATCH=True,
        ),
    )
    app = FastAPI()
    app.include_router(patch_router.router)
    client = TestClient(app)

    preview = client.post(
        "/patch/preview",
        json={"file_path": "tests/tmp_conflict.py", "content": 'VALUE = "patch"\n'},
    )
    assert preview.status_code == 200

    target.write_text('VALUE = "external"\n', encoding="utf-8")
    response = client.post(
        "/patch/apply",
        json={"file_path": "tests/tmp_conflict.py", "content": 'VALUE = "patch"\n'},
    )

    body = response.json()
    assert response.status_code == 409
    assert body["success"] is False
    assert "Apply conflict" in body["error"]
    assert target.read_text(encoding="utf-8") == 'VALUE = "external"\n'
