"""
Real-time log stream broker (STAGE 9.9)
=======================================
Bridges Python's ``logging`` module to any number of WebSocket subscribers
without blocking the loggers.

Design notes
------------
* A single :class:`LogBroker` instance lives at module scope.
* Python loggers push log records onto a thread-safe queue (the
  :class:`StreamLogHandler` lives outside the asyncio loop, so it must be
  thread-safe).
* An asyncio task (started lazily on the first ``subscribe()``) drains the
  queue and fans the records out to every WebSocket subscriber.
* Slow / dead subscribers are isolated: a per-subscriber bounded queue
  drops the oldest record on overflow rather than blocking the broker.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StreamLogHandler — sits inside Python's logging tree
# ---------------------------------------------------------------------------
class StreamLogHandler(logging.Handler):
    """A logging.Handler that pushes JSON-serialisable records onto a queue.

    Lives in plain (non-asyncio) thread-land because Python loggers can be
    invoked from any thread.  The broker pulls from the queue inside the
    asyncio event loop.
    """

    def __init__(self, max_buffer: int = 5000) -> None:
        super().__init__()
        # Bounded queue — if subscribers fall behind, the oldest record is
        # dropped to bound memory.
        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_buffer)
        # Ring buffer of recent records so newcomers can backfill on connect.
        self._recent: List[Dict[str, Any]] = []
        self._recent_lock = threading.Lock()
        self._max_recent = 200

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = self._serialise(record)
        except Exception:  # never let logging crash the app
            return
        # Snapshot for backfill
        with self._recent_lock:
            self._recent.append(payload)
            if len(self._recent) > self._max_recent:
                del self._recent[: len(self._recent) - self._max_recent]
        # Try to enqueue — drop oldest if full
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except queue.Empty:
                pass

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._recent_lock:
            return list(self._recent[-limit:])

    @staticmethod
    def _serialise(record: logging.LogRecord) -> Dict[str, Any]:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        return {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }


# ---------------------------------------------------------------------------
# LogBroker — fans records out to WebSocket subscribers
# ---------------------------------------------------------------------------
class LogBroker:
    """Single-process broker between StreamLogHandler and WebSockets."""

    def __init__(self, handler: StreamLogHandler) -> None:
        self.handler = handler
        self._subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def _ensure_pump(self) -> None:
        """Lazily start the queue-drain task on first use."""
        if self._task is None or self._task.done():
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._pump())

    async def _pump(self) -> None:
        """Drain the threadsafe queue and fan out to subscribers."""
        loop = asyncio.get_running_loop()
        while True:
            # ``queue.get`` is blocking; offload to the default executor so we
            # don't block the asyncio loop.
            try:
                payload = await loop.run_in_executor(None, self.handler.queue.get)
            except RuntimeError:
                return
            await self._broadcast(payload)

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        if not self._subscribers:
            return
        async with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest to make room
                try:
                    sub.get_nowait()
                    sub.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        """Register a new subscriber. Returns its bounded queue."""
        await self._ensure_pump()
        sub: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.add(sub)
        return sub

    async def unsubscribe(self, sub: "asyncio.Queue[Dict[str, Any]]") -> None:
        async with self._lock:
            self._subscribers.discard(sub)


# ---------------------------------------------------------------------------
# Module-level singletons + bootstrap helpers
# ---------------------------------------------------------------------------
_handler: Optional[StreamLogHandler] = None
_broker: Optional[LogBroker] = None


def install(level: int = logging.INFO) -> StreamLogHandler:
    """Install the streaming handler on the root logger (idempotent)."""
    global _handler, _broker
    if _handler is not None:
        return _handler
    _handler = StreamLogHandler()
    _handler.setLevel(level)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    _handler.setFormatter(formatter)
    logging.getLogger().addHandler(_handler)
    _broker = LogBroker(_handler)
    return _handler


def get_handler() -> Optional[StreamLogHandler]:
    return _handler


def get_broker() -> Optional[LogBroker]:
    return _broker


def reset() -> None:
    """Test helper — uninstall the handler and drop the broker."""
    global _handler, _broker
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _handler = None
    _broker = None


__all__ = [
    "StreamLogHandler",
    "LogBroker",
    "install",
    "get_handler",
    "get_broker",
    "reset",
]
