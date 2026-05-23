"""
Logging and monitoring endpoints
Handles system logs, performance metrics, and monitoring
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core import (
    get_edit_pipeline,
    get_git_manager,
    get_memory_manager,
    get_search_engine,
    get_write_pipeline,
)
from schemas.common import LogLevel, SystemLog
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/logs", tags=["logs"])

logger = logging.getLogger(__name__)


# Global log storage
system_logs: List[SystemLog] = []


def add_system_log(
    level: LogLevel, component: str, message: str, details: Optional[dict] = None
) -> None:
    """Add a log entry to the system logs"""
    log_entry = SystemLog(
        timestamp=datetime.now(),
        level=level,
        component=component,
        message=message,
        details=details or {},
    )
    system_logs.append(log_entry)

    # Keep only last 1000 logs
    if len(system_logs) > 1000:
        system_logs.pop(0)


@router.get("")
async def get_system_logs(
    level: Optional[LogLevel] = Query(None, description="Filter by log level"),
    component: Optional[str] = Query(None, description="Filter by component"),
    limit: int = Query(100, description="Maximum number of logs to return", le=1000),
    since: Optional[datetime] = Query(
        None, description="Get logs since this timestamp"
    ),
):
    """Get system logs with filtering options"""
    try:
        filtered_logs = system_logs.copy()

        # Apply filters
        if level:
            filtered_logs = [log for log in filtered_logs if log.level == level]

        if component:
            filtered_logs = [
                log
                for log in filtered_logs
                if log.component.lower() == component.lower()
            ]

        if since:
            filtered_logs = [log for log in filtered_logs if log.timestamp >= since]

        # Sort by timestamp (newest first) and limit
        filtered_logs.sort(key=lambda x: x.timestamp, reverse=True)
        filtered_logs = filtered_logs[:limit]

        return create_success_response(
            {
                "logs": [
                    {
                        "timestamp": log.timestamp.isoformat(),
                        "level": log.level,
                        "component": log.component,
                        "message": log.message,
                        "details": log.details,
                    }
                    for log in filtered_logs
                ],
                "total_logs": len(system_logs),
                "filtered_count": len(filtered_logs),
            }
        )

    except Exception as e:
        return create_error_response(f"Failed to get logs: {str(e)}", 500)


@router.delete("")
async def clear_system_logs():
    """Clear all system logs"""
    try:
        global system_logs
        system_logs.clear()

        add_system_log(LogLevel.INFO, "system", "System logs cleared via API")

        return create_success_response(
            {
                "message": "System logs cleared successfully",
                "cleared_at": datetime.now().isoformat(),
            }
        )

    except Exception as e:
        return create_error_response(f"Failed to clear logs: {str(e)}", 500)


@router.get("/monitoring/performance")
async def get_performance_metrics():
    """Get system performance metrics"""
    try:
        import time

        import psutil

        # Basic system metrics
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        # Application-specific metrics
        process = psutil.Process()
        app_memory = process.memory_info().rss / 1024 / 1024  # MB

        metrics = {
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_gb": memory.available / 1024 / 1024 / 1024,
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / 1024 / 1024 / 1024,
            },
            "application": {
                "memory_usage_mb": app_memory,
                "uptime_seconds": time.time() - process.create_time(),
                "active_connections": (
                    len(process.connections()) if hasattr(process, "connections") else 0
                ),
            },
            "services": {
                "search_engine_active": get_search_engine() is not None,
                "memory_system_active": get_memory_manager() is not None,
                "git_manager_active": get_git_manager() is not None,
                "write_pipeline_active": get_write_pipeline() is not None,
                "edit_pipeline_active": get_edit_pipeline() is not None,
            },
            "timestamp": datetime.now().isoformat(),
        }

        return create_success_response(metrics)

    except Exception as e:
        return create_error_response(
            f"Failed to get performance metrics: {str(e)}", 500
        )


# ----------------------------------------------------------------------------
# STAGE 9.9 — Real-time log stream over WebSocket
# ----------------------------------------------------------------------------
@router.websocket("/stream")
async def websocket_log_stream(websocket: WebSocket):
    """Push every log record to the client in real time as it's emitted.

    Protocol:
        * Server immediately sends a backlog frame:
              {"type": "backfill", "records": [...]}
        * Then, for every new record:
              {"type": "log", "record": {...}}
        * Server respects ``ping`` text frames; sends ``{"type":"pong"}``.
        * On disconnect the subscriber is cleaned up automatically.
    """
    await websocket.accept()
    from core.log_stream import get_broker, get_handler

    handler = get_handler()
    broker = get_broker()
    if handler is None or broker is None:
        await websocket.send_json({
            "type": "error",
            "message": "Log streaming not initialised on this server.",
        })
        await websocket.close()
        return

    sub = await broker.subscribe()
    # 1) Backfill recent buffer so the UI feels alive immediately.
    try:
        recent = handler.recent(limit=80)
        await websocket.send_json({"type": "backfill", "records": recent})
    except Exception as exc:  # pragma: no cover
        logger.debug("backfill send failed: %s", exc)

    async def _receiver() -> None:
        # Listen for the optional ping/close frames so we exit cleanly.
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                raise
            except Exception:
                raise
            if msg == "ping":
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    return

    async def _sender() -> None:
        while True:
            payload = await sub.get()
            try:
                await websocket.send_json({"type": "log", "record": payload})
            except WebSocketDisconnect:
                return
            except Exception:
                return

    sender_task = asyncio.create_task(_sender())
    receiver_task = asyncio.create_task(_receiver())
    try:
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        receiver_task.cancel()
        await broker.unsubscribe(sub)
        try:
            await websocket.close()
        except Exception:
            pass


# Export add_system_log for use by other routers
__all__ = ["router", "add_system_log"]
