"""Shared unit-test isolation fixtures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


for var in ("TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_OFFLINE"):
    os.environ.setdefault(var, "1")


@pytest.fixture(autouse=True)
def _isolate_process_environment():
    """Prevent environment mutations in one test from leaking to the next."""
    baseline = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(baseline)


@pytest.fixture(autouse=True)
def _ensure_event_loop_for_legacy_async_helpers():
    """Keep legacy tests using asyncio.get_event_loop() stable."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture
def tmp_working_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "providers.db")
