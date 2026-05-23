"""
File-System Browser
===================

Backend support for opening files from anywhere on the user's machine,
not just from inside ``WORKING_DIR``.

Safety model
------------
This is a single-user developer tool, but we still apply a few safety nets:

1. A configurable deny-list (``FS_BROWSER_DENY_PATTERNS``) blocks paths that
   are commonly sensitive (``/etc/shadow``, ``C:\\Windows\\System32\\config``,
   ``/proc``, etc.).  The check is case-insensitive on Windows.
2. A maximum readable file size (``FS_BROWSER_MAX_FILE_BYTES``) prevents the
   server from returning huge binaries.
3. Binary detection refuses to return obviously non-text files unless the
   caller passes ``allow_binary=True`` (and even then we cap the byte size).
4. Symlinks are resolved before path checks so deny-list bypass via symlinks
   is impossible.

The module exposes :class:`FSBrowser`, a thin OOP wrapper around ``pathlib``
plus drive enumeration on Windows.
"""

from __future__ import annotations

import logging
import os
import platform
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class FSError(Exception):
    """Generic file-system browser error."""


class PathDeniedError(FSError):
    """Raised when the requested path matches a deny-list entry."""


class FileTooLargeError(FSError):
    """Raised when a file exceeds the configured max byte size."""


class BinaryFileError(FSError):
    """Raised when attempting to read a binary file as text."""


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------
@dataclass
class FSEntry:
    """A single entry returned by :meth:`FSBrowser.list_dir`."""

    name: str
    path: str
    is_dir: bool
    is_file: bool
    is_symlink: bool = False
    size: int = 0
    modified: Optional[str] = None
    extension: str = ""
    hidden: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "is_file": self.is_file,
            "is_symlink": self.is_symlink,
            "size": self.size,
            "modified": self.modified,
            "extension": self.extension,
            "hidden": self.hidden,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
class FSBrowser:
    """OS-wide directory listing + safe file reading."""

    _BINARY_SAMPLE_BYTES = 4096
    _PRINTABLE_BYTES = bytes(range(0x20, 0x7F)) + b"\r\n\t\f\b"

    def __init__(
        self,
        max_file_bytes: int = 2 * 1024 * 1024,
        deny_patterns: Optional[List[str]] = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.deny_patterns = [p.strip() for p in (deny_patterns or []) if p.strip()]
        self.is_windows = platform.system().lower().startswith("win")

    # ------------------------------------------------------------ helpers
    def _normalise(self, raw_path: str) -> Path:
        """Resolve, expanduser, expandvars, and check existence."""
        if not raw_path or not raw_path.strip():
            raise FSError("Empty path supplied.")
        expanded = os.path.expandvars(os.path.expanduser(raw_path.strip()))
        path = Path(expanded)
        try:
            resolved = path.resolve(strict=False)
        except Exception as exc:
            raise FSError(f"Cannot resolve path: {exc}") from exc
        return resolved

    def _is_denied(self, path: Path) -> bool:
        if not self.deny_patterns:
            return False
        path_str = str(path).replace("\\", "/")
        for pat in self.deny_patterns:
            norm = pat.replace("\\", "/")
            if self.is_windows:
                if norm.lower() in path_str.lower():
                    return True
            else:
                if norm in path_str:
                    return True
        return False

    def _ensure_allowed(self, path: Path) -> None:
        if self._is_denied(path):
            raise PathDeniedError(
                f"Path '{path}' is on the file-system browser deny-list."
            )

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        if path.name.startswith("."):
            return True
        if platform.system().lower().startswith("win"):
            try:
                import ctypes

                FILE_ATTRIBUTE_HIDDEN = 0x2
                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
                if attrs != -1 and (attrs & FILE_ATTRIBUTE_HIDDEN):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _looks_binary(sample: bytes) -> bool:
        if not sample:
            return False
        # Strong signal: NUL byte → almost certainly binary
        if b"\x00" in sample:
            return True
        # Count "suspicious" control bytes — non-printable ASCII control chars
        # that are NOT \t (0x09), \n (0x0A), \r (0x0D), \f (0x0C), \b (0x08).
        # High-bit bytes (>= 0x80) are part of UTF-8 multi-byte sequences and
        # are common in text files (CJK, emojis, etc.) — DO NOT count them.
        allowed_ctrl = {0x09, 0x0A, 0x0D, 0x0C, 0x08, 0x1B}  # last is ESC
        suspicious = sum(
            1 for b in sample
            if b < 0x20 and b not in allowed_ctrl
        )
        ratio = suspicious / len(sample)
        return ratio > 0.10

    # ------------------------------------------------------------ drives
    def list_drives(self) -> List[Dict[str, Any]]:
        """Enumerate filesystem roots (Windows drives or POSIX root)."""
        roots: List[Dict[str, Any]] = []
        if self.is_windows:
            try:
                import ctypes

                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for i, letter in enumerate(string.ascii_uppercase):
                    if bitmask & (1 << i):
                        path = f"{letter}:\\"
                        try:
                            usage = self._safe_disk_usage(path)
                        except Exception:
                            usage = None
                        roots.append({
                            "label": letter,
                            "path": path,
                            "type": "drive",
                            "usage": usage,
                        })
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to enumerate Windows drives: %s", exc)
        else:
            roots.append({
                "label": "/",
                "path": "/",
                "type": "root",
                "usage": self._safe_disk_usage("/"),
            })

        # Always expose the user's home directory as a quick-pick
        try:
            home = str(Path.home())
            if home and home not in [r["path"] for r in roots]:
                roots.append({
                    "label": "Home",
                    "path": home,
                    "type": "home",
                    "usage": None,
                })
        except Exception:
            pass

        return roots

    @staticmethod
    def _safe_disk_usage(path: str) -> Optional[Dict[str, int]]:
        try:
            import shutil

            usage = shutil.disk_usage(path)
            return {
                "total": int(usage.total),
                "used": int(usage.used),
                "free": int(usage.free),
            }
        except Exception:
            return None

    # ------------------------------------------------------------ list_dir
    def list_dir(
        self,
        raw_path: str,
        include_hidden: bool = False,
        files_only: bool = False,
        dirs_only: bool = False,
    ) -> Dict[str, Any]:
        """List the contents of a directory."""
        path = self._normalise(raw_path)
        self._ensure_allowed(path)
        if not path.exists():
            raise FSError(f"Path does not exist: {path}")
        if not path.is_dir():
            raise FSError(f"Not a directory: {path}")
        if not os.access(str(path), os.R_OK):
            raise FSError(f"Permission denied: {path}")

        entries: List[FSEntry] = []
        try:
            iterator = os.scandir(str(path))
        except PermissionError as exc:
            raise FSError(f"Permission denied while listing: {path}") from exc

        with iterator as it:
            for de in it:
                try:
                    full = Path(de.path)
                    is_link = de.is_symlink()
                    is_dir = False
                    is_file = False
                    try:
                        is_dir = de.is_dir(follow_symlinks=False)
                        is_file = de.is_file(follow_symlinks=False)
                    except OSError:
                        pass
                    if dirs_only and not is_dir:
                        continue
                    if files_only and not is_file:
                        continue
                    hidden = self._is_hidden(full)
                    if hidden and not include_hidden:
                        continue
                    size = 0
                    modified = None
                    try:
                        st = de.stat(follow_symlinks=False)
                        size = int(st.st_size)
                        modified = datetime.fromtimestamp(st.st_mtime).isoformat()
                    except OSError:
                        pass
                    entries.append(FSEntry(
                        name=de.name,
                        path=str(full),
                        is_dir=is_dir,
                        is_file=is_file,
                        is_symlink=is_link,
                        size=size,
                        modified=modified,
                        extension=full.suffix.lower() if is_file else "",
                        hidden=hidden,
                    ))
                except Exception as exc:
                    entries.append(FSEntry(
                        name=getattr(de, "name", "?"),
                        path=getattr(de, "path", str(path)),
                        is_dir=False,
                        is_file=False,
                        error=str(exc),
                    ))

        # Directories first, then files; case-insensitive name sort
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

        # Build breadcrumb segments
        breadcrumb: List[Dict[str, str]] = []
        cur = path
        while True:
            breadcrumb.insert(0, {"name": cur.name or str(cur), "path": str(cur)})
            parent = cur.parent
            if parent == cur:
                break
            cur = parent

        return {
            "path": str(path),
            "parent": str(path.parent) if path.parent != path else None,
            "exists": True,
            "is_dir": True,
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
            "breadcrumb": breadcrumb,
        }

    # ------------------------------------------------------------ read_file
    def read_file(
        self,
        raw_path: str,
        max_bytes: Optional[int] = None,
        allow_binary: bool = False,
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """Read a file's content (text by default)."""
        path = self._normalise(raw_path)
        self._ensure_allowed(path)
        if not path.exists():
            raise FSError(f"File does not exist: {path}")
        if not path.is_file():
            raise FSError(f"Not a regular file: {path}")
        if not os.access(str(path), os.R_OK):
            raise FSError(f"Permission denied: {path}")

        size = path.stat().st_size
        cap = int(max_bytes if max_bytes is not None else self.max_file_bytes)
        if size > cap:
            raise FileTooLargeError(
                f"File is {size} bytes, exceeds cap of {cap} bytes."
            )

        with open(path, "rb") as fh:
            sample = fh.read(self._BINARY_SAMPLE_BYTES)
            is_binary = self._looks_binary(sample)
            if is_binary and not allow_binary:
                raise BinaryFileError(
                    f"File appears to be binary: {path}. "
                    "Pass allow_binary=true to read as base64."
                )
            rest = fh.read()
        data = sample + rest

        if is_binary:
            import base64

            return {
                "path": str(path),
                "size": size,
                "encoding": "base64",
                "is_binary": True,
                "content": base64.b64encode(data).decode("ascii"),
                "extension": path.suffix.lower(),
            }
        try:
            text = data.decode(encoding, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        return {
            "path": str(path),
            "size": size,
            "encoding": encoding,
            "is_binary": False,
            "content": text,
            "extension": path.suffix.lower(),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        }

    # ------------------------------------------------------------ stat
    def stat(self, raw_path: str) -> Dict[str, Any]:
        """Return metadata for a single path (file or directory)."""
        path = self._normalise(raw_path)
        self._ensure_allowed(path)
        exists = path.exists()
        info: Dict[str, Any] = {
            "path": str(path),
            "exists": exists,
            "is_dir": False,
            "is_file": False,
        }
        if not exists:
            return info
        try:
            st = path.stat()
            info.update({
                "is_dir": path.is_dir(),
                "is_file": path.is_file(),
                "is_symlink": path.is_symlink(),
                "size": int(st.st_size),
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "extension": path.suffix.lower(),
                "readable": os.access(str(path), os.R_OK),
                "writable": os.access(str(path), os.W_OK),
            })
        except OSError as exc:
            info["error"] = str(exc)
        return info
