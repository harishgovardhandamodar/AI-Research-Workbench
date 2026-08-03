"""Approval broker: timeout, resolve, reject_all, and audit logging."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.agents.approval import ApprovalBroker
from backend.store import ProjectStore


class TestApprovalBroker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.events = []

    async def _emit(self, t: str, p: dict):
        self.events.append((t, p))

    async def test_timeout_returns_denied_and_logs(self):
        broker = ApprovalBroker(self._emit, store=self.store, timeout=0.2)
        ok, temporary = await broker.request("run_shell", "rm -rf /", "test")
        self.assertFalse(ok)
        self.assertFalse(temporary)
        log = self.store.list_approvals()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["decision"], "timeout")
        self.assertTrue(any(t == "approval_result" and p["decision"] == "timeout"
                            for t, p in self.events))

    async def test_resolve_allow_temporary(self):
        broker = ApprovalBroker(self._emit, store=self.store, timeout=5)
        task = asyncio.create_task(broker.request("run_shell", "ls", "x"))
        await asyncio.sleep(0.05)
        rid = [p["request_id"] for t, p in self.events if t == "approval_request"][-1]
        broker.resolve(rid, True, temporary=True)
        ok, temporary = await task
        self.assertTrue(ok)
        self.assertTrue(temporary)
        log = self.store.list_approvals()
        self.assertEqual(log[0]["decision"], "allow")
        self.assertEqual(log[0]["temporary"], 1)

    async def test_resolve_deny(self):
        broker = ApprovalBroker(self._emit, store=self.store, timeout=5)
        task = asyncio.create_task(broker.request("run_shell", "ls", "x"))
        await asyncio.sleep(0.05)
        rid = [p["request_id"] for t, p in self.events if t == "approval_request"][-1]
        broker.resolve(rid, False)
        ok, temporary = await task
        self.assertFalse(ok)
        self.assertEqual(self.store.list_approvals()[0]["decision"], "deny")

    async def test_reject_all_resolves_pending_as_denied(self):
        broker = ApprovalBroker(self._emit, store=self.store, timeout=5)
        task = asyncio.create_task(broker.request("run_shell", "whoami", "y"))
        await asyncio.sleep(0.05)
        broker.reject_all()
        ok, temporary = await task
        self.assertFalse(ok)
        self.assertFalse(temporary)


if __name__ == "__main__":
    unittest.main()
