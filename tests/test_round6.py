"""Round-6 tests: event bus, background campaign runner, stop/recover, and a
full background campaign run against a real ProjectRuntime."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from backend.store import ProjectStore


class QuietLLM:
    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        return {"role": "assistant", "content": "Step executed and measured."}

    async def complete(self, messages, tools=None, temperature=None, model=None):
        return {"content": "{}"}


class _BusStub:
    """Minimal ProjectRuntime-shaped stub for the bus / runner helpers."""

    def __init__(self, store=None):
        self.store = store or ProjectStore(Path(tempfile.mkdtemp()))
        self._event_subs = []
        self.campaign_stop = False
        self._campaign_task = None

    def subscribe_events(self, fn):
        if fn not in self._event_subs:
            self._event_subs.append(fn)

    def unsubscribe_events(self, fn):
        if fn in self._event_subs:
            self._event_subs.remove(fn)

    async def broadcast(self, event, payload):
        for fn in list(self._event_subs):
            try:
                await fn(event, payload)
            except Exception:  # noqa: BLE001
                pass

    def campaign_running(self):
        return self._campaign_task is not None and not self._campaign_task.done()

    def start_campaign(self, cid, plan_steps=None):
        if self.campaign_running():
            return False, "a campaign is already running for this project"
        if self.store.get_campaign(cid) is None:
            return False, f"campaign #{cid} not found"
        return True, "started"

    def stop_campaign(self):
        if not self.campaign_running():
            return False
        self.campaign_stop = True
        return True


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_broadcast_unsubscribe(self):
        bus = _BusStub()
        got = []
        fn1 = lambda e, p: got.append((e, p))  # noqa: E731
        fn2 = lambda e, p: got.append((e, p))  # noqa: E731
        bus.subscribe_events(fn1)
        bus.subscribe_events(fn1)  # dedup
        bus.subscribe_events(fn2)
        await bus.broadcast("notice", {"message": "hi"})
        await bus.broadcast("workflow", {"status": "done"})
        self.assertEqual(len(got), 4)  # two subscribers × two events
        self.assertEqual(got[0][0], "notice")
        bus.unsubscribe_events(fn1)
        self.assertEqual(len(bus._event_subs), 1)
        await bus.broadcast("status", {"message": "x"})
        self.assertEqual(len(got), 5)  # only fn2 received
        bus.unsubscribe_events(fn2)
        self.assertEqual(len(bus._event_subs), 0)


class CampaignControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_refuses_when_running_and_missing(self):
        bus = _BusStub()
        ok, msg = bus.start_campaign(999)  # not found
        self.assertFalse(ok)
        self.assertIn("not found", msg)
        # Simulate a running task.
        bus._campaign_task = asyncio.get_running_loop().create_task(
            asyncio.sleep(30))
        cid = bus.store.create_campaign("c", "q", "acc", True)
        ok, msg = bus.start_campaign(cid)
        self.assertFalse(ok)
        self.assertIn("already running", msg)
        self.assertTrue(bus.stop_campaign())
        bus._campaign_task.cancel()

    async def test_stop_returns_false_when_idle(self):
        bus = _BusStub()
        self.assertFalse(bus.stop_campaign())


class RecoverCampaignsTests(unittest.IsolatedAsyncioTestCase):
    async def test_recover_marks_stale_running_as_failed(self):
        from backend.project_runtime import ProjectRuntime

        bus = object.__new__(ProjectRuntime)
        bus.store = ProjectStore(Path(tempfile.mkdtemp()))
        cid = bus.store.create_campaign("c", "q", "acc", True)
        bus.store.update_campaign(cid, status="running")
        bus.recover_campaigns()
        c = bus.store.get_campaign(cid)
        self.assertEqual(c["status"], "failed")
        self.assertIn("Interrupted by a server restart", c["report"])


class BackgroundCampaignRunTests(unittest.IsolatedAsyncioTestCase):
    """Run a real background campaign through ProjectRuntime.start_campaign."""

    async def test_background_campaign_completes_and_broadcasts(self):
        import backend.project_runtime as pr

        tmp = Path(tempfile.mkdtemp())
        orig_dir = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = tmp
        try:
            rt = pr.ProjectRuntime("proj")
        finally:
            pr.PROJECTS_DIR = orig_dir

        rt.llm = QuietLLM()
        received = []
        rt.subscribe_events(lambda e, p: received.append(e))

        cid = rt.store.create_campaign("Study", "Q?", "acc", True)
        plan = [
            {"title": "Baseline", "kind": "experiment", "hypothesis": "h1", "plan": "p1"},
            {"title": "Tuned", "kind": "experiment", "hypothesis": "h2", "plan": "p2"},
        ]
        ok, msg = rt.start_campaign(cid, plan_steps=plan)
        self.assertTrue(ok, msg)
        self.assertTrue(rt.campaign_running())

        # Wait for completion (QuietLLM steps are fast).
        deadline = time.time() + 15
        while rt.campaign_running() and time.time() < deadline:
            await asyncio.sleep(0.1)

        c = rt.store.get_campaign(cid)
        self.assertEqual(c["status"], "done", c.get("report"))
        self.assertIn("Baseline", c["report"])
        self.assertTrue(received)
        # Steps are recorded with experiments + best runs.
        steps = rt.store.list_campaign_steps(cid)
        self.assertEqual(len(steps), 2)
        self.assertTrue(all(s["status"] == "done" for s in steps))
        self.assertTrue(all(s["best_run_id"] is not None for s in steps))

    async def test_stop_flag_halts(self):
        import backend.project_runtime as pr

        tmp = Path(tempfile.mkdtemp())
        orig_dir = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = tmp
        try:
            rt = pr.ProjectRuntime("proj")
        finally:
            pr.PROJECTS_DIR = orig_dir

        rt.llm = QuietLLM()
        cid = rt.store.create_campaign("Study", "Q", "acc", True)
        ok, _ = rt.start_campaign(cid, plan_steps=[{"title": "S1", "kind": "experiment",
                                                    "hypothesis": "", "plan": "p"}])
        self.assertTrue(ok)
        self.assertTrue(rt.stop_campaign())
        deadline = time.time() + 10
        while rt.campaign_running() and time.time() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(rt.campaign_running())
        # Either finished quickly or stopped — campaign status must not be running.
        self.assertNotEqual(rt.store.get_campaign(cid)["status"], "running")


if __name__ == "__main__":
    unittest.main()
