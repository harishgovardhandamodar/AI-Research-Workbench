"""Round-9 tests: cross-experiment/campaign comparison, N-run comparison, and
the model-eval benchmark."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.eval import _eval_report, run_eval
from backend.experiments import (compare_campaigns, compare_experiments,
                                 compare_runs_many)
from backend.permissions import PermissionManager
from backend.store import ProjectStore

from tests.test_coordinator import FakeKernels


class QuietLLM:
    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        return {"role": "assistant", "content": "Ran the experiment."}

    async def complete(self, messages, tools=None, temperature=None, model=None):
        return {"content": "{}"}


class CompareExperimentsTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.a = self.store.create_experiment("A", "", "acc", 0.9, True)
        self.b = self.store.create_experiment("B", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.7}, experiment_id=self.a)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.85}, experiment_id=self.b)

    def test_leaderboard_ranks_by_best(self):
        res = compare_experiments(self.store, self.store.list_experiments())
        self.assertEqual(res["rows"][0]["id"], self.b)  # 0.85 best
        self.assertEqual(res["rows"][0]["best"], 0.85)
        self.assertAlmostEqual(res["rows"][0]["delta_best"], 0.0)
        self.assertAlmostEqual(res["rows"][1]["delta_best"], -0.15)
        self.assertAlmostEqual(res["rows"][0]["to_target"], 0.05)

    def test_no_goal_metric_skipped(self):
        c = self.store.create_experiment("C", "", "", None, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"f1": 0.5}, experiment_id=c)
        res = compare_experiments(self.store, self.store.list_experiments())
        ids = {r["id"] for r in res["rows"]}
        self.assertNotIn(c, ids)


class CompareCampaignsTests(unittest.TestCase):
    def test_leaderboard_across_steps(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        c1 = store.create_campaign("Study 1", "q", "acc", True)
        c2 = store.create_campaign("Study 2", "q", "acc", True)
        e1 = store.create_experiment("[Study 1] step", "", "acc", None, True)
        e2 = store.create_experiment("[Study 2] step", "", "acc", None, True)
        store.add_run("p", "r", "done", 1.0, 2.0,
                      metrics={"acc": 0.6}, experiment_id=e1)
        store.add_run("p", "r", "done", 1.0, 2.0,
                      metrics={"acc": 0.9}, experiment_id=e2)
        store.add_campaign_step(c1, 1, "s", "experiment", "", "")
        store.update_campaign_step(store.list_campaign_steps(c1)[0]["id"],
                                   status="done", experiment_id=e1)
        store.add_campaign_step(c2, 1, "s", "experiment", "", "")
        store.update_campaign_step(store.list_campaign_steps(c2)[0]["id"],
                                   status="done", experiment_id=e2)
        res = compare_campaigns(store, store.list_campaigns())
        self.assertEqual(res["rows"][0]["id"], c2)
        self.assertEqual(res["rows"][0]["best"], 0.9)


class CompareRunsManyTests(unittest.TestCase):
    def test_side_by_side_table(self):
        runs = [
            {"id": 1, "label": "a", "metrics": {"acc": 0.7, "f1": 0.6}},
            {"id": 2, "label": "b", "metrics": {"acc": 0.85}},
        ]
        res = compare_runs_many(runs)
        self.assertEqual(res["columns"], ["a", "b"])
        acc = next(r for r in res["rows"] if r["metric"] == "acc")
        self.assertEqual(acc["values"], {"a": 0.7, "b": 0.85})
        self.assertEqual(acc["best"], "b")
        f1 = next(r for r in res["rows"] if r["metric"] == "f1")
        self.assertEqual(f1["values"], {"a": 0.6})
        self.assertEqual(f1["best"], "a")


class EvalStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))

    def test_crud(self):
        eid = self.store.create_eval("bench", "run task", ["m1", "m2"], "acc", True)
        ev = self.store.get_eval(eid)
        self.assertEqual(ev["models"], ["m1", "m2"])
        self.assertEqual(ev["status"], "planned")
        self.store.update_eval(eid, status="done", report="# done")
        self.assertEqual(self.store.get_eval(eid)["status"], "done")
        self.assertEqual(len(self.store.list_evals()), 1)

    def test_eval_report_ranks(self):
        ev = {"name": "bench", "goal_metric": "acc", "higher_better": True}
        results = [{"model": "m1", "best": 0.7, "experiment_id": 1, "best_run_id": 1},
                   {"model": "m2", "best": 0.9, "experiment_id": 2, "best_run_id": 2}]
        report = _eval_report(self.store, ev, results)
        self.assertIn("m2", report.split("Best model")[1])
        self.assertIn("0.9", report)


class RunEvalTests(unittest.IsolatedAsyncioTestCase):
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

    def _rt(self):
        class _Rt:
            name = "proj"
            reviewer_enabled = False

            def __init__(self, store, d, llm):
                self.store = store
                self.dir = d
                self.llm = llm

            async def maybe_compact(self):
                pass

        return _Rt(self.store, self.tmp, QuietLLM())

    def _coordinator(self):
        def record(r):
            return self.store.add_run(
                prompt=r.get("prompt", ""), reply=r.get("reply", ""),
                status=r.get("status", "done"),
                started_at=r.get("started_at", 0.0),
                finished_at=r.get("finished_at", 1.0),
                tool_sequence=r.get("tool_sequence"),
                artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                review=r.get("review"),
                experiment_id=r.get("experiment_id") or None,
                config=r.get("config"), label=r.get("label"),
                parent_run_id=r.get("parent_run_id") or None,
                model=r.get("model") or None, code=r.get("code"), env=r.get("env"),
                message_id=r.get("message_id") or None)

        return Coordinator(QuietLLM(), self.ctx, emit=self._emit,
                           persist=lambda r, c, m: None, record=record,
                           max_iters=3, mcp=None)

    def _build_llm_messages(self):
        msgs = [{"role": "system", "content": "You are Fox."}]
        for m in self.store.list_messages():
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    async def test_run_eval_creates_per_model_experiments(self):
        rt = self._rt()
        eid = rt.store.create_eval("bench", "Run the task", ["m1", "m2"], "acc", True)
        result = await run_eval(rt, self._coordinator(), self._build_llm_messages,
                                eid, emit=self._emit)
        self.assertEqual(result["eval"]["status"], "done")
        exps = rt.store.list_experiments()
        self.assertEqual(len(exps), 2)
        models = {e["model"] for e in exps}
        self.assertEqual(models, {"m1", "m2"})
        self.assertIn("m1", result["report"])
        self.assertIn("m2", result["report"])


if __name__ == "__main__":
    unittest.main()
