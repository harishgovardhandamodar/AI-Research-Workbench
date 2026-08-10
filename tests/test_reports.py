"""Experiment results & report-publishing tests: the reports hub endpoints
(consolidated report artifacts + publish-to-chat)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.artifacts.store import Artifact


class TestReportsHub(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("repproj")
        runtimes["repproj"] = self.rt
        from backend.routers import runs as runsmod
        self.runsmod = runsmod
        # Route handlers resolve the runtime via the global registry; pin them
        # to our isolated runtime so discover-order pollution can't leak in.
        self._gr = mock.patch.object(runsmod, "get_runtime",
                                     lambda name: self.rt)
        self._gr.start()

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self._gr.stop()
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("repproj", None)
        await self.rt.stop()

    def _add_artifact(self, name, kind, data_type="text", data=b"", created_at=None):
        art = Artifact(kind=kind, name=name, description="d", code="", env={},
                       message_id="", run_id="", created_at=created_at)
        self.rt.artifacts.add_artifact(art, data=data, data_type=data_type)
        return art.id

    async def test_reports_lists_only_report_artifacts(self):
        rid = self._add_artifact("run-7-report", "text", data=b"# Run 7 report", created_at=2.0)
        pid = self._add_artifact("plan-abc-report", "report", data=b"# Plan report", created_at=1.0)
        self._add_artifact("fig_hist.png", "figure", data_type="png", data=b"png")
        self._add_artifact("notes", "text", data=b"not a report")

        res = await self.runsmod.project_reports("repproj")
        ids = [r["id"] for r in res["reports"]]
        self.assertIn(rid, ids)
        self.assertIn(pid, ids)
        self.assertEqual(len(ids), 2)
        # newest first
        self.assertEqual(ids[0], rid)

    async def test_publish_report_to_chat(self):
        from fastapi import HTTPException
        aid = self._add_artifact("run-9-report", "text", data=b"# Report body\n\nfindings")
        res = await self.runsmod.publish_report("repproj", aid)
        self.assertTrue(res["message_id"])
        msgs = self.rt.store.list_messages()
        self.assertTrue(any("Report body" in m["content"] for m in msgs))
        # non-existent artifact -> 404
        with self.assertRaises(HTTPException) as ctx:
            await self.runsmod.publish_report("repproj", "does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_run_report_endpoint(self):
        # a real run + report generation (no LLM: summary is best-effort).
        rid = self.rt.store.add_run(
            "profile the dataset", "| metric | value |\n|---|---|\n| rows | 10 |",
            "done", 0.0, 1.0, metrics={"rows": 10.0}, kind="agent_run")
        res = await self.runsmod.project_run_report("repproj", rid)
        self.assertIn("rows", res["report"])
        self.assertTrue(res["artifact_id"])
        self.assertTrue(res["message_id"])

    def _exp_with_runs(self, goal="accuracy", target=0.9, higher=True):
        eid = self.rt.store.create_experiment(
            "Round-13 exp", "hypothesis", goal, target, higher)
        for i, v in enumerate([0.5, 0.7, 0.95]):
            self.rt.store.add_run(f"run {i}", "ok", "done", 0.0, float(i + 1),
                                  metrics={goal: float(v)},
                                  experiment_id=eid, kind="agent_run")
        self.rt.store.add_learning(
            eid, None, goal, 0.5, 0.95, 0.45, True,
            "raising epochs helped", "suggestion")
        return eid

    async def test_publish_experiment_report(self):
        eid = self._exp_with_runs()
        res = await self.runsmod.publish_experiment_report("repproj", eid)
        self.assertIn("Round-13 exp", res["report"])
        self.assertIn("`accuracy`", res["report"])
        self.assertIn("goal reached ✓", res["report"])
        self.assertIn("## Runs (3)", res["report"])
        self.assertIn("## Learnings", res["report"])
        self.assertTrue(res["artifact_id"])
        self.assertTrue(res["message_id"])
        art = self.rt.artifacts.get(res["artifact_id"])
        self.assertEqual(art.kind, "report")
        self.assertEqual(art.name, f"exp-{eid}-report")

    async def test_get_experiment_report_on_demand(self):
        eid = self._exp_with_runs()
        res = await self.runsmod.get_experiment_report("repproj", eid)
        self.assertIsNone(res["artifact_id"])  # generated, not yet published
        self.assertIn("Round-13 exp", res["report"])
        # after publishing, GET returns the stored artifact
        await self.runsmod.publish_experiment_report("repproj", eid)
        res2 = await self.runsmod.get_experiment_report("repproj", eid)
        self.assertTrue(res2["artifact_id"])
        # unknown experiment -> 404
        with self.assertRaises(Exception) as ctx:
            await self.runsmod.publish_experiment_report("repproj", 99999)
        self.assertIn("404", str(ctx.exception))

    async def test_status_complete_auto_publishes_report(self):
        eid = self._exp_with_runs()
        res = await self.runsmod.update_project_experiment(
            "repproj", eid, {"status": "completed"})
        self.assertEqual(res["experiment"]["status"], "completed")
        # background task publishes the report artifact
        for _ in range(100):
            arts = [a for a in self.rt.artifacts.list()
                    if a.name == f"exp-{eid}-report"]
            if arts:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(arts)
        self.assertEqual(arts[0].kind, "report")
        # invalid status -> 400
        with self.assertRaises(Exception) as ctx:
            await self.runsmod.update_project_experiment("repproj", eid, {"status": "banana"})
        self.assertIn("400", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
