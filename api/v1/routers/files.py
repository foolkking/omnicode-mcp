"""
File operation endpoints
Handles intelligent write, AI-assisted edit, and read operations
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from core import get_edit_pipeline, get_search_engine, get_write_pipeline
from core.config import get_settings
from omnicode.pipelines.edit import EditRequest
from schemas.requests import EditRequestAPI, FileRequest, WriteRequest
from utils import (
    create_detailed_error_response,
    create_error_response,
    create_success_response,
    validate_file_path,
)

router = APIRouter(tags=["files"])


@router.post("/write")
async def intelligent_write(request: WriteRequest):
    """Intelligent write operation with formatting and dependency checking"""
    try:
        write_pipeline = get_write_pipeline()
        settings = get_settings()
        print("relative filepath:",request.file_path)
        if not write_pipeline:
            return create_detailed_error_response(
                "Write pipeline not initialized",
                500,
                "ServiceNotAvailable",
                {"service": "WritePipeline"},
                "WritePipeline",
                "initialization",
                settings.WORKING_DIR,
            )

        # Validate file path
        try:
            file_path = await validate_file_path(
                request.file_path, settings.WORKING_DIR
            )
            print("after file path:",file_path)
        except HTTPException as e:
            return create_detailed_error_response(
                f"Invalid file path: {request.file_path}",
                e.status_code,
                "FilePathValidationError",
                {"file_path": request.file_path, "validation_error": e.detail},
                "WritePipeline",
                "file_validation",
                settings.WORKING_DIR,
            )

        if not request.content.strip():
            return create_detailed_error_response(
                "Empty content provided for write operation",
                400,
                "EmptyContent",
                {
                    "file_path": request.file_path,
                    "content_length": len(request.content),
                },
                "WritePipeline",
                "content_validation",
                settings.WORKING_DIR,
            )

        try:
            result = await write_pipeline.process_write(
                content=request.content,
                file_path=str(file_path),
                purpose=request.purpose,
                language=request.language,
                save_to_file=request.save_to_file,
            )
        except Exception as pipeline_error:
            return create_detailed_error_response(
                f"Write pipeline processing failed: {str(pipeline_error)}",
                500,
                "WritePipelineError",
                {
                    "file_path": request.file_path,
                    "content_preview": (
                        request.content[:200] + "..."
                        if len(request.content) > 200
                        else request.content
                    ),
                    "language": request.language,
                    "pipeline_error": str(pipeline_error),
                    "error_type": type(pipeline_error).__name__,
                },
                "WritePipeline",
                "process_write",
                settings.WORKING_DIR,
            )

        # Convert result to API response format
        response_data = {
            "file_path": result.file_path,
            "success": result.success,
            "quality_score": result.quality_score,
            "summary": result.summary,
            "formatting": {
                "success": (
                    result.format_result.success
                    if hasattr(result, "format_result")
                    else True
                ),
                "changes_made": (
                    result.format_result.changes_made
                    if hasattr(result, "format_result")
                    else []
                ),
                "errors": (
                    result.format_result.errors
                    if hasattr(result, "format_result")
                    else []
                ),
                "warnings": (
                    result.format_result.warnings
                    if hasattr(result, "format_result")
                    else []
                ),
            },
            "dependencies": {
                "success": (
                    result.dependency_result.success
                    if hasattr(result, "dependency_result")
                    else True
                ),
                "imports_found": (
                    len(result.dependency_result.imports_found)
                    if hasattr(result, "dependency_result")
                    else 0
                ),
                "missing_dependencies": (
                    result.dependency_result.missing_dependencies
                    if hasattr(result, "dependency_result")
                    else []
                ),
                "resolved_symbols": (
                    result.dependency_result.resolved_symbols
                    if hasattr(result, "dependency_result")
                    else []
                ),
                "duplicate_definitions": (
                    result.dependency_result.duplicate_definitions
                    if hasattr(result, "dependency_result")
                    else []
                ),
                "suggestions": (
                    result.dependency_result.suggestions
                    if hasattr(result, "dependency_result")
                    else []
                ),
            },
            "errors": result.errors if hasattr(result, "errors") else [],
            "warnings": result.warnings if hasattr(result, "warnings") else [],
            "edit_session_id": getattr(result, "edit_session_id", None),
            "rollback_available": bool(getattr(result, "edit_session_id", None)),
        }

        if result.success:
            return create_success_response(response_data)
        else:
            # Analyze why it failed
            failure_reasons = []
            suggested_fixes = []

            if hasattr(result, "format_result") and not result.format_result.success:
                failure_reasons.append("Code formatting failed")
                suggested_fixes.extend(
                    [
                        "Check for syntax errors in the code",
                        "Ensure proper indentation and structure",
                        "Verify the programming language is correctly detected",
                    ]
                )

            if result.quality_score < 0.6:
                failure_reasons.append(
                    f"Quality score too low: {result.quality_score:.1%}"
                )
                suggested_fixes.extend(
                    [
                        "Review code for completeness and correctness",
                        "Add proper documentation and comments",
                        "Ensure all imports and dependencies are included",
                    ]
                )

            if (
                hasattr(result, "dependency_result")
                and result.dependency_result.missing_dependencies
            ):
                failure_reasons.append(
                    f"Missing dependencies: {', '.join(result.dependency_result.missing_dependencies)}"
                )
                suggested_fixes.append(
                    "Add required imports or install missing packages"
                )

            if not failure_reasons:
                failure_reasons.append("Unknown quality issue")
                suggested_fixes.append("Check write pipeline logs for more details")

            response_data["failure_analysis"] = {
                "failure_reasons": failure_reasons,
                "suggested_fixes": suggested_fixes,
                "quality_threshold": 0.6,
            }
            response_data["success"] = False
            # Pipeline ran successfully but quality bar wasn't met — return
            # 200 with structured failure data so the UI renders diagnostics.
            return create_success_response(response_data)

    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Unexpected error in write operation: {str(e)}",
            500,
            "UnexpectedError",
            {
                "file_path": request.file_path if "request" in locals() else "unknown",
                "exception_type": type(e).__name__,
                "full_error": str(e),
            },
            "WritePipeline",
            "write_operation",
            settings.WORKING_DIR,
        )


@router.get("/write/stats")
async def get_write_stats():
    """Get write pipeline statistics"""
    try:
        write_pipeline = get_write_pipeline()
        if not write_pipeline:
            return create_error_response("Write pipeline not initialized", 500)

        stats = write_pipeline.get_stats()
        return create_success_response(stats)

    except Exception as e:
        return create_error_response(f"Failed to get write stats: {str(e)}", 500)


@router.post("/edit")
async def intelligent_edit(request: EditRequestAPI):
    """AI-assisted code editing with comprehensive error reporting"""
    try:
        edit_pipeline = get_edit_pipeline()
        settings = get_settings()

        if not edit_pipeline:
            return create_detailed_error_response(
                "Edit pipeline not initialized",
                500,
                "ServiceNotAvailable",
                {"service": "EditPipeline"},
                "EditPipeline",
                "initialization",
                settings.WORKING_DIR,
            )

        # Validate file path
        try:
            file_path = await validate_file_path(
                request.target_file, settings.WORKING_DIR
            )
        except HTTPException as e:
            return create_detailed_error_response(
                f"Invalid target file path: {request.target_file}",
                e.status_code,
                "FilePathValidationError",
                {"target_file": request.target_file, "validation_error": e.detail},
                "EditPipeline",
                "file_validation",
                settings.WORKING_DIR,
            )

        # Check if file exists
        if not file_path.exists():
            return create_detailed_error_response(
                f"Target file does not exist: {request.target_file}",
                404,
                "FileNotFound",
                {
                    "target_file": request.target_file,
                    "resolved_path": str(file_path),
                    "suggestion": "Ensure the file exists before attempting to edit it",
                },
                "EditPipeline",
                "file_existence_check",
                settings.WORKING_DIR,
            )

        # Convert API request to internal EditRequest
        edit_request = EditRequest(
            target_file=str(file_path),
            instructions=request.instructions,
            code_edit=request.code_edit,
            language=request.language,
        )

        # When dry_run is requested, force save_to_file off so the LLM
        # output never lands on disk. The diff still gets surfaced via
        # `preview_diff` below so the AI editor can show the user the
        # change and decide whether to re-run with dry_run=false.
        effective_save = request.save_to_file and not request.dry_run

        # Process through edit pipeline
        try:
            result = await edit_pipeline.process_edit(
                request=edit_request, save_to_file=effective_save
            )
        except Exception as pipeline_error:
            return create_detailed_error_response(
                f"Edit pipeline processing failed: {str(pipeline_error)}",
                500,
                "EditPipelineError",
                {
                    "target_file": request.target_file,
                    "instructions": (
                        request.instructions[:100] + "..."
                        if len(request.instructions) > 100
                        else request.instructions
                    ),
                    "pipeline_error": str(pipeline_error),
                    "error_type": type(pipeline_error).__name__,
                },
                "EditPipeline",
                "process_edit",
                settings.WORKING_DIR,
            )

        # Create detailed response data
        response_data = {
            "file_path": result.file_path,
            "success": result.success,
            "instructions": result.instructions,
            "summary": result.summary,
            "quality_score": result.quality_score,
            "processing": {
                "gemini_edit_success": result.gemini_edit_success,
                "format_success": result.format_success,
                "error_correction_attempts": result.error_correction_attempts,
                "total_gemini_calls": result.total_gemini_calls,
                "processing_time_seconds": result.processing_time_seconds,
            },
            "content_info": {
                "original_length": len(result.original_content),
                "final_length": len(result.final_content),
                "content_changed": result.original_content != result.final_content,
            },
            "errors": {
                "gemini_errors": result.gemini_errors,
                "format_errors": result.format_errors,
                "warnings": result.warnings,
            },
            "edit_session_id": getattr(result, "edit_session_id", None),
            "rollback_available": bool(getattr(result, "edit_session_id", None)),
            "dry_run": request.dry_run,
        }

        # In dry-run mode, attach a unified diff so the AI editor can
        # show the user what *would* have changed without anything
        # actually being written to disk.
        if request.dry_run and result.original_content is not None:
            import difflib
            diff_lines = list(difflib.unified_diff(
                result.original_content.splitlines(keepends=True),
                result.final_content.splitlines(keepends=True),
                fromfile=f"a/{request.target_file}",
                tofile=f"b/{request.target_file}",
                n=3,
            ))
            response_data["preview_diff"] = "".join(diff_lines)
            # Lines summary for token-conscious clients
            added = sum(
                1 for line in diff_lines
                if line.startswith("+") and not line.startswith("+++")
            )
            removed = sum(
                1 for line in diff_lines
                if line.startswith("-") and not line.startswith("---")
            )
            response_data["preview_summary"] = {
                "lines_added": added,
                "lines_removed": removed,
                "no_changes": (
                    result.original_content == result.final_content
                ),
            }

        if result.success:
            return create_success_response(response_data)
        else:
            # Edit pipeline ran but produced an unsatisfactory result.  This
            # is NOT a request-validation failure (the request was fine) — it
            # is a runtime outcome.  Return 200 + success_response carrying
            # the full failure analysis so the UI can render diagnostics
            # instead of a generic "API Error".
            failure_analysis = {
                "failure_stage": "unknown",
                "root_cause": "unknown",
                "suggested_fixes": [],
            }

            if not result.gemini_edit_success:
                failure_analysis["failure_stage"] = "llm_edit"
                # Most common cause: provider not configured / wrong key /
                # wrong model name.  Surface the actual exception message
                # collected by the pipeline.
                if result.gemini_errors:
                    raw = "; ".join(str(e).split("\n", 1)[0] for e in result.gemini_errors)[:300]
                    failure_analysis["root_cause"] = f"LLM call failed: {raw}"
                else:
                    failure_analysis["root_cause"] = (
                        "LLM call failed (no response). Check that the active "
                        "provider for the 'edit' role is configured and reachable."
                    )
                failure_analysis["suggested_fixes"] = [
                    "Verify the API key for the assigned provider on the Providers page",
                    "Click the Test button next to a provider to ping it directly",
                    "If you use a self-hosted gateway (e.g. http://127.0.0.1:2048/v1), "
                    "make sure the api_base field is set on that provider — otherwise "
                    "LiteLLM tries the public domain and gets 401 from your placeholder key",
                ]

            elif not result.format_success:
                failure_analysis["failure_stage"] = "guard_check"
                failure_analysis["root_cause"] = (
                    "Static analysis (Guard) reported ERROR-level issues even "
                    "after the review-role escalation pass."
                )
                failure_analysis["suggested_fixes"] = [
                    "Review the 'errors.format_errors' field for the specific tool reports",
                    "Check for syntax errors in the edit",
                    "Verify the edit follows proper code structure",
                ]

            elif result.quality_score < 0.6:
                failure_analysis["failure_stage"] = "quality_check"
                failure_analysis["root_cause"] = (
                    f"Quality score too low: {result.quality_score:.2f}"
                )
                failure_analysis["suggested_fixes"] = [
                    "Review edit instructions for clarity",
                    "Check if the target file has complex dependencies",
                ]

            response_data["failure_analysis"] = failure_analysis
            response_data["success"] = False
            return create_success_response(response_data)

    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Unexpected error in edit operation: {str(e)}",
            500,
            "UnexpectedError",
            {
                "target_file": (
                    request.target_file if "request" in locals() else "unknown"
                ),
                "exception_type": type(e).__name__,
                "full_error": str(e),
            },
            "EditPipeline",
            "edit_operation",
            settings.WORKING_DIR,
        )


@router.get("/edit/stats")
async def get_edit_stats():
    """Get edit pipeline statistics"""
    try:
        edit_pipeline = get_edit_pipeline()
        if not edit_pipeline:
            return create_error_response("Edit pipeline not initialized", 500)

        stats = edit_pipeline.get_stats()
        return create_success_response(stats)

    except Exception as e:
        return create_error_response(f"Failed to get edit stats: {str(e)}", 500)


@router.post("/read")
async def read_code_content(
    file_path: str = Query(..., description="Path to the file"),
    symbol_name: Optional[str] = Query(None, description="Symbol name to read"),
    occurrence: int = Query(
        1, description="Which occurrence of the symbol (default: 1)"
    ),
    start_line: Optional[str] = Query(
        None, description="Start line number (1-indexed)"
    ),
    end_line: Optional[str] = Query(None, description="End line number (inclusive)"),
    with_line_numbers: bool = Query(True, description="Include line numbers in output"),
    mode: str = Query(
        "full",
        description=(
            "Read mode: full (entire file), outline (signatures + first docstring line), "
            "symbols (symbol list only), diagnostics (lint issues only), imports (import lines only), "
            "relevant_chunks (top-K semantic chunks of this file vs `query`), "
            "tests (test files that likely cover this file)"
        ),
    ),
    query: Optional[str] = Query(
        None,
        description="Required when mode=relevant_chunks. Free-text search query "
        "scoped to this file's chunks.",
    ),
):
    """Read code content with multiple modes for token efficiency.

    Modes:
      - full: complete file content (default, backward-compatible)
      - outline: only function/class signatures + first docstring line (~90% token savings)
      - symbols: structured symbol list (name, kind, lines) — no code content
      - diagnostics: only ruff/eslint diagnostics for this file
      - imports: only import/require statements
      - relevant_chunks: semantic top-K chunks of this file vs `query`
      - tests: candidate test files for this file
    """
    # Defensive coercion: some clients (URLSearchParams in older JS code,
    # MCP wrappers) serialize Python None as the literal string "null" or
    # "undefined".  Treat those as if the param were omitted and parse the
    # remaining strings into ints.
    if isinstance(symbol_name, str) and symbol_name.lower() in {"null", "undefined", ""}:
        symbol_name = None

    # Sandbox check happens BEFORE mode dispatch so a hostile path can't
    # exercise any of the read modes (each of which would then call into
    # the workspace itself).  Wave 1, gap §13.
    from core.config import get_settings as _gs

    try:
        await validate_file_path(file_path, _gs().WORKING_DIR)
    except HTTPException as exc:
        return create_error_response(str(exc.detail), exc.status_code)

    # Handle non-full modes early — they don't need line range / symbol resolution
    if mode and mode.lower() not in ("full", ""):
        return await _read_mode_dispatch(file_path, mode.lower(), with_line_numbers, query)

    def _coerce_int(v: Optional[str], field: str):
        if v is None:
            return None, None
        if isinstance(v, str) and v.lower() in {"null", "undefined", ""}:
            return None, None
        try:
            return int(v), None
        except (TypeError, ValueError):
            return None, f"Invalid {field}: {v!r} (must be an integer)"

    start_line_int, err1 = _coerce_int(start_line, "start_line")
    if err1:
        return create_error_response(err1, 400)
    end_line_int, err2 = _coerce_int(end_line, "end_line")
    if err2:
        return create_error_response(err2, 400)
    start_line = start_line_int  # type: ignore[assignment]
    end_line = end_line_int      # type: ignore[assignment]
    try:
        search_engine = get_search_engine()
        settings = get_settings()

        if not search_engine:
            return create_detailed_error_response(
                "Search engine not initialized",
                500,
                "ServiceNotAvailable",
                {"service": "SearchEngine"},
                "SearchEngine",
                "initialization",
                settings.WORKING_DIR,
            )

        # Validate file path
        try:
            await validate_file_path(file_path, settings.WORKING_DIR)
        except HTTPException as e:
            return create_detailed_error_response(
                f"Invalid file path: {file_path}",
                e.status_code,
                "FilePathValidationError",
                {"file_path": file_path, "validation_error": e.detail},
                "SearchEngine",
                "file_validation",
                settings.WORKING_DIR,
            )

        # Validate line range parameters
        if start_line is not None and end_line is not None:
            if start_line < 1:
                return create_detailed_error_response(
                    f"Invalid start_line: {start_line}. Line numbers must be >= 1",
                    400,
                    "InvalidLineRange",
                    {"start_line": start_line, "end_line": end_line},
                    "SearchEngine",
                    "line_validation",
                    settings.WORKING_DIR,
                )

            if end_line < start_line:
                return create_detailed_error_response(
                    f"Invalid line range: end_line ({end_line}) < start_line ({start_line})",
                    400,
                    "InvalidLineRange",
                    {
                        "start_line": start_line,
                        "end_line": end_line,
                        "suggestion": "Ensure end_line >= start_line",
                    },
                    "SearchEngine",
                    "line_validation",
                    settings.WORKING_DIR,
                )

        # Validate occurrence parameter
        if occurrence < 1:
            return create_detailed_error_response(
                f"Invalid occurrence: {occurrence}. Must be >= 1",
                400,
                "InvalidOccurrence",
                {"occurrence": occurrence},
                "SearchEngine",
                "occurrence_validation",
                settings.WORKING_DIR,
            )

        try:
            result = await search_engine.read_symbol_content(
                file_path=file_path,
                symbol_name=symbol_name,
                occurrence=occurrence,
                start_line=start_line,
                end_line=end_line,
                with_line_numbers=with_line_numbers,
            )
        except Exception as read_error:
            return create_detailed_error_response(
                f"Failed to read content: {str(read_error)}",
                500,
                "ReadContentError",
                {
                    "file_path": file_path,
                    "symbol_name": symbol_name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "read_error": str(read_error),
                },
                "SearchEngine",
                "read_content",
                settings.WORKING_DIR,
            )

        if result.get("success"):
            return create_success_response(result)
        else:
            return create_detailed_error_response(
                result.get("error", "Unknown read error"),
                400,
                "ReadOperationFailed",
                {
                    "file_path": file_path,
                    "symbol_name": symbol_name,
                    "line_range": (
                        f"{start_line}-{end_line}" if start_line and end_line else None
                    ),
                    "occurrence": occurrence,
                    "with_line_numbers": with_line_numbers,
                },
                "SearchEngine",
                "read_operation",
                settings.WORKING_DIR,
            )

    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"Unexpected error in read operation: {str(e)}",
            500,
            "UnexpectedError",
            {
                "file_path": file_path,
                "exception_type": type(e).__name__,
                "full_error": str(e),
            },
            "SearchEngine",
            "read_operation",
            settings.WORKING_DIR,
        )


@router.get("/read/{file_path:path}")
async def read_file_content(
    file_path: str,
    start_line: Optional[int] = Query(None, description="Start line number"),
    end_line: Optional[int] = Query(None, description="End line number"),
    with_line_numbers: bool = Query(True, description="Include line numbers"),
):
    """Read file content with optional line range"""
    try:
        search_engine = get_search_engine()
        settings = get_settings()

        if not search_engine:
            return create_error_response("Search engine not initialized", 500)

        # Validate file path
        await validate_file_path(file_path, settings.WORKING_DIR)

        result = await search_engine.read_symbol_content(
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            with_line_numbers=with_line_numbers,
        )

        if result.get("success"):
            return create_success_response(result)
        else:
            return create_error_response(result.get("error", "Unknown error"), 400)

    except HTTPException:
        raise
    except Exception as e:
        return create_error_response(f"Read operation failed: {str(e)}", 500)


@router.post("/file")
async def file_operations(request: FileRequest):
    """Handle legacy file operations"""
    try:
        settings = get_settings()

        try:
            file_path = await validate_file_path(
                request.file_path, settings.WORKING_DIR
            )
        except HTTPException as e:
            return create_detailed_error_response(
                f"Invalid file path: {request.file_path}",
                e.status_code,
                "FilePathValidationError",
                {"file_path": request.file_path, "validation_error": e.detail},
                "FileOperations",
                "path_validation",
                settings.WORKING_DIR,
            )

        operation = request.operation.lower()

        if operation == "read":
            if not file_path.exists():
                return create_detailed_error_response(
                    f"File not found: {request.file_path}",
                    404,
                    "FileNotFound",
                    {
                        "file_path": request.file_path,
                        "resolved_path": str(file_path),
                    },
                    "FileOperations",
                    "read",
                    settings.WORKING_DIR,
                )

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Extract specific lines if requested
                if request.start_line is not None and request.end_line is not None:
                    lines = content.split("\n")
                    if request.start_line < 1 or request.end_line < request.start_line:
                        return create_detailed_error_response(
                            f"Invalid line range: {request.start_line}-{request.end_line}",
                            400,
                            "InvalidLineRange",
                            {
                                "start_line": request.start_line,
                                "end_line": request.end_line,
                                "total_lines": len(lines),
                            },
                            "FileOperations",
                            "line_range_validation",
                            settings.WORKING_DIR,
                        )

                    if request.end_line > len(lines):
                        return create_detailed_error_response(
                            f"End line {request.end_line} exceeds file length {len(lines)}",
                            400,
                            "LineRangeExceeded",
                            {
                                "end_line": request.end_line,
                                "file_length": len(lines),
                                "suggestion": f"Use end_line <= {len(lines)}",
                            },
                            "FileOperations",
                            "line_range_check",
                            settings.WORKING_DIR,
                        )

                    selected_lines = lines[request.start_line - 1 : request.end_line]
                    content = "\n".join(selected_lines)

                return create_success_response(
                    {
                        "operation": "read",
                        "file_path": request.file_path,
                        "content": content,
                        "total_lines": len(content.split("\n")),
                        "line_range": (
                            f"{request.start_line}-{request.end_line}"
                            if request.start_line
                            else "full_file"
                        ),
                    }
                )
            except Exception as e:
                return create_detailed_error_response(
                    f"Read error: {str(e)}",
                    500,
                    "FileReadError",
                    {"file_path": request.file_path, "error_details": str(e)},
                    "FileOperations",
                    "file_read",
                    settings.WORKING_DIR,
                )

        elif operation == "write":
            if not request.content:
                return create_error_response(
                    "Content required for write operation", 400
                )

            # Use intelligent write pipeline if available
            write_pipeline = get_write_pipeline()
            if write_pipeline:
                result = await write_pipeline.process_write(
                    content=request.content,
                    file_path=request.file_path,
                    save_to_file=True,
                )

                if result.success:
                    return create_success_response(
                        {
                            "operation": "write",
                            "file_path": request.file_path,
                            "summary": result.summary,
                            "quality_score": result.quality_score,
                        }
                    )
                else:
                    return create_error_response(
                        f"Write failed: {'; '.join(result.errors)}", 422
                    )
            else:
                # Fallback when WritePipeline isn't wired up: still go
                # through PatchManager so the user gets a snapshot +
                # rollback. This block historically used a raw
                # `open(..., "w")` write — that left no audit trail and
                # silently broke the project's safety contract.
                from omnicode_core.edit.patch import PatchManager
                pm = PatchManager(settings.WORKING_DIR)
                result = pm.apply_patch(
                    file_path=request.file_path,
                    new_content=request.content,
                    source="file_operations:write_fallback",
                )
                if not result.success:
                    return create_error_response(
                        f"Write failed: {result.message}", 500
                    )
                return create_success_response(
                    {
                        "operation": "write",
                        "file_path": request.file_path,
                        "message": "File written (without intelligent processing)",
                        "edit_session_id": result.session_id,
                        "rollback_available": result.rollback_available,
                        "lines_added": result.lines_added,
                        "lines_removed": result.lines_removed,
                    }
                )

        elif operation == "create":
            if file_path.exists():
                return create_detailed_error_response(
                    f"File already exists: {request.file_path}",
                    409,
                    "FileExists",
                    {
                        "file_path": request.file_path,
                        "resolved_path": str(file_path),
                        "suggestion": "Use 'write' operation to overwrite or choose a different filename",
                    },
                    "FileOperations",
                    "create",
                    settings.WORKING_DIR,
                )

            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                content = request.content or ""

                # Route through PatchManager so even fresh-file creates
                # land in EditSessions and get a rollback hook (the
                # snapshot will simply be empty, which apply_patch
                # handles cleanly).
                from omnicode_core.edit.patch import PatchManager
                pm = PatchManager(settings.WORKING_DIR)
                result = pm.apply_patch(
                    file_path=request.file_path,
                    new_content=content,
                    source="file_operations:create",
                )
                if not result.success:
                    return create_detailed_error_response(
                        f"Create error: {result.message}",
                        500,
                        "FileCreateError",
                        {"file_path": request.file_path},
                        "FileOperations",
                        "create",
                        settings.WORKING_DIR,
                    )

                return create_success_response(
                    {
                        "operation": "create",
                        "file_path": request.file_path,
                        "message": "File created successfully",
                        "content_length": len(content),
                        "lines_written": len(content.split("\n")) if content else 0,
                        "edit_session_id": result.session_id,
                        "rollback_available": result.rollback_available,
                    }
                )
            except Exception as e:
                return create_detailed_error_response(
                    f"Create error: {str(e)}",
                    500,
                    "FileCreateError",
                    {"file_path": request.file_path, "error_details": str(e)},
                    "FileOperations",
                    "file_create",
                    settings.WORKING_DIR,
                )

        elif operation == "delete":
            if not file_path.exists():
                return create_error_response(
                    f"File not found: {request.file_path}", 404
                )

            file_path.unlink()
            return create_success_response(
                {
                    "operation": "delete",
                    "file_path": request.file_path,
                    "message": "File deleted successfully",
                }
            )

        else:
            return create_error_response(
                f"Unsupported file operation: {operation}", 400
            )

    except HTTPException:
        raise
    except Exception as e:
        settings = get_settings()
        return create_detailed_error_response(
            f"File operation failed: {str(e)}",
            500,
            "FileOperationError",
            {
                "operation": request.operation,
                "file_path": request.file_path,
                "exception_type": type(e).__name__,
            },
            "FileOperations",
            request.operation,
            settings.WORKING_DIR,
        )


# =============================================================================
# Multi-mode read dispatch (outline / symbols / diagnostics / imports)
# =============================================================================

async def _read_mode_dispatch(file_path: str, mode: str, with_line_numbers: bool, query: Optional[str] = None):
    """Handle non-full read modes that return structured, token-efficient output.

    These modes are designed to give AI agents the minimum context they need
    without reading the entire file — typically saving 50-90% of tokens.
    """
    import os

    from core import get_search_engine
    from core.config import get_settings
    from utils import create_error_response, create_success_response, validate_file_path

    settings = get_settings()

    try:
        await validate_file_path(file_path, settings.WORKING_DIR)
    except Exception as e:
        return create_error_response(f"Invalid file path: {file_path} — {e}", 400)

    full_path = os.path.abspath(os.path.join(settings.WORKING_DIR, file_path))
    if not os.path.exists(full_path):
        return create_error_response(f"File not found: {file_path}", 404)

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as exc:
        return create_error_response(f"Cannot read file: {exc}", 500)

    lines = content.splitlines()
    total_lines = len(lines)

    # -------------------------------------------------------------------------
    # MODE: outline — signatures + first docstring line
    # -------------------------------------------------------------------------
    if mode == "outline":
        # Special-case Markdown: tree-sitter doesn't ship a default Markdown
        # grammar in our env, so the symbol extractor returns []. Pull
        # headings out with a tiny regex so the agent at least gets a TOC.
        if file_path.lower().endswith((".md", ".markdown", ".mdx")):
            import re as _re

            outline_items = []
            heading_re = _re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
            in_fence = False
            for i, ln in enumerate(lines, 1):
                stripped = ln.lstrip()
                if stripped.startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue
                m = heading_re.match(ln)
                if m:
                    level = len(m.group(1))
                    title = m.group(2).strip()
                    parent = None
                    # Crude parent inference: previous heading at level-1.
                    for prev in reversed(outline_items):
                        if prev.get("_level", 1) < level:
                            parent = prev["name"]
                            break
                    outline_items.append({
                        "name": title,
                        "kind": f"h{level}",
                        "signature": ln.rstrip(),
                        "doc": "",
                        "lines": [i, i],
                        "parent": parent,
                        "_level": level,
                    })
            # Strip the internal _level helper before returning.
            for item in outline_items:
                item.pop("_level", None)
            return create_success_response({
                "file": file_path,
                "mode": "outline",
                "total_lines": total_lines,
                "language": "markdown",
                "symbols": outline_items,
                "symbol_count": len(outline_items),
            })

        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Search engine not initialized", 500)

        symbols_data = await search_engine.list_symbols_in_file(file_path)
        symbols = symbols_data.get("symbols") or []

        outline_items = []
        for sym in symbols:
            name = sym.get("name", "?")
            stype = sym.get("type", "symbol")
            sline = sym.get("line_start", 1)
            eline = sym.get("line_end", sline)
            parent = sym.get("parent")

            # Extract signature (first non-empty line of the symbol)
            signature = ""
            if 1 <= sline <= total_lines:
                for ln in lines[sline - 1: min(eline, sline + 3)]:
                    stripped = ln.strip()
                    if stripped:
                        signature = stripped[:200]
                        break

            # Extract first docstring line (Python only for now)
            docstring_line = ""
            if sline < total_lines:
                for ln in lines[sline: min(eline, sline + 5)]:
                    stripped = ln.strip()
                    if stripped.startswith(('"""', "'''")):
                        doc = stripped.strip("\"'").strip()
                        if doc:
                            docstring_line = doc[:150]
                        break
                    elif stripped.startswith(('#', '//')):
                        docstring_line = stripped.lstrip('#/ ').strip()[:150]
                        break

            outline_items.append({
                "name": name,
                "kind": stype,
                "signature": signature,
                "doc": docstring_line,
                "lines": [sline, eline],
                "parent": parent,
            })

        return create_success_response({
            "file": file_path,
            "mode": "outline",
            "total_lines": total_lines,
            "language": symbols_data.get("language", ""),
            "symbols": outline_items,
            "symbol_count": len(outline_items),
        })

    # -------------------------------------------------------------------------
    # MODE: symbols — just the symbol list (even more compact than outline)
    # -------------------------------------------------------------------------
    if mode == "symbols":
        search_engine = get_search_engine()
        if not search_engine:
            return create_error_response("Search engine not initialized", 500)

        symbols_data = await search_engine.list_symbols_in_file(file_path)
        symbols = symbols_data.get("symbols") or []

        return create_success_response({
            "file": file_path,
            "mode": "symbols",
            "total_lines": total_lines,
            "language": symbols_data.get("language", ""),
            "symbols": [
                {
                    "name": s.get("name"),
                    "kind": s.get("type"),
                    "lines": [s.get("line_start", 1), s.get("line_end", 1)],
                    "parent": s.get("parent"),
                }
                for s in symbols
            ],
            "symbol_count": len(symbols),
        })

    # -------------------------------------------------------------------------
    # MODE: imports — only import/require lines
    #
    # Uses the AST when possible so multi-line imports come back with their
    # closing parenthesis intact and "import" appearing inside a string
    # literal (e.g. `# from x import y` in a docstring) is not misclassified.
    # Falls back to the line-prefix heuristic when the language has no
    # tree-sitter parser (.md, .yaml, ...).
    # -------------------------------------------------------------------------
    if mode == "imports":
        from core import get_ast_parser

        search_engine = get_search_engine()
        language = (
            search_engine._guess_language(file_path)
            if search_engine is not None
            else ""
        )

        import_lines: list[dict] = []
        ast_parser = get_ast_parser()
        ast_used = False

        if ast_parser is not None and language:
            try:
                imports = ast_parser.extract_imports(content, language) or []
            except Exception:
                imports = []

            for imp in imports:
                if not isinstance(imp, dict):
                    continue
                start = int(imp.get("line") or imp.get("line_start") or 0)
                if start <= 0:
                    continue
                # The raw text from extract_imports already covers the
                # whole node (start_byte..end_byte) so multi-line imports
                # carry their trailing ')' / '}' along.
                raw = imp.get("raw") or imp.get("module") or ""
                if not raw and 1 <= start <= total_lines:
                    raw = lines[start - 1]
                # Compute end line by counting newlines in the raw block
                end_line_num = start + max(0, raw.count("\n"))
                import_lines.append(
                    {
                        "line": start,
                        "line_end": end_line_num,
                        "text": raw,
                    }
                )
                ast_used = True

        if not ast_used:
            # Heuristic fallback for non-AST-supported languages.
            for i, ln in enumerate(lines, 1):
                stripped = ln.strip()
                if (
                    stripped.startswith(("import ", "from ", "require(", "require ("))
                    or (
                        stripped.startswith(("const ", "let ", "var "))
                        and " require(" in stripped
                    )
                    or stripped.startswith("#include")
                    or stripped.startswith("use ")  # rust
                    or stripped.startswith("package ")  # go / java
                ):
                    import_lines.append(
                        {"line": i, "line_end": i, "text": stripped}
                    )

        if with_line_numbers:
            rendered_parts: list[str] = []
            for il in import_lines:
                head = f"{il['line']:>4} | "
                txt = il["text"]
                if "\n" in txt:
                    # Indent continuation lines so the column-alignment
                    # stays consistent.
                    pad = " " * len(head)
                    parts = txt.split("\n")
                    rendered_parts.append(
                        head + parts[0] + "\n" + "\n".join(pad + p for p in parts[1:])
                    )
                else:
                    rendered_parts.append(head + txt)
            rendered = "\n".join(rendered_parts)
        else:
            rendered = "\n".join(il["text"] for il in import_lines)

        return create_success_response({
            "file": file_path,
            "mode": "imports",
            "total_lines": total_lines,
            "language": language,
            "imports": import_lines,
            "import_count": len(import_lines),
            "content": rendered,
            "ast_used": ast_used,
        })

    # -------------------------------------------------------------------------
    # MODE: diagnostics — run guard checks on this file
    #
    # Filters out mypy info-level "note" lines (which add noise without
    # being actionable: e.g. "by default the bodies of untyped
    # functions are not checked"). Errors and warnings are kept.
    # -------------------------------------------------------------------------
    if mode == "diagnostics":
        try:
            from omnicode.guard import ProactiveGuard
            from omnicode.guard.models import GuardResult, IssueSeverity

            guard = ProactiveGuard()
            result = await guard.check(file_path)

            issues_out: list[dict] = []
            tools_run: list[str] = []
            tools_skipped: list[str] = []

            if isinstance(result, GuardResult):
                tools_run = list(result.tools_run or [])
                tools_skipped = list(result.tools_skipped or [])
                for issue in result.issues or []:
                    sev = (
                        issue.severity.value
                        if isinstance(issue.severity, IssueSeverity)
                        else str(issue.severity)
                    )
                    tool = (issue.tool or "").lower()
                    # Drop mypy info-level notes — they're advisory, not actionable.
                    if tool == "mypy" and sev == "info":
                        continue
                    issues_out.append(
                        {
                            "tool": issue.tool,
                            "code": issue.code,
                            "severity": sev,
                            "message": issue.message,
                            "line": issue.line,
                            "column": issue.column,
                            "file_path": issue.file_path,
                        }
                    )

            return create_success_response({
                "file": file_path,
                "mode": "diagnostics",
                "total_lines": total_lines,
                "diagnostics": issues_out,
                "diagnostic_count": len(issues_out),
                "tools_run": tools_run,
                "tools_skipped": tools_skipped,
            })
        except Exception as exc:
            return create_success_response({
                "file": file_path,
                "mode": "diagnostics",
                "total_lines": total_lines,
                "diagnostics": [],
                "diagnostic_count": 0,
                "note": f"Guard check unavailable: {exc}",
            })

    # -------------------------------------------------------------------------
    # MODE: relevant_chunks — semantic top-K chunks of THIS file vs a query.
    # Useful when the editor cares about a specific aspect of a long file
    # without reading the whole thing. Implemented by:
    #   1. running the indexed semantic search restricted to this file
    #   2. returning the matching chunks ranked by score
    #
    # Falls back to a clear error when ``query`` is empty (we don't try to
    # guess intent — the caller must opt in).
    # -------------------------------------------------------------------------
    if mode == "relevant_chunks":
        if not query or not query.strip():
            return create_error_response(
                "mode=relevant_chunks requires a `query` parameter.",
                400,
            )

        from omnicode.search.models import SearchRequest

        engine = get_search_engine()
        if engine is None:
            return create_error_response("Search engine not initialized", 500)

        # Don't pass ``file_pattern`` to the engine: most backends treat it
        # as an fnmatch glob, and a full path with slashes won't match
        # anything. We over-fetch and then strictly filter on this side.
        req = SearchRequest(
            query=query.strip(),
            search_type="semantic",
            max_results=50,
        )
        results = await engine.search(req)

        # Filter strictly to this file. Compare both forward-slash forms
        # (handles the case where the indexer normalised one way and the
        # caller passed the other).
        normalised = file_path.replace("\\", "/").lstrip("./")
        filtered = []
        for r in results:
            rp = (getattr(r, "file_path", "") or "").replace("\\", "/").lstrip("./")
            if rp == normalised or rp.endswith("/" + normalised) or normalised.endswith("/" + rp):
                filtered.append(r)

        return create_success_response(
            {
                "file": file_path,
                "mode": "relevant_chunks",
                "query": query.strip(),
                "total_lines": total_lines,
                "result_count": len(filtered),
                "chunks": [
                    {
                        "symbol_name": getattr(r, "symbol_name", ""),
                        "chunk_type": getattr(r, "chunk_type", ""),
                        "line_start": getattr(r, "line_start", None),
                        "line_end": getattr(r, "line_end", None),
                        "signature": getattr(r, "signature", ""),
                        "docstring": getattr(r, "docstring", ""),
                        "score": getattr(r, "relevance_score", 0.0),
                        "why_matched": getattr(r, "why_matched", []),
                    }
                    for r in filtered
                ],
            }
        )

    # -------------------------------------------------------------------------
    # MODE: tests — list test files that cover this file's symbols.
    # Quick heuristic: filename + tree-walk; combined with the symbol
    # index from list_symbols_in_file. Falls back to filename-only when
    # the symbol index is empty.
    # -------------------------------------------------------------------------
    if mode == "tests":
        from omnicode.config.settings import get_settings as _gs

        wd = _gs().WORKING_DIR
        # Filename-based candidates: tests/test_<basename>.py + co-located
        # *.test.ts / *.spec.ts.
        import os as _os

        base = _os.path.splitext(_os.path.basename(file_path))[0]
        candidates: list[str] = []
        for root, dirs, files in _os.walk(wd):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".data", ".venv"}]
            for f in files:
                fl = f.lower()
                if (
                    (fl.startswith("test_") and base.lower() in fl)
                    or fl == f"test_{base.lower()}.py"
                    or fl == f"{base.lower()}.test.ts"
                    or fl == f"{base.lower()}.test.js"
                    or fl == f"{base.lower()}.spec.ts"
                ):
                    rel = _os.path.relpath(_os.path.join(root, f), wd).replace("\\", "/")
                    candidates.append(rel)

        # Plus call-graph based suggestions for each top-level symbol.
        graph_suggestions: list[dict] = []
        try:
            from omnicode_core.graph.impact import ImpactAnalyzer

            analyser = ImpactAnalyzer(wd)
            search_engine = get_search_engine()
            if search_engine is not None:
                symbols_data = await search_engine.list_symbols_in_file(file_path)
                top_symbols = [
                    s.get("name") for s in (symbols_data.get("symbols") or [])[:5] if s.get("name")
                ]
                for sym in top_symbols:
                    sug = await analyser.suggest_related_tests(symbol=sym)
                    if "error" not in sug:
                        graph_suggestions.append(
                            {
                                "symbol": sym,
                                "test_files": sug.get("test_files", []),
                                "suggested_commands": sug.get("suggested_commands", []),
                            }
                        )
        except Exception:
            # Best-effort — if the graph isn't built yet we still return
            # the filename-based suggestions.
            pass

        # Deduplicate the flat candidate list.
        candidates = sorted(set(candidates))
        return create_success_response(
            {
                "file": file_path,
                "mode": "tests",
                "total_lines": total_lines,
                "candidate_test_files": candidates,
                "graph_suggestions": graph_suggestions,
                "suggested_commands": [f"pytest {t}" for t in candidates[:5]],
                "note": (
                    "Combines filename heuristics with call-graph "
                    "reachability when the graph is available."
                ),
            }
        )

    # -------------------------------------------------------------------------
    # Unknown mode — fall back to full
    # -------------------------------------------------------------------------
    return create_error_response(
        f"Unknown read mode: '{mode}'. Valid modes: full, outline, symbols, "
        "imports, diagnostics, relevant_chunks, tests",
        400,
    )
