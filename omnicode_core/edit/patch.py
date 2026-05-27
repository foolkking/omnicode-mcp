"""
Safe patch operations — preview, validate, apply, rollback.

This module provides the core edit safety layer that does NOT depend on
any LLM.  External AI editors (Cursor, Claude Code, Kiro, Aider) generate
patches; OmniCode validates and applies them safely with rollback support.

Flow:
    1. preview_patch(file, patch_text) → unified diff showing what would change
    2. validate_patch(file, patch_text) → run static checks on the result
    3. apply_patch(file, patch_text) → write to disk + create snapshot
    4. rollback_patch(session_id) → restore from snapshot
    5. explain_patch(patch_text) → human-readable summary of changes

Edit sessions are stored in .data/edit_sessions/ as JSON files.
File snapshots (pre-apply backups) live in .data/snapshots/.
"""

import difflib
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PatchResult:
    """Result of a patch operation."""
    success: bool
    message: str
    session_id: Optional[str] = None
    diff: Optional[str] = None
    diagnostics: Optional[List[Dict[str, Any]]] = None
    file_path: Optional[str] = None
    lines_added: int = 0
    lines_removed: int = 0
    rollback_available: bool = False


@dataclass
class EditSession:
    """Record of a single edit operation."""
    session_id: str
    file_path: str
    timestamp: str
    patch_type: str  # "unified_diff" | "full_replace" | "line_range"
    original_hash: str
    new_hash: str
    diff: str
    lines_added: int = 0
    lines_removed: int = 0
    applied: bool = False
    rolled_back: bool = False
    source: str = "external"  # "external" | "ai_edit" | "manual"
    metadata: Dict[str, Any] = field(default_factory=dict)


class PatchManager:
    """Manages safe patch operations with snapshot-based rollback.

    All operations are relative to a working directory.  Snapshots and
    session records are stored in ``<working_dir>/.data/``.
    """

    def __init__(self, working_dir: str):
        self.working_dir = os.path.abspath(working_dir)
        self.data_dir = os.path.join(self.working_dir, ".data")
        self.snapshots_dir = os.path.join(self.data_dir, "snapshots")
        self.sessions_dir = os.path.join(self.data_dir, "edit_sessions")
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(self.sessions_dir, exist_ok=True)

    # -------------------------------------------------------------------------
    # Preview
    # -------------------------------------------------------------------------
    def preview_patch(
        self,
        file_path: str,
        new_content: str,
    ) -> PatchResult:
        """Show a unified diff of what applying new_content would change.

        Does NOT modify the file.
        """
        full_path = self._resolve(file_path)
        if not os.path.exists(full_path):
            return PatchResult(
                success=False,
                message=f"File not found: {file_path}",
            )

        original = self._read(full_path)
        diff = self._make_diff(original, new_content, file_path)
        added, removed = self._count_changes(diff)

        if not diff.strip():
            return PatchResult(
                success=True,
                message="No changes — new content is identical to current file.",
                diff="",
                lines_added=0,
                lines_removed=0,
                file_path=file_path,
            )

        return PatchResult(
            success=True,
            message=f"Preview: {added} lines added, {removed} lines removed",
            diff=diff,
            lines_added=added,
            lines_removed=removed,
            file_path=file_path,
        )

    # -------------------------------------------------------------------------
    # Validate
    # -------------------------------------------------------------------------
    async def validate_patch(
        self,
        file_path: str,
        new_content: str,
    ) -> PatchResult:
        """Validate a patch by running static analysis on the result.

        Writes to a temp file, runs ruff/eslint/etc, then deletes the temp.
        Does NOT modify the original file.
        """
        full_path = self._resolve(file_path)
        ext = os.path.splitext(file_path)[1].lower()

        # Write to temp location for checking
        temp_path = full_path + ".omnicode_validate_tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            diagnostics = await self._run_checks(temp_path, ext)

            errors = [d for d in diagnostics if d.get("severity") == "error"]
            warnings = [d for d in diagnostics if d.get("severity") == "warning"]

            if errors:
                return PatchResult(
                    success=False,
                    message=f"Validation failed: {len(errors)} error(s), {len(warnings)} warning(s)",
                    diagnostics=diagnostics,
                    file_path=file_path,
                )

            return PatchResult(
                success=True,
                message=f"Validation passed ({len(warnings)} warning(s))",
                diagnostics=diagnostics,
                file_path=file_path,
            )
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # -------------------------------------------------------------------------
    # Apply
    # -------------------------------------------------------------------------
    def apply_patch(
        self,
        file_path: str,
        new_content: str,
        source: str = "external",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PatchResult:
        """Apply new content to a file with snapshot backup.

        Creates a snapshot of the original file before overwriting, and
        records an edit session for rollback.
        """
        full_path = self._resolve(file_path)

        # Read original (or empty if new file)
        if os.path.exists(full_path):
            original = self._read(full_path)
        else:
            original = ""
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

        # Create diff
        diff = self._make_diff(original, new_content, file_path)
        added, removed = self._count_changes(diff)

        # Create snapshot
        session_id = str(uuid.uuid4())[:12]
        original_hash = hashlib.sha256(original.encode()).hexdigest()[:16]
        new_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]

        snapshot_path = os.path.join(self.snapshots_dir, f"{session_id}.snapshot")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            f.write(original)

        # Write new content
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # Record session
        session = EditSession(
            session_id=session_id,
            file_path=file_path,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            patch_type="full_replace",
            original_hash=original_hash,
            new_hash=new_hash,
            diff=diff[:10000],  # cap diff size in session record
            lines_added=added,
            lines_removed=removed,
            applied=True,
            source=source,
            metadata=metadata or {},
        )
        self._save_session(session)

        logger.info(f"Patch applied: {file_path} (session={session_id}, +{added}/-{removed})")

        return PatchResult(
            success=True,
            message=f"Applied: +{added}/-{removed} lines (session={session_id})",
            session_id=session_id,
            diff=diff,
            lines_added=added,
            lines_removed=removed,
            file_path=file_path,
            rollback_available=True,
        )

    # -------------------------------------------------------------------------
    # Rollback
    # -------------------------------------------------------------------------
    def rollback_patch(self, session_id: str) -> PatchResult:
        """Rollback a previously applied patch using its snapshot."""
        session = self._load_session(session_id)
        if not session:
            return PatchResult(
                success=False,
                message=f"Session not found: {session_id}",
            )

        if session.rolled_back:
            return PatchResult(
                success=False,
                message=f"Session {session_id} was already rolled back.",
            )

        if not session.applied:
            return PatchResult(
                success=False,
                message=f"Session {session_id} was never applied.",
            )

        snapshot_path = os.path.join(self.snapshots_dir, f"{session_id}.snapshot")
        if not os.path.exists(snapshot_path):
            return PatchResult(
                success=False,
                message=f"Snapshot file missing for session {session_id}.",
            )

        # Restore original content
        full_path = self._resolve(session.file_path)
        with open(snapshot_path, "r", encoding="utf-8") as f:
            original_content = f.read()
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(original_content)

        # Update session record
        session.rolled_back = True
        self._save_session(session)

        logger.info(f"Rolled back session {session_id} for {session.file_path}")

        return PatchResult(
            success=True,
            message=f"Rolled back {session.file_path} to pre-edit state (session={session_id})",
            session_id=session_id,
            file_path=session.file_path,
        )

    # -------------------------------------------------------------------------
    # Explain
    # -------------------------------------------------------------------------
    def explain_patch(self, file_path: str, new_content: str) -> PatchResult:
        """Generate a human-readable summary of what a patch does."""
        full_path = self._resolve(file_path)
        if not os.path.exists(full_path):
            return PatchResult(success=False, message=f"File not found: {file_path}")

        original = self._read(full_path)
        diff = self._make_diff(original, new_content, file_path)
        added, removed = self._count_changes(diff)

        # Simple heuristic explanation
        explanations = []
        if added > 0 and removed == 0:
            explanations.append(f"Adds {added} new lines")
        elif removed > 0 and added == 0:
            explanations.append(f"Removes {removed} lines")
        else:
            explanations.append(f"Modifies code: +{added}/-{removed} lines")

        # Detect what kind of changes
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                stripped = line[1:].strip()
                if stripped.startswith(("def ", "async def ")):
                    explanations.append(f"Adds/modifies function: {stripped[:60]}")
                elif stripped.startswith("class "):
                    explanations.append(f"Adds/modifies class: {stripped[:60]}")
                elif stripped.startswith(("import ", "from ")):
                    explanations.append(f"Adds import: {stripped[:60]}")

        return PatchResult(
            success=True,
            message="\n".join(explanations[:10]),
            diff=diff,
            lines_added=added,
            lines_removed=removed,
            file_path=file_path,
        )

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------
    def list_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent edit sessions."""
        sessions = []
        if not os.path.exists(self.sessions_dir):
            return sessions

        files = sorted(
            Path(self.sessions_dir).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files[:limit]:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    # Don't include full diff in listing
                    data.pop("diff", None)
                    sessions.append(data)
            except Exception:
                continue
        return sessions

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get full details of a specific edit session."""
        session = self._load_session(session_id)
        if session:
            return asdict(session)
        return None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _resolve(self, file_path: str) -> str:
        """Resolve a relative file path to absolute."""
        if os.path.isabs(file_path):
            return file_path
        return os.path.join(self.working_dir, file_path)

    @staticmethod
    def _read(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    @staticmethod
    def _make_diff(original: str, new: str, file_path: str) -> str:
        orig_lines = original.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines, new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
        return "\n".join(diff)

    @staticmethod
    def _count_changes(diff: str) -> tuple:
        added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        return added, removed

    async def _run_checks(self, file_path: str, ext: str) -> List[Dict[str, Any]]:
        """Run appropriate static analysis on a file."""
        diagnostics = []
        import subprocess

        if ext == ".py":
            try:
                result = subprocess.run(
                    ["ruff", "check", "--output-format=json", file_path],
                    capture_output=True, text=True, timeout=15,
                )
                if result.stdout.strip():
                    items = json.loads(result.stdout)
                    for item in items:
                        diagnostics.append({
                            "severity": "error" if item.get("fix") is None else "warning",
                            "message": item.get("message", ""),
                            "code": item.get("code", ""),
                            "line": item.get("location", {}).get("row", 0),
                        })
            except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
                pass

        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            try:
                result = subprocess.run(
                    ["eslint", "--format=json", file_path],
                    capture_output=True, text=True, timeout=15,
                )
                if result.stdout.strip():
                    data = json.loads(result.stdout)
                    for file_result in data:
                        for msg in file_result.get("messages", []):
                            diagnostics.append({
                                "severity": "error" if msg.get("severity") == 2 else "warning",
                                "message": msg.get("message", ""),
                                "code": msg.get("ruleId", ""),
                                "line": msg.get("line", 0),
                            })
            except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
                pass

        return diagnostics

    def _save_session(self, session: EditSession):
        path = os.path.join(self.sessions_dir, f"{session.session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(session), f, indent=2, ensure_ascii=False)

    def _load_session(self, session_id: str) -> Optional[EditSession]:
        path = os.path.join(self.sessions_dir, f"{session_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return EditSession(**data)
        except Exception:
            return None
