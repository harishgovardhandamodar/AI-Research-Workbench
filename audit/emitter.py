"""AuditEmitter: async, low-overhead bridge into a LocalAuditStore.

The agent loop emits audit events by awaiting :meth:`AuditEmitter.emit`; the
emitter pushes the event onto an internal asyncio queue and a background
writer task drains it, so the call site pays only queue-append cost. Writes
that fail are logged and dropped (never crash the agent turn).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .models import AuditEvent
from .store import LocalAuditStore

log = logging.getLogger("fox.audit")

EmitHook = Callable[[AuditEvent], Awaitable[None]]


class AuditEmitter:
    def __init__(self, store: LocalAuditStore | None = None,
                 on_event: EmitHook | None = None,
                 max_queue: int = 4096):
        self.store = store
        self.on_event = on_event
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self._started = False

    # ------------------------------------------------------------------ run ---
    def start(self):
        if self._started:
            return
        self._started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._drain())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._started = False

    async def emit(self, event: AuditEvent | dict) -> AuditEvent | None:
        """Queue an event for persistence. Returns the fully-hashed event."""
        ev = event if isinstance(event, AuditEvent) else AuditEvent.from_dict(event)
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            log.warning("audit queue full; dropping event %s", ev.event_id)
            return None
        if self.on_event is not None:
            try:
                await self.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("audit on_event hook failed")
        return ev

    async def flush(self) -> int:
        """Drain the queue to the store synchronously (best-effort)."""
        if self.store is None:
            return 0
        n = 0
        while not self._queue.empty():
            ev = self._queue.get_nowait()
            try:
                self.store.append(ev)
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("audit store append failed")
        return n

    # ------------------------------------------------------------------ drain
    async def _drain(self):
        try:
            while True:
                ev = await self._queue.get()
                if self.store is not None:
                    try:
                        self.store.append(ev)
                    except Exception:  # noqa: BLE001
                        log.exception("audit store append failed")
        except asyncio.CancelledError:
            # Best-effort final flush of anything still queued.
            if self.store is not None:
                while not self._queue.empty():
                    ev = self._queue.get_nowait()
                    try:
                        self.store.append(ev)
                    except Exception:  # noqa: BLE001
                        break
            raise
