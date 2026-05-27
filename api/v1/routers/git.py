#ruff: noqa
#type:ignore
"""
Git operations and session management endpoints
Handles git commands, branch operations, and AI session management
"""

from typing import List, Optional
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Query, HTTPException

from core import get_git_manager
from core.config import get_settings
from schemas.requests import GitOperationRequest, SessionRequest
from utils import (
    create_success_response,
    create_error_response,
    create_detailed_error_response,
    validate_file_path,
)

router1 = APIRouter(prefix="/git", tags=["git"])
router2=APIRouter(prefix="/session",tags=["session"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _resolve_trunk_branch(git_manager) -> Optional[str]:
    """Find the local 'trunk' branch.

    Probe order: ``main`` → ``master`` → ``trunk`` → ``develop`` → first
    other branch.  Returns ``None`` if the repo has no branches at all.
    """
    try:
        branches_res = await git_manager.get_branches()
    except Exception:
        return None
    if not branches_res.success:
        return None
    names: List[str] = []
    for b in (branches_res.data or {}).get("branches", []) or []:
        n = b.get("name", "") if isinstance(b, dict) else str(b)
        if n:
            names.append(n)
    for preferred in ("main", "master", "trunk", "develop"):
        if preferred in names:
            return preferred
    return names[0] if names else None

@router1.post("")
async def git_operations(request: GitOperationRequest):
    """Handle git operations with comprehensive error reporting"""
    try:
        git_manager = get_git_manager()
        settings = get_settings()

        if not git_manager:
            return create_detailed_error_response(
                "Git manager not initialized - service startup may have failed",
                500,
                "ServiceNotAvailable",
                {"initialization_status": "failed"},
                "GitManager",
                "initialization",
                settings.WORKING_DIR,
            )

        operation = request.operation.lower()

        # Pre-flight checks
        if not git_manager.is_git_repo:
            init_result = await git_manager.initialize_codebase_repo()
            if not init_result.success:
                return create_detailed_error_response(
                    f"Cannot initialize .codebase repository: {init_result.error}",
                    400,
                    "GitInitializationError",
                    {
                        "git_dir": str(git_manager.git_dir),
                        "init_output": init_result.output,
                        "suggested_fix": "Ensure working directory has write permissions and is a valid project directory",
                    },
                    "GitManager",
                    "repository_init",
                    settings.WORKING_DIR,
                )

        # Execute operation
        result = None
        try:
            if operation == "status":
                result = await git_manager.get_status()
            elif operation == "branches":
                result = await git_manager.get_branches()
            elif operation == "log":
                result = await git_manager.get_log(
                    max_commits=request.max_results or 10, file_path=request.file_path
                )
            elif operation == "diff":
                result = await git_manager.get_diff(
                    file_path=request.file_path, cached=request.cached or False
                )
            elif operation == "add":
                if not request.files and not request.file_path:
                    return create_detailed_error_response(
                        "Files or file_path required for add operation",
                        400,
                        "MissingParameter",
                        {
                            "required_fields": ["files", "file_path"],
                            "provided_request": request.dict(),
                        },
                        "GitManager",
                        "add",
                        settings.WORKING_DIR,
                    )

                files_to_add = [
                    f for f in (request.files or [request.file_path]) if f is not None
                ]
                result = await git_manager.add_files(files_to_add)

            elif operation == "commit":
                if not request.message:
                    return create_detailed_error_response(
                        "Commit message required for commit operation",
                        400,
                        "MissingParameter",
                        {
                            "required_field": "message",
                            "provided_request": request.dict(),
                        },
                        "GitManager",
                        "commit",
                        settings.WORKING_DIR,
                    )

                result = await git_manager.commit(
                    message=request.message, files=request.files
                )

            elif operation == "blame":
                if not request.file_path:
                    return create_detailed_error_response(
                        "File path required for blame operation",
                        400,
                        "MissingParameter",
                        {"required_field": "file_path"},
                        "GitManager",
                        "blame",
                        settings.WORKING_DIR,
                    )

                # Validate file exists
                try:
                    await validate_file_path(request.file_path, settings.WORKING_DIR)
                except HTTPException as e:
                    return create_detailed_error_response(
                        f"Invalid file path for blame: {request.file_path}",
                        400,
                        "FilePathError",
                        {"file_path": request.file_path, "validation_error": str(e)},
                        "GitManager",
                        "blame",
                        settings.WORKING_DIR,
                    )

                import os
                from omnicode.git_context.blame import GitBlameAnalyzer

                # Determine line range
                start_line = request.start_line or 1
                end_line = request.end_line

                if not end_line:
                    full_file_path = os.path.abspath(os.path.join(settings.WORKING_DIR, request.file_path))
                    try:
                        with open(full_file_path, "r", encoding="utf-8", errors="ignore") as f:
                            end_line = sum(1 for _ in f)
                    except Exception:
                        end_line = start_line + 100

                # Instantiate GitBlameAnalyzer
                analyzer = GitBlameAnalyzer(settings.WORKING_DIR)
                blame_lines = analyzer.get_blame(request.file_path, start_line, end_line)
                change_context = analyzer.get_change_context(request.file_path, (start_line, end_line))

                # Build response data structure
                data = {
                    "blame_lines": [bl.dict() for bl in blame_lines],
                    "change_context": change_context.dict() if change_context else None,
                    "start_line": start_line,
                    "end_line": end_line
                }

                class MockGitResult:
                    success = True
                    output = f"Git blame analyzed for {request.file_path} from lines {start_line} to {end_line}"
                    data = data
                    return_code = 0
                    error = None

                result = MockGitResult()
            elif operation == "history":
                # STAGE 5.4 — full history risk analysis
                if not request.file_path:
                    return create_detailed_error_response(
                        "File path required for history operation",
                        400,
                        "MissingParameter",
                        {"required_field": "file_path"},
                        "GitManager",
                        "history",
                        settings.WORKING_DIR,
                    )

                from omnicode.git_context.history import GitHistoryAnalyzer

                analyzer = GitHistoryAnalyzer(settings.WORKING_DIR)
                report = analyzer.analyze_file(request.file_path)

                class MockHistoryResult:
                    success = True
                    output = (
                        f"History analyzed for {request.file_path}: "
                        f"risk_score={report.risk_score:.2f} ({report.risk_level})"
                    )
                    data = {"history_report": report.dict()}
                    return_code = 0
                    error = None

                result = MockHistoryResult()
            else:
                return create_detailed_error_response(
                    f"Unsupported git operation: {operation}",
                    400,
                    "UnsupportedOperation",
                    {
                        "requested_operation": operation,
                        "supported_operations": [
                            "status",
                            "branches",
                            "log",
                            "diff",
                            "add",
                            "commit",
                            "blame",
                            "history",
                        ],
                    },
                    "GitManager",
                    operation,
                    settings.WORKING_DIR,
                )

        except Exception as op_error:
            return create_detailed_error_response(
                f"Git operation {operation} failed with exception: {str(op_error)}",
                500,
                "GitOperationException",
                {
                    "operation": operation,
                    "exception_type": type(op_error).__name__,
                    "exception_details": str(op_error),
                    "git_dir": str(git_manager.git_dir),
                },
                "GitManager",
                operation,
                settings.WORKING_DIR,
            )

        # Handle operation result
        if result and result.success:
            response_data = {
                "operation": operation,
                "output": result.output,
                "data": result.data,
                "git_dir": str(git_manager.git_dir),
                "working_dir": settings.WORKING_DIR,
            }
            return create_success_response(response_data)
        elif result:
            return create_detailed_error_response(
                f"Git {operation} failed: {result.error or 'Unknown error'}",
                400 if result.return_code not in [128, 129] else 500,
                "GitCommandFailed",
                {
                    "operation": operation,
                    "return_code": result.return_code,
                    "git_output": result.output,
                    "git_error": result.error,
                    "git_dir": str(git_manager.git_dir),
                    "command_suggestion": f"Try running: cd {settings.WORKING_DIR} && git {operation}",
                },
                "GitManager",
                operation,
                settings.WORKING_DIR,
            )
        else:
            return create_detailed_error_response(
                f"Git operation {operation} returned no result",
                500,
                "NoResult",
                {"operation": operation},
                "GitManager",
                operation,
                settings.WORKING_DIR,
            )

    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Unexpected error in git operations: {str(e)}",
            500,
            "UnexpectedError",
            {
                "operation": request.operation,
                "exception_type": type(e).__name__,
                "full_traceback": str(e),
            },
            "GitManager",
            request.operation,
            settings.WORKING_DIR,
        )


# Convenience endpoints
@router1.get("/status")
async def git_status():
    """Get git repository status"""
    return await git_operations(GitOperationRequest(operation="status"))


@router1.get("/branches")
async def git_branches():
    """Get all git branches"""
    return await git_operations(GitOperationRequest(operation="branches"))


@router1.get("/log")
async def git_log(
    max_commits: int = Query(10, description="Maximum number of commits"),
    file_path: Optional[str] = Query(
        None, description="File path for file-specific log"
    ),
):
    """Get git commit history"""
    return await git_operations(
        GitOperationRequest(
            operation="log", max_results=max_commits, file_path=file_path
        )
    )


@router1.get("/history")
async def git_history(
    file_path: str = Query(..., description="File path for history analysis"),
    max_commits: int = Query(200, description="Maximum commits to scan"),
):
    """STAGE 5.4 — Risk-aware history analysis for a file.

    Returns defensive-patch detection, co-changed files, and a 0–1 risk score
    so the caller can decide how cautiously to refactor.
    """
    try:
        from omnicode.git_context.history import GitHistoryAnalyzer

        settings = get_settings()
        analyzer = GitHistoryAnalyzer(
            settings.WORKING_DIR, max_commits_scanned=max_commits
        )
        report = analyzer.analyze_file(file_path)
        return create_success_response(report.model_dump())
    except Exception as e:
        return create_error_response(f"History analysis failed: {e}", 500)


@router1.post("/tree")
async def get_git_tree_visualization():
    """Get comprehensive git repository tree view"""
    try:
        git_manager = get_git_manager()
        if not git_manager:
            return create_error_response("Git manager not initialized", 500)

        # Get status and branches for tree view
        status_result = await git_manager.get_status()
        branches_result = await git_manager.get_branches()

        output = []
        output.append("🌳 Git Repository Tree View")
        output.append("=" * 40)

        if status_result.success:
            status_data = status_result.data.get("status", {})
            current_branch = status_data.get("current_branch", "unknown")
            output.append(f"📍 Current Branch: {current_branch}")

            # Show modified files
            modified = status_data.get("modified_files", [])
            if modified:
                output.append(f"📝 Modified Files ({len(modified)}):")
                for file in modified[:10]:
                    output.append(f"   ├── {file}")
                if len(modified) > 10:
                    output.append(f"   └── ... and {len(modified) - 10} more")

            # Show untracked files
            untracked = status_data.get("untracked_files", [])
            if untracked:
                output.append(f"❓ Untracked Files ({len(untracked)}):")
                for file in untracked[:5]:
                    output.append(f"   ├── {file}")
                if len(untracked) > 5:
                    output.append(f"   └── ... and {len(untracked) - 5} more")

        if branches_result.success:
            branches = branches_result.data.get("branches", [])
            output.append(f"🌿 Branches ({len(branches)}):")

            for branch in branches[:10]:
                is_current = branch.get("is_current", False)
                is_session = branch.get("name", "").startswith(
                    ("ai-session-", "session-")
                )

                prefix = "├── "
                if is_current:
                    prefix += "👉 "
                if is_session:
                    prefix += "🤖 "

                output.append(f"   {prefix}{branch.get('name', 'unknown')}")

            if len(branches) > 10:
                output.append(f"   └── ... and {len(branches) - 10} more branches")

        tree_output = "\n".join(output)

        return create_success_response(
            {
                "tree_view": tree_output,
                "current_branch": (
                    status_data.get("current_branch") if status_result.success else None
                ),
                "total_branches": len(branches) if branches_result.success else 0,
                "modified_files": len(modified) if status_result.success else 0,
                "untracked_files": len(untracked) if status_result.success else 0,
            }
        )

    except Exception as e:
        return create_error_response(f"Failed to generate git tree: {str(e)}", 500)


# Session management endpoints
@router2.post("")
async def session_operations(request: SessionRequest):
    """Handle session branch operations"""
    try:
        git_manager = get_git_manager()
        settings = get_settings()

        if not git_manager:
            return create_detailed_error_response(
                "Git manager not initialized - service startup may have failed",
                500,
                "ServiceNotAvailable",
                {"initialization_status": "failed"},
                "GitManager",
                "initialization",
                settings.WORKING_DIR,
            )

        # Pre-flight checks
        if not git_manager.is_git_repo:
            init_result = await git_manager.initialize_codebase_repo()
            if not init_result.success:
                return create_detailed_error_response(
                    f"Cannot initialize .codebase repository: {init_result.error}",
                    400,
                    "GitInitializationError",
                    {
                        "git_dir": str(git_manager.git_dir),
                        "init_output": init_result.output,
                        "suggested_fix": "Ensure working directory has write permissions",
                    },
                    "GitManager",
                    "repository_init",
                    settings.WORKING_DIR,
                )

        operation = request.operation.lower()

        if operation == "start":
            # Generate session name if not provided
            if not request.session_name:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                request.session_name = f"ai-session-{timestamp}"

            # Idempotent: if the branch already exists, just switch to it.
            # If we're already on it, return success.  Avoids the "fatal: a
            # branch named 'X' already exists" trap when the user clicks
            # Start twice or comes back to a previous session.
            current_branch_res = await git_manager.get_current_branch()
            if current_branch_res.success:
                current = (current_branch_res.data or {}).get("current_branch", "")
                if current == request.session_name:
                    return create_success_response({
                        "operation": "start",
                        "session_name": request.session_name,
                        "message": f"Already on session '{request.session_name}'.",
                        "output": "",
                        "reused": True,
                    })

            branches_res = await git_manager.get_branches()
            existing_names: List[str] = []
            if branches_res.success:
                for b in (branches_res.data or {}).get("branches", []):
                    n = b.get("name", "") if isinstance(b, dict) else str(b)
                    if n:
                        existing_names.append(n)

            if request.session_name in existing_names:
                # Already exists -> just check it out instead of failing
                checkout_res = await git_manager.checkout_branch(request.session_name)
                if checkout_res.success:
                    return create_success_response({
                        "operation": "start",
                        "session_name": request.session_name,
                        "message": f"Resumed existing session: {request.session_name}",
                        "output": checkout_res.output,
                        "reused": True,
                    })
                return create_error_response(
                    f"Branch '{request.session_name}' exists but checkout failed: "
                    f"{checkout_res.error}",
                    400,
                )

            result = await git_manager.create_branch(
                request.session_name, switch_to=True
            )

            if result.success:
                return create_success_response({
                    "operation": "start",
                    "session_name": request.session_name,
                    "message": f"Started new session: {request.session_name}",
                    "output": result.output,
                    "reused": False,
                })
            else:
                return create_error_response(
                    f"Failed to start session: {result.error}", 400
                )

        elif operation == "end":
            current_branch_result = await git_manager.get_current_branch()
            if not current_branch_result.success:
                return create_error_response("Could not determine current branch", 400)

            current_branch = current_branch_result.data.get("current_branch")

            # Pick the right trunk dynamically: prefer 'main', then 'master',
            # then 'trunk'/'develop'.  Hard-coding 'master' was wrong on
            # any repo following the modern default.
            trunk = await _resolve_trunk_branch(git_manager) or "master"

            if current_branch == trunk:
                return create_error_response(
                    f"Already on trunk branch '{trunk}'. Nothing to end.", 400
                )

            checkout_result = await git_manager.checkout_branch(trunk)
            if not checkout_result.success:
                return create_error_response(
                    f"Failed to switch to {trunk}: {checkout_result.error}", 400
                )

            response_data = {
                "operation": "end",
                "session_name": current_branch,
                "trunk_branch": trunk,
                "message": f"Ended session: {current_branch}, switched to {trunk}",
            }

            # Auto-merge if requested
            if request.auto_merge:
                merge_msg = request.message or f"Merge session: {current_branch}"
                merge_result = await git_manager.merge_branch(current_branch, merge_msg)
                if merge_result.success:
                    response_data["merged"] = True
                    response_data["message"] += f", merged to {trunk}"
                else:
                    response_data["merge_error"] = merge_result.error

            return create_success_response(response_data)

        elif operation == "switch":
            if not request.session_name:
                return create_error_response(
                    "Session name required for switch operation", 400
                )

            result = await git_manager.checkout_branch(request.session_name)

            if result.success:
                return create_success_response(
                    {
                        "operation": "switch",
                        "session_name": request.session_name,
                        "message": f"Switched to session: {request.session_name}",
                        "output": result.output,
                    }
                )
            else:
                return create_error_response(
                    f"Failed to switch to session: {result.error}", 400
                )

        elif operation == "list":
            result = await git_manager.list_session_branches()

            if result.success:
                return create_success_response(
                    {"operation": "list", "output": result.output, "data": result.data}
                )
            else:
                return create_error_response(
                    f"Failed to list sessions: {result.error}", 500
                )

        elif operation == "merge":
            if not request.session_name:
                return create_error_response(
                    "Session name required for merge operation", 400
                )

            merge_msg = request.message or f"Merge session: {request.session_name}"
            result = await git_manager.merge_branch(request.session_name, merge_msg)

            if result.success:
                return create_success_response(
                    {
                        "operation": "merge",
                        "session_name": request.session_name,
                        "message": f"Merged session {request.session_name}",
                        "output": result.output,
                    }
                )
            else:
                return create_error_response(
                    f"Failed to merge session: {result.error}", 400
                )

        elif operation == "delete":
            if not request.session_name:
                return create_detailed_error_response(
                    "Must provide branch or session name to delete the branch",
                    400,
                    "MissingParameter",
                    {"required_field": "session_name"},
                    "GitManager",
                    "delete",
                    settings.WORKING_DIR,
                )

            result = await git_manager.get_current_branch()
            if result.data.get("current_branch") == request.session_name:
                trunk = await _resolve_trunk_branch(git_manager) or "master"
                switch_result = await git_manager.checkout_branch(trunk)
                print(
                    f"Switched to {trunk} to delete the session. Result: {switch_result.output}"
                )

            result = await git_manager.delete_branch(request.session_name, force=True)
            if result.success:
                return create_success_response(
                    {
                        "operation": "delete",
                        "session_name": request.session_name,
                        "message": f"Branch deleted successfully: {request.session_name}",
                        "output": result.output,
                    }
                )
            else:
                return create_error_response(
                    f"Failed to delete session: {result.error}", 400
                )

        else:
            return create_error_response(
                f"Unsupported session operation: {operation}", 400
            )

    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Session operation failed: {str(e)}",
            500,
            "SessionOperationError",
            {"operation": request.operation, "exception_type": type(e).__name__},
            "SessionManager",
            request.operation,
            settings.WORKING_DIR,
        )


@router2.get("/current")
async def get_current_session():
    """Get current session information"""
    try:
        git_manager = get_git_manager()
        if not git_manager:
            return create_error_response("Git manager not initialized", 500)

        result = await git_manager.get_current_branch()

        if result.success:
            current_branch = result.data.get("current_branch") or ""
            # A session is anything that isn't one of the conventional
            # "trunk" branches.  Restricting to ai-session-* / session-*
            # caused user-named branches like '你好' to silently show as
            # "inactive" even though they were checked out.
            trunk_branches = {"master", "main", "trunk", "develop"}
            is_session = bool(current_branch) and current_branch not in trunk_branches
            # We still flag conventional names so the UI can offer them in
            # the session-list dropdown.
            is_conventional = current_branch.startswith(
                ("ai-session-", "session-")
            )

            return create_success_response(
                {
                    "current_branch": current_branch,
                    "is_session_branch": is_session,
                    "is_conventional_session": is_conventional,
                    "session_name": current_branch if is_session else None,
                }
            )
        else:
            return create_error_response(
                f"Failed to get current branch: {result.error}", 500
            )

    except Exception as e:
        return create_error_response(f"Failed to get current session: {str(e)}", 500)


@router2.post("/auto-commit")
async def auto_commit_change(
    file_path: str = Query(..., description="File that was changed"),
    operation: str = Query(..., description="Operation performed (write/edit)"),
    purpose: Optional[str] = Query(None, description="Purpose of the change"),
    quality_score: Optional[float] = Query(None, description="Quality score"),
):
    """Auto-commit a change made by AI"""
    try:
        git_manager = get_git_manager()
        if not git_manager:
            return create_error_response("Git manager not initialized", 500)

        # Only auto-commit if quality is good enough
        min_quality = 0.8
        if quality_score is not None and quality_score < min_quality:
            return create_success_response(
                {
                    "auto_commit": False,
                    "reason": f"Quality score {quality_score:.1%} below threshold {min_quality:.1%}",
                }
            )

        # Add the file
        add_result = await git_manager.add_files([file_path])
        if not add_result.success:
            return create_error_response(f"Failed to add file: {add_result.error}", 400)

        # Create commit message
        file_name = Path(file_path).name
        commit_msg = f"AI: {operation.title()} {file_name}"

        if purpose:
            commit_msg += f" - {purpose[:100]}"

        if quality_score:
            commit_msg += f" (Q: {quality_score:.1%})"

        # Commit
        commit_result = await git_manager.commit(commit_msg)

        if commit_result.success:
            commit_hash = (
                commit_result.data.get("commit_hash") if commit_result.data else None
            )
            return create_success_response(
                {
                    "auto_commit": True,
                    "commit_hash": commit_hash,
                    "commit_message": commit_msg,
                    "file_path": file_path,
                }
            )
        else:
            return create_error_response(
                f"Failed to commit: {commit_result.error}", 400
            )

    except Exception as e:
        return create_error_response(f"Auto-commit failed: {str(e)}", 500)



# ----------------------------------------------------------------------------
# STAGE 5.5 — Issue / PR linker
# ----------------------------------------------------------------------------
@router1.get("/issues")
async def git_issues(
    file_path: Optional[str] = Query(
        None,
        description="Optional file path. If supplied, only commits touching this file are scanned.",
    ),
    max_commits: int = Query(50, description="How many recent commits to scan"),
    enrich: bool = Query(
        True,
        description="If a GITHUB_TOKEN is set and the remote is GitHub, fetch issue metadata.",
    ),
):
    """Extract Issue / PR references from recent commit messages.

    Returns a list of structured references (one per unique issue) with:
    `kind`, `identifier`, `closing` (whether the commit verb implies the issue
    will close), `source_commit` (the commit that mentioned it), and — when
    available — `state`, `title`, `author`, `labels`, `url` from GitHub.
    """
    try:
        from omnicode.git_context.issue_linker import IssueLinker

        settings = get_settings()
        linker = IssueLinker(
            settings.WORKING_DIR,
            enable_network=enrich,
            max_commits=max_commits,
        )
        refs = linker.extract_from_repo(file_path=file_path, enrich=enrich)
        return create_success_response(
            {
                "references": [r.to_dict() for r in refs],
                "count": len(refs),
                "scanned_commits": max_commits,
                "github_enriched": bool(linker.github_token and linker.enable_network),
            }
        )
    except Exception as e:
        return create_error_response(f"Issue extraction failed: {e}", 500)
