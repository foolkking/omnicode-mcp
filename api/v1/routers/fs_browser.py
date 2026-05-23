"""
File-system browser endpoints
=============================

Provides OS-wide file/directory access for the native file picker UI.
All endpoints respect the deny-list configured in ``settings.FS_BROWSER_DENY_PATTERNS``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core import get_settings
from omnicode.fs_browser import (
    BinaryFileError,
    FileTooLargeError,
    FSBrowser,
    FSError,
    PathDeniedError,
)
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/fs", tags=["filesystem"])


def _make_browser() -> FSBrowser:
    s = get_settings()
    return FSBrowser(
        max_file_bytes=s.FS_BROWSER_MAX_FILE_BYTES,
        deny_patterns=s.FS_BROWSER_DENY_PATTERNS,
    )


def _enabled() -> bool:
    return bool(get_settings().FS_BROWSER_ENABLED)


def _disabled_response():
    return create_error_response(
        "File-system browser is disabled. "
        "Set FS_BROWSER_ENABLED=true in .env to enable.",
        403,
    )


# ---------------------------------------------------------------------------
# /fs/drives — root + drive enumeration
# ---------------------------------------------------------------------------
@router.get("/drives")
async def list_drives():
    """List filesystem roots — Windows drives, POSIX root, and home."""
    if not _enabled():
        return _disabled_response()
    try:
        browser = _make_browser()
        return create_success_response({"drives": browser.list_drives()})
    except Exception as e:
        return create_error_response(f"Failed to enumerate drives: {e}", 500)


# ---------------------------------------------------------------------------
# /fs/list — directory contents
# ---------------------------------------------------------------------------
@router.get("/list")
async def list_directory(
    path: str = Query(..., description="Absolute path to list"),
    include_hidden: bool = Query(False, description="Include hidden / dotfiles"),
    files_only: bool = Query(False),
    dirs_only: bool = Query(False),
):
    """List the contents of a directory anywhere on the file-system."""
    if not _enabled():
        return _disabled_response()
    try:
        browser = _make_browser()
        result = browser.list_dir(
            path,
            include_hidden=include_hidden,
            files_only=files_only,
            dirs_only=dirs_only,
        )
        return create_success_response(result)
    except PathDeniedError as e:
        return create_error_response(str(e), 403)
    except FSError as e:
        return create_error_response(str(e), 400)
    except Exception as e:
        return create_error_response(f"List failed: {e}", 500)


# ---------------------------------------------------------------------------
# /fs/stat — single path info
# ---------------------------------------------------------------------------
@router.get("/stat")
async def stat_path(path: str = Query(..., description="Absolute path")):
    if not _enabled():
        return _disabled_response()
    try:
        browser = _make_browser()
        return create_success_response(browser.stat(path))
    except PathDeniedError as e:
        return create_error_response(str(e), 403)
    except FSError as e:
        return create_error_response(str(e), 400)
    except Exception as e:
        return create_error_response(f"Stat failed: {e}", 500)


# ---------------------------------------------------------------------------
# /fs/open — read a single file
# ---------------------------------------------------------------------------
class FSOpenRequest(BaseModel):
    path: str = Field(..., description="Absolute path to read")
    max_bytes: Optional[int] = Field(None, ge=1)
    allow_binary: bool = Field(default=False)
    encoding: str = Field(default="utf-8")


@router.post("/open")
async def open_file(request: FSOpenRequest):
    """Read a file from anywhere on disk (subject to deny-list & size cap)."""
    if not _enabled():
        return _disabled_response()
    try:
        browser = _make_browser()
        result = browser.read_file(
            request.path,
            max_bytes=request.max_bytes,
            allow_binary=request.allow_binary,
            encoding=request.encoding,
        )
        return create_success_response(result)
    except PathDeniedError as e:
        return create_error_response(str(e), 403)
    except FileTooLargeError as e:
        return create_error_response(str(e), 413)
    except BinaryFileError as e:
        return create_error_response(str(e), 415)
    except FSError as e:
        return create_error_response(str(e), 400)
    except Exception as e:
        return create_error_response(f"Open failed: {e}", 500)
