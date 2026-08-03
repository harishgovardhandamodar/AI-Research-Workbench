"""Approval broker: coordinates permission requests between the agent loop and the
WebSocket client. The coordinator awaits a decision; the client resolves it.

A decision carries a `temporary` flag: a temporary approval applies to the
current request only and is NOT remembered, so the next similar request still
prompts (the "ask" is never silenced). A non-temporary approval is persisted as
a grant and remembered.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable
from uuid import uuid4


class ApprovalBroker:
    def __init__(self, emit: Callable[[str, dict], Awaitable[None]]):
        self.emit = emit
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, kind: str, command: str, reason: str) -> tuple[bool, bool]:
        """Ask the user for approval.

        Returns (approved: bool, temporary: bool). On timeout the request is
        treated as denied (approved=False).
        """
        rid = uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self.emit("approval_request", {
            "request_id": rid, "kind": kind, "command": command, "reason": reason,
        })
        try:
            res = await asyncio.wait_for(fut, timeout=300)
        except asyncio.TimeoutError:
            return False, False
        return bool(res.get("decision", False)), bool(res.get("temporary", False))

    def resolve(self, request_id: str, decision: bool, temporary: bool = False):
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result({"decision": bool(decision), "temporary": bool(temporary)})
