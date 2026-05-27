"""Unit tests for master-key rotation (Wave 2 W2-4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Skip the whole file when cryptography isn't installed (the rest of
# the codebase already pins it, but tests should be portable).
cryptography = pytest.importorskip("cryptography")

from omnicode.llm.provider_registry import ProviderConfig, ProviderRegistry
from omnicode.llm.secret_box import ENCRYPTED_PREFIX, SecretBox
from omnicode_core.auth.rotation import rotate_master_key


def _seed_registry(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "providers.db"
    key = tmp_path / "providers.key"
    box = SecretBox(key)
    reg = ProviderRegistry(db, secret_box=box)
    reg.upsert(
        ProviderConfig(name="alpha", model="m", api_key="sk-alpha-0001")
    )
    reg.upsert(
        ProviderConfig(name="beta", model="m", api_key="sk-beta-9999")
    )
    return db, key


def test_rotation_re_encrypts_every_row(tmp_path: Path):
    db, key = _seed_registry(tmp_path)

    # Capture pre-rotation ciphertexts.
    with sqlite3.connect(str(db)) as conn:
        before = dict(conn.execute("SELECT name, api_key FROM providers").fetchall())
    assert all(v.startswith(ENCRYPTED_PREFIX) for v in before.values())

    report = rotate_master_key(db_path=db, key_path=key)
    assert report.rows_re_encrypted == 2
    assert Path(report.backup_key_path).is_file()
    assert Path(report.backup_key_path).stat().st_size > 0

    # Post-rotation ciphertexts should differ but still be encrypted
    # AND must decrypt to the original plain text under the new key.
    with sqlite3.connect(str(db)) as conn:
        after = dict(conn.execute("SELECT name, api_key FROM providers").fetchall())
    assert before != after
    assert all(v.startswith(ENCRYPTED_PREFIX) for v in after.values())

    new_box = SecretBox(key)
    assert new_box.decrypt(after["alpha"]) == "sk-alpha-0001"
    assert new_box.decrypt(after["beta"]) == "sk-beta-9999"


def test_rotation_creates_a_backup_key_file(tmp_path: Path):
    db, key = _seed_registry(tmp_path)
    original_bytes = key.read_bytes()
    report = rotate_master_key(db_path=db, key_path=key)
    backup = Path(report.backup_key_path)
    assert backup.is_file()
    assert backup.read_bytes().strip() == original_bytes.strip()


def test_rotation_with_explicit_new_key(tmp_path: Path):
    from cryptography.fernet import Fernet

    db, key = _seed_registry(tmp_path)
    custom_key = Fernet.generate_key()
    rotate_master_key(db_path=db, key_path=key, new_key_bytes=custom_key)
    assert key.read_bytes().strip() == custom_key.strip()


def test_rotation_aborts_on_invalid_new_key(tmp_path: Path):
    db, key = _seed_registry(tmp_path)
    original_bytes = key.read_bytes()

    with pytest.raises(RuntimeError):
        rotate_master_key(
            db_path=db,
            key_path=key,
            new_key_bytes=b"this is not a valid fernet key",
        )

    # Original key file restored from backup.
    assert key.read_bytes() == original_bytes


def test_rotation_idempotent_when_run_twice(tmp_path: Path):
    db, key = _seed_registry(tmp_path)
    rotate_master_key(db_path=db, key_path=key)
    # Second pass should still work and end with everything decryptable
    # via the (now twice-rotated) key.
    rotate_master_key(db_path=db, key_path=key)
    box = SecretBox(key)
    with sqlite3.connect(str(db)) as conn:
        rows = dict(conn.execute("SELECT name, api_key FROM providers").fetchall())
    assert box.decrypt(rows["alpha"]) == "sk-alpha-0001"
    assert box.decrypt(rows["beta"]) == "sk-beta-9999"


def test_rotation_fails_when_db_missing(tmp_path: Path):
    key = tmp_path / "providers.key"
    SecretBox(key)  # creates the key
    with pytest.raises(FileNotFoundError):
        rotate_master_key(db_path=tmp_path / "missing.db", key_path=key)


def test_rotation_fails_when_key_missing(tmp_path: Path):
    db, _ = _seed_registry(tmp_path)
    other = tmp_path / "no-such.key"
    with pytest.raises(FileNotFoundError):
        rotate_master_key(db_path=db, key_path=other)
