"""
Secret box (STAGE 2.13)
=======================
Symmetric-encryption wrapper for sensitive provider configuration.

The provider registry stores ``api_key`` values in SQLite. On a single-
developer laptop that's fine, but anyone glancing at ``.data/providers.db``
can read them. ``SecretBox`` opaquely encrypts those strings using a key
that lives next to the database (file mode 0600 on POSIX) so:

* the registry SQLite blob no longer contains plain-text keys;
* if the database file is shipped in a backup / shared, the keys are
  worthless without the matching ``providers.key``;
* all encryption / decryption stays local — never sent over the network.

Threat model — what this DOES protect against:
* casual reading of ``.data/providers.db`` (e.g. shared with a colleague);
* accidental commit of the database file to git (key file is gitignored);
* dump-style backups that mix sensitive + non-sensitive data.

Threat model — what this does NOT protect against:
* an attacker with full filesystem access (they get key + db);
* memory inspection while the server is running;
* a compromised / malicious LLM provider (out of scope).

For defence-in-depth above this, use OS-level secret managers
(Keychain / DPAPI / libsecret) — that's a future enhancement.
"""

from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Plain-text payloads are wrapped with this prefix when encrypted, so we
# can tell at-rest blobs apart from legacy plain-text values that may
# already exist in older databases.
ENCRYPTED_PREFIX = "ofb1:"   # OmniCode-Fernet-Box version 1


class SecretBox:
    """Encrypts / decrypts strings using a Fernet key on disk.

    The first time you instantiate the box pointed at a fresh location, a
    new 256-bit key is generated and written to disk with mode 0600.
    Subsequent runs reuse the same key.

    The class is import-safe even when ``cryptography`` is not installed:
    in that case the box becomes a no-op (returns plain text untouched and
    logs a warning). This keeps the system working in minimal-deps
    environments while still benefiting users who installed the lib.
    """

    def __init__(self, key_path: str | Path) -> None:
        self.key_path = Path(key_path)
        self._fernet = None
        self._available = self._init_fernet()

    # ------------------------------------------------------------------ init
    def _init_fernet(self) -> bool:
        try:
            from cryptography.fernet import Fernet  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "`cryptography` not installed — SecretBox will pass values through "
                "in plain text. `pip install cryptography` to enable at-rest encryption."
            )
            return False

        key = self._load_or_create_key()
        if key is None:
            return False
        try:
            self._fernet = Fernet(key)
        except Exception as exc:  # pragma: no cover - corrupted key on disk
            logger.error(
                "SecretBox key at %s could not be loaded: %s. "
                "Encryption disabled (values stored in plain text).",
                self.key_path,
                exc,
            )
            return False
        return True

    def _load_or_create_key(self) -> Optional[bytes]:
        from cryptography.fernet import Fernet  # noqa: PLC0415

        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            if self.key_path.exists():
                key = self.key_path.read_bytes().strip()
                if not key:
                    raise ValueError("Empty key file")
                return key
            # Generate a fresh key.
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            self._tighten_permissions(self.key_path)
            logger.info("SecretBox: generated a new Fernet key at %s", self.key_path)
            return key
        except Exception as exc:  # pragma: no cover - permission / IO issue
            logger.error("SecretBox: cannot establish key at %s: %s", self.key_path, exc)
            return None

    @staticmethod
    def _tighten_permissions(path: Path) -> None:
        """Best-effort 0600 permissions on POSIX. No-op on Windows."""
        if os.name != "posix":
            return
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.debug("Could not tighten permissions on %s: %s", path, exc)

    # ------------------------------------------------------------------ public
    @property
    def available(self) -> bool:
        """True when the box has a working Fernet instance."""
        return self._available

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        """Return ``value`` wrapped in ``ofb1:<token>``; pass through if not enabled.

        ``None`` and empty strings are returned untouched so callers don't
        have to special-case "no key configured".
        """
        if value is None or value == "":
            return value
        if not self._available or self._fernet is None:
            return value
        if value.startswith(ENCRYPTED_PREFIX):
            # Already encrypted — don't double-wrap.
            return value
        token = self._fernet.encrypt(value.encode("utf-8"))
        return ENCRYPTED_PREFIX + base64.urlsafe_b64encode(token).decode("ascii")

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        """Inverse of :meth:`encrypt`. Plain-text values pass through.

        Decrypt failures are logged once and return ``None`` so the caller
        can decide what to do (typically: prompt the user to re-enter the
        key, or skip the provider).
        """
        if value is None or value == "":
            return value
        if not value.startswith(ENCRYPTED_PREFIX):
            return value  # legacy plain-text row
        if not self._available or self._fernet is None:
            return None
        try:
            payload = value[len(ENCRYPTED_PREFIX):]
            token = base64.urlsafe_b64decode(payload.encode("ascii"))
            return self._fernet.decrypt(token).decode("utf-8")
        except Exception as exc:
            logger.error("SecretBox decrypt failed: %s", exc)
            return None

    @staticmethod
    def is_encrypted(value: Optional[str]) -> bool:
        return bool(value) and value.startswith(ENCRYPTED_PREFIX)


# ---------------------------------------------------------------------------
# Module-level singleton accessor — paired with the registry's db_path.
# ---------------------------------------------------------------------------
_default_box: Optional[SecretBox] = None


def get_secret_box(key_path: Optional[str | Path] = None) -> SecretBox:
    """Return a process-wide singleton SecretBox.

    By default the key sits next to ``providers.db`` (i.e.
    ``<db_path_dir>/providers.key``). Callers that want a custom key path
    can pass it explicitly.
    """
    global _default_box
    if _default_box is None:
        if key_path is None:
            key_path = Path(".data") / "providers.key"
        _default_box = SecretBox(key_path)
    return _default_box


def reset_default_box() -> None:
    """Test helper — drops the singleton so a fresh key path can be used."""
    global _default_box
    _default_box = None


__all__ = ["SecretBox", "get_secret_box", "reset_default_box", "ENCRYPTED_PREFIX"]
