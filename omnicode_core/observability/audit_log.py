"""Append-only audit log for security-sensitive admin actions.

Stored at ``<user_data_dir>/audit.log`` as CSV (one event per line) so
ops can grep / awk / tail it without parsing JSON. Each line is::

    timestamp,actor,action,target,ip,outcome,extra

Where:
  * ``timestamp`` is ISO-8601 UTC.
  * ``actor`` is the username (or ``"anonymous"`` for unauthenticated
    /admin/users bootstrap calls).
  * ``action`` is the route + method, e.g. ``POST /admin/users``.
  * ``target`` is the resource ID (username, token hash, etc.).
  * ``ip`` is X-Forwarded-For (or remote_addr).
  * ``outcome`` is ``ok`` / ``denied`` / ``error``.
  * ``extra`` is a free-form short string (max 200 chars).

Why CSV not JSON: ops tooling. Loading this into Splunk / Loki /
DataDog is one config line for CSV; for JSON it's a parser. If you
want richer events, use the metrics endpoint.

Failure modes are silent: the function logs a warning and returns
``False`` rather than blocking the request. We never want a write to
``audit.log`` to take down the server.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _default_audit_path() -> Path:
    """Resolve the audit-log path under the user data dir.

    Mirrors the convention used by providers.db / users.db so all
    OmniCode state lives next to each other.
    """
    home = Path.home()
    return home / ".kiro" / "codebase-mcp" / "audit.log"


class AuditLog:
    """Thread-safe append-only audit logger.

    Rotation is intentionally *not* implemented here — running
    deployments are expected to plug logrotate or journal-only
    capture. Append-only is the security primitive.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _default_audit_path()
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create audit log dir %s: %s", self.path.parent, exc)

    def emit(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        ip: str = "",
        outcome: str = "ok",
        extra: str = "",
    ) -> bool:
        """Append one row to the audit log.

        Returns True on success, False on any I/O failure.
        """
        ts = datetime.now(timezone.utc).isoformat()
        row = [
            ts,
            actor or "anonymous",
            action,
            target[:200],
            ip[:64],
            outcome[:32],
            extra[:200],
        ]
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(row)
            return True
        except OSError as exc:
            logger.warning("Audit log write failed: %s", exc)
            return False


_DEFAULT: Optional[AuditLog] = None


def get_audit_log() -> AuditLog:
    """Return the process-wide AuditLog singleton.

    The first call lazily resolves the path from ``OMNICODE_AUDIT_LOG``
    env var (if set) or the default user-data-dir location.
    """
    global _DEFAULT
    if _DEFAULT is None:
        env_path = os.environ.get("OMNICODE_AUDIT_LOG")
        _DEFAULT = AuditLog(path=Path(env_path) if env_path else None)
    return _DEFAULT


__all__ = ["AuditLog", "get_audit_log"]
