"""Workflow progress tracker: stages reconcile when the pipeline finishes."""

from __future__ import annotations

import asyncio
import unittest

from backend.workflows import ARXIV_STAGES, WorkflowTracker


class TestWorkflowFinish(unittest.IsolatedAsyncioTestCase):
    async def _tracker(self):
        events = []

        async def emit(_event, payload):
            events.append(payload)

        wt = WorkflowTracker()
        wt.subscribe(emit)
        return wt, events

    async def test_finish_marks_leftover_pending_stages_done(self):
        wt, events = await self._tracker()
        await wt.start("arXiv replication")
        # Agent runs only the first few stages (e.g. ingests, extracts, notes,
        # experiment spec) and finishes the turn without touching the rest.
        for sid in ("ingest", "extract", "notes", "experiment"):
            await wt.update_stage(sid, "done")
        await wt.finish()

        snap = events[-1]
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["pct"], 100)
        states = {s["id"]: s["state"] for s in snap["stages"]}
        self.assertEqual(states["summarize"], "done")
        self.assertEqual(states["run"], "done")
        self.assertEqual(states["report"], "done")
        for s in snap["stages"]:
            self.assertNotIn(s["state"], ("pending", "running"))

    async def test_failed_stage_keeps_pending_stages_and_marks_pipeline_failed(self):
        wt, events = await self._tracker()
        await wt.start("arXiv replication")
        await wt.update_stage("ingest", "done")
        await wt.update_stage("extract", "failed")
        await wt.finish()

        snap = events[-1]
        self.assertEqual(snap["status"], "failed")
        states = {s["id"]: s["state"] for s in snap["stages"]}
        self.assertEqual(states["extract"], "failed")
        self.assertEqual(states["notes"], "pending")  # untouched leftovers stay queued

    async def test_finish_is_noop_when_not_running(self):
        wt, _events = await self._tracker()
        await wt.finish()  # idle tracker: must not raise or change state
        self.assertEqual(wt._status, "idle")


if __name__ == "__main__":
    unittest.main()
