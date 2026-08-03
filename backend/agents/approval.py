"""Approval broker: coordinates permission requests between the agent loop and the
WebSocket client. The coordinator awaits a decision; the client resolves it.

A decision carries a `temporary` flag: a temporary approval applies to the
current request only and is NOT remembered, so the next similar request still
prompts (the "ask" is never silenced). A non-temporary approval is persisted as
a grant and remembered.

Every decision (allow / deny / temporary / timeout) is appended to the
project's approval audit log when a store is provided.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable
from uuid import uuid4


class ApprovalBroker:
    def __init__(self, emit: Callable[[str, dict], Awaitable[None]],
                 store=None, timeout: float = 300.0):
        self.emit = emit
        self.store = store
        self.timeout = timeout
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, kind: str, command: str, reason: str) -> tuple[bool, bool]:
        """Ask the user for approval.

        Returns (approved: bool, temporary: bool). On timeout the request is
        treated as denied (approved=False) and a notice is emitted.
        """
        rid = uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self.emit("approval_request", {
            "request_id": rid, "kind": kind, "command": command, "reason": reason,
        })
        try:
            res = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._log(kind, command, "timeout", False)
            try:
                await self.emit("approval_result", {
                    "request_id": rid, "decision": "timeout",
                    "kind": kind, "command": command, "reason": reason,
                })
            except Exception:  # noqa: BLE001
                pass
            return False, False
        decision = bool(res.get("decision", False))
        temporary = bool(res.get("temporary", False))
        self._log(kind, command, "allow" if decision else "deny", temporary)
        return decision, temporary

    def _log(self, kind: str, command: str, decision: str, temporary: bool):
        if self.store is None:
            return
        try:
            self.store.log_approval(kind, command, decision, temporary)
        except Exception:  # noqa: BLE001
            pass

    def resolve(self, request_id: str, decision: bool, temporary: bool = False):
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result({"decision": bool(decision), "temporary": bool(temporary)})

    def reject_all(self):
        """Resolve every pending request as denied (e.g. the client disconnected)."""
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result({"decision": False, "temporary": False})
        self._pending.clear()
