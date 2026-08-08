"""Round-3 tests: first-class suggestions + regression check, model pinning,
run diffs, improve-loop resume, workflow retry metadata, and parallel sweeps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext, _run_sweep
from backend.artifacts.store import ArtifactStore
from backend.experiment_loop import _next_pending_suggestion, run_improve_loop
from backend.experiments import run_diff
from backend.permissions import PermissionManager
from backend.store import ProjectStore, _UNSET
from backend.workflows import WorkflowTracker

from tests.test_coordinator import FakeKernels
from tests.test_experiment_loop import StubCtx, FakeLLM, ScriptedReviewer


class SuggestionRecordTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                      metrics={"acc": 0.5}, experiment_id=self.eid)

    def test_add_and_list(self):
        ids = self.store.add_suggestions(self.eid, self.rid, {
            "suggestions": [
                {"title": "try eps=1", "action": "a", "prompt": "rerun with eps=1"},
                {"title": "more data", "action": "b", "prompt": "rerun with more data"},
            ]})
        self.assertEqual(len(ids), 2)
        sug = self.store.get_suggestion(ids[0])
        self.assertEqual(sug["title"], "try eps=1")
        self.assertEqual(sug["status"], "pending")
        self.assertEqual(sug["experiment_id"], self.eid)
        self.assertEqual(sug["source_run_id"], self.rid)
        self.assertEqual(len(self.store.list_suggestions(self.eid)), 2)

    def test_mark_applied_and_resolve_accepted(self):
        sid = self.store.add_suggestions(self.eid, self.rid, {
            "suggestions": [{"title": "t", "action": "a", "prompt": "p"}]})[0]
        rid2 = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                  metrics={"acc": 0.7}, experiment_id=self.eid)
        self.store.mark_suggestion_applied(sid, rid2)
        out = self.store.resolve_suggestion_outcome(sid)
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(out["improved"], 1)
        self.assertAlmostEqual(out["delta"], 0.2)

    def test_mark_applied_and_resolve_rejected(self):
        sid = self.store.add_suggestions(self.eid, self.rid, {
            "suggestions": [{"title": "t", "action": "a", "prompt": "p"}]})[0]
        rid2 = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                  metrics={"acc": 0.4}, experiment_id=self.eid)
        self.store.mark_suggestion_applied(sid, rid2)
        out = self.store.resolve_suggestion_outcome(sid)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["improved"], 0)

    def test_resolve_without_metric_stays_applied(self):
        sid = self.store.add_suggestions(self.eid, self.rid, {
            "suggestions": [{"title": "t", "action": "a", "prompt": "p"}]})[0]
        rid2 = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                  metrics={"other": 1}, experiment_id=self.eid)
        self.store.mark_suggestion_applied(sid, rid2)
        self.assertEqual(self.store.resolve_suggestion_outcome(sid)["status"], "applied")


class PendingSuggestionSelectionTests(unittest.TestCase):
    def test_skips_already_applied(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        eid = store.create_experiment("e", "", "acc", 0.9, True)
        rid = store.add_run("p", "r", "done", 1.0, 2.0,
                            metrics={"acc": 0.5}, experiment_id=eid)
        sid = store.add_suggestions(eid, rid, {
            "suggestions": [{"title": "used", "action": "a", "prompt": "used prompt"}]})[0]
        store.mark_suggestion_applied(sid)
        store.resolve_suggestion_outcome(sid)  # stays 'applied' (no run_id metric)
        fresh = {"suggestions": [{"title": "used", "action": "a", "prompt": "used prompt", "id": sid},
                                 {"title": "new", "action": "b", "prompt": "new prompt"}]}
        pick = _next_pending_suggestion(store, eid, fresh)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["title"], "new")


class ModelPinningTests(unittest.TestCase):
    def test_experiment_model_field(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        eid = store.create_experiment("e", "", "acc", 0.9, True, model="llama3:8b")
        self.assertEqual(store.get_experiment(eid)["model"], "llama3:8b")
        store.update_experiment(eid, model="qwen:7b")
        self.assertEqual(store.get_experiment(eid)["model"], "qwen:7b")
        store.update_experiment(eid, model=_UNSET)
        self.assertEqual(store.get_experiment(eid)["model"], "")

    def test_coordinator_pinned_model_resolution(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        eid = store.create_experiment("e", "", "acc", 0.9, True, model="pinned:7b")
        ctx = StubCtx()
        ctx.store = store
        coord = Coordinator.__new__(Coordinator)
        coord.ctx = ctx
        ctx.experiment_id = str(eid)
        self.assertEqual(coord._pinned_model(), "pinned:7b")
        ctx.experiment_id = ""
        store.set_setting("focus_experiment_id", str(eid))
        self.assertEqual(coord._pinned_model(), "pinned:7b")
        store.set_setting("focus_experiment_id", "")
        self.assertEqual(coord._pinned_model(), "")


class RunDiffTests(unittest.TestCase):
    def test_diff_config_tools_metrics(self):
        a = {"id": 1, "label": "base", "config": {"eps": 1, "lr": 0.01},
             "metrics": {"acc": 0.7}, "prompt": "run base",
             "tool_sequence": [{"name": "run_python", "ok": True}]}
        b = {"id": 2, "label": "eps=2", "config": {"eps": 2, "lr": 0.01},
             "metrics": {"acc": 0.8}, "prompt": "run eps=2",
             "tool_sequence": [{"name": "run_python", "ok": True},
                               {"name": "save_artifact", "ok": False}]}
        d = run_diff(a, b)
        self.assertEqual(d["config"]["added"], [])
        self.assertEqual(d["config"]["changed"], [["eps", 1, 2]])
        self.assertEqual(d["tools"]["added"], ["save_artifact"])
        self.assertEqual(d["tools"]["failed"], ["save_artifact"])
        self.assertEqual(d["metrics"]["rows"][0]["metric"], "acc")
        self.assertAlmostEqual(d["metrics"]["rows"][0]["delta"], 0.1)


class ImproveLoopResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.ctx = ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.emitted = []

    async def _emit(self, t, p):
        self.emitted.append((t, p))

    def _coordinator(self, llm):
        return Coordinator(llm, self.ctx, emit=self._emit,
                           persist=lambda r, c, m: None,
                           record=self._persist_run, max_iters=6, mcp=None)

    def _persist_run(self, r):
        return self.store.add_run(
            prompt=r.get("prompt", ""), reply=r.get("reply", ""),
            status=r.get("status", "done"), started_at=r.get("started_at", 0.0),
            finished_at=r.get("finished_at", 0.0),
            tool_sequence=r.get("tool_sequence"), artifact_ids=r.get("artifact_ids"),
            metrics=r.get("metrics"), review=r.get("review"),
            experiment_id=r.get("experiment_id") or None, config=r.get("config"),
            label=r.get("label"))

    def _build_llm_messages(self):
        msgs = [{"role": "system", "content": "You are Fox."}]
        for m in self.store.list_messages():
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    async def test_resume_from_iteration_2(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.99, True)
        review = {"findings": [], "suggestions": [
            {"title": "try eps=2", "action": "rerun",
             "prompt": "Start variant 'eps=2' with config {eps:2}, rerun."}]}
        reviewer = ScriptedReviewer([review, review])
        wf = WorkflowTracker()
        result = await run_improve_loop(
            self.store, self._coordinator(FakeLLM()), self._build_llm_messages,
            reviewer, eid, "Improve it.", emit=self._emit, iterations=2,
            workflow=wf, start_at=2)
        self.assertEqual(len(result["iterations"]), 1)
        self.assertEqual(result["iterations"][0]["iteration"], 2)
        snap = wf.snapshot()
        self.assertEqual([s["id"] for s in snap["stages"]], ["iter2"])
        self.assertEqual(snap["invoke"]["kind"], "improve")
        self.assertEqual(snap["invoke"]["experiment_id"], eid)


class WorkflowInvokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_roundtrip(self):
        wf = WorkflowTracker()
        await wf.start(title="Improve e", stages=[{"id": "iter1", "label": "Iteration 1"}])
        wf.set_invoke(kind="improve", experiment_id=3, prompt="go", iterations=2)
        snap = wf.snapshot()
        self.assertEqual(snap["invoke"]["experiment_id"], 3)
        wf2 = WorkflowTracker()
        wf2.restore(snap)
        self.assertEqual(wf2.invoke["iterations"], 2)


class SweepKernel:
    def __init__(self, metrics):
        self._metrics = metrics

    async def run_code(self, code, timeout=30.0):
        if code.startswith("config = "):
            return {"output": "", "metrics": {}}
        return {"output": "sweep done", "metrics": self._metrics}


class PoolKernels(FakeKernels):
    def __init__(self):
        super().__init__()
        self._n = 0

    def pool(self, n):
        self._n = n
        return [SweepKernel({"acc": 0.7 + i * 0.1}) for i in range(n)]

    async def stop_pool(self, kernels):
        pass


class RunSweepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("eps sweep", "", "acc", 0.95, True)
        self.base = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                       metrics={"acc": 0.5}, experiment_id=self.eid)
        self.ctx = ToolContext(kernels=PoolKernels(), artifacts=ArtifactStore(self.tmp),
                               store=self.store, permissions=PermissionManager(self.store))
        self.ctx.experiment_id = str(self.eid)
        self.ctx.parent_run_id = self.base

    async def test_sweep_records_one_run_per_config(self):
        out = await _run_sweep(
            self.ctx, "report_metric('acc', config['eps'] / 10)",
            [{"eps": 1}, {"eps": 2}, {"eps": 3}], label_prefix="eps")
        self.assertIn("## Parameter sweep", out)
        self.assertIn("parallel", out)
        runs = self.store.experiment_runs(self.eid)
        sweep = [r for r in runs if r["kind"] == "sweep"]
        self.assertEqual(len(sweep), 3)
        self.assertEqual(sorted(r["config"]["eps"] for r in sweep), [1, 2, 3])
        for r in sweep:
            self.assertEqual(r["parent_run_id"], self.base)
            self.assertIn("acc", r["metrics"])


if __name__ == "__main__":
    unittest.main()
