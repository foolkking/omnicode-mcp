"""Shared test fixtures.

Forces HuggingFace into offline mode and provides a tmp working dir
so unit tests don't need network access or the real .data SQLite stores.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Force offline mode BEFORE any module-level imports that hit the network.
for var in ("TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_OFFLINE"):
    os.environ.setdefault(var, "1")


@pytest.fixture
def tmp_working_dir(tmp_path: Path) -> Path:
    """A clean working directory rooted in pytest's tmp_path."""
    return tmp_path


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Path to a fresh per-test SQLite file."""
    return str(tmp_path / "providers.db")
