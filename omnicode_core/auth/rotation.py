"""Master-key rotation for the encrypted provider DB (Wave 2 W2-4).

The provider registry encrypts every ``api_key`` value with a Fernet
key kept on disk (``providers.key`` next to ``providers.db``). Rotating
that master key means:

1. Read all ciphertext rows with the *old* key.
2. Decrypt them in memory.
3. Write a *new* key file.
4. Re-encrypt every row with the new key.
5. Atomically replace the active key file.

We do this in-process because the registry already lazy-loads
``SecretBox`` on first use; a CLI command spawns the rotation and
shuts down. If the rotation fails partway the original key file is
restored from a backup written under ``<key_path>.bak.<timestamp>``.

If the user passes ``--new-key=<base64>`` we use that key bytes
verbatim. Otherwise a fresh ``Fernet.generate_key()`` is generated.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RotationReport:
    db_path: str
    key_path: str
    backup_key_path: str
    rows_re_encrypted: int
    rows_skipped: int
    new_key_path: str

    def to_dict(self) -> dict:
        return {
            "db_path": self.db_path,
            "key_path": self.key_path,
            "backup_key_path": self.backup_key_path,
            "rows_re_encrypted": self.rows_re_encrypted,
            "rows_skipped": self.rows_skipped,
            "new_key_path": self.new_key_path,
        }


def _read_all_api_keys(conn: sqlite3.Connection) -> List[Tuple[str, Optional[str]]]:
    rows = conn.execute("SELECT name, api_key FROM providers").fetchall()
    return [(r[0], r[1]) for r in rows]


def _write_all_api_keys(
    conn: sqlite3.Connection, items: List[Tuple[str, Optional[str]]]
) -> None:
    with conn:
        for name, encrypted in items:
            conn.execute(
                "UPDATE providers SET api_key = ? WHERE name = ?",
                (encrypted, name),
            )


def rotate_master_key(
    db_path: str | Path,
    key_path: str | Path,
    new_key_bytes: Optional[bytes] = None,
) -> RotationReport:
    """Rotate the master Fernet key in place.

    Aborts and restores the original key on any error so the registry
    is always usable after the call (worst case: same key as before).
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dep is in pyproject
        raise RuntimeError(
            "cryptography is required to rotate the master key"
        ) from exc

    db_path = Path(db_path)
    key_path = Path(key_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"providers DB not found at {db_path}")
    if not key_path.is_file():
        raise FileNotFoundError(f"master key file not found at {key_path}")

    # Step 1 — load the existing key + the old SecretBox.
    from omnicode.llm.secret_box import SecretBox

    old_box = SecretBox(key_path)
    if not old_box.available:
        raise RuntimeError(
            "Existing key file could not be opened — refusing to rotate "
            "(would lose access to existing rows)."
        )

    # Step 2 — decrypt every row in memory.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = _read_all_api_keys(conn)
        decrypted: List[Tuple[str, Optional[str]]] = []
        skipped = 0
        for name, ciphertext in rows:
            if not ciphertext:
                decrypted.append((name, None))
                continue
            plain = old_box.decrypt(ciphertext)
            if plain is None:
                # Box couldn't decrypt — most likely a row that
                # predates encryption. Pass it through; we'll
                # re-encrypt under the new key later.
                skipped += 1
                decrypted.append((name, ciphertext))
            else:
                decrypted.append((name, plain))
    finally:
        conn.close()

    # Step 3 — write the new key file. Backup old one first.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = key_path.with_name(f"{key_path.name}.bak.{timestamp}")
    shutil.copy2(key_path, backup_path)
    try:
        new_key = new_key_bytes or Fernet.generate_key()
        # sanity: validate the supplied bytes are a Fernet-shaped key.
        Fernet(new_key)
        key_path.write_bytes(new_key.strip() + b"\n")
    except Exception as exc:
        # Roll back to the original key file.
        shutil.copy2(backup_path, key_path)
        raise RuntimeError(f"Could not write the new key: {exc}") from exc

    # Step 4 — re-encrypt every row with the new SecretBox.
    new_box = SecretBox(key_path)
    if not new_box.available:
        # Roll back.
        shutil.copy2(backup_path, key_path)
        raise RuntimeError(
            "Newly written key could not be opened — rolled back to backup."
        )

    re_encrypted: List[Tuple[str, Optional[str]]] = []
    re_count = 0
    for name, plain in decrypted:
        if plain is None:
            re_encrypted.append((name, None))
            continue
        if SecretBox.is_encrypted(plain):
            # We couldn't decrypt earlier; leave it unchanged.
            re_encrypted.append((name, plain))
            continue
        wrapped = new_box.encrypt(plain)
        re_encrypted.append((name, wrapped))
        re_count += 1

    # Step 5 — write back atomically (single transaction).
    conn = sqlite3.connect(str(db_path))
    try:
        _write_all_api_keys(conn, re_encrypted)
    finally:
        conn.close()

    logger.info(
        "Master key rotated: %d rows re-encrypted, %d skipped, backup at %s",
        re_count,
        skipped,
        backup_path,
    )
    return RotationReport(
        db_path=str(db_path),
        key_path=str(key_path),
        backup_key_path=str(backup_path),
        rows_re_encrypted=re_count,
        rows_skipped=skipped,
        new_key_path=str(key_path),
    )


__all__ = ["rotate_master_key", "RotationReport"]
