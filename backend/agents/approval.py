"""Approval broker: coordinates permission requests between the agent loop and the
WebSocket client. The coordinator awaits a decision; the client resolves it."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable
from uuid import uuid4


class ApprovalBroker:
    def __init__(self, emit: Callable[[str, dict], Awaitable[None]]):
        self.emit = emit
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, kind: str, command: str, reason: str) -> bool:
        rid = uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self.emit("approval_request", {
            "request_id": rid, "kind": kind, "command": command, "reason": reason,
        })
        try:
            return bool(await asyncio.wait_for(fut, timeout=300))
        except asyncio.TimeoutError:
            return False

    def resolve(self, request_id: str, decision: bool):
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(decision)
