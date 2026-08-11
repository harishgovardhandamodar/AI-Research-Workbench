"""Round-5 tests: research campaigns — data model, plan parsing, the execution
loop (per-step experiments, lineage, synthesis report), and resume."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.campaign import _campaign_report, _parse_steps, run_campaign
from backend.permissions import PermissionManager
from backend.store import ProjectStore
from backend.workflows import WorkflowTracker

from tests.test_coordinator import FakeKernels


class QuietLLM:
    """Returns plain-text replies (no tools) so each step records one run."""

    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        return {"role": "assistant", "content": "Step executed and measured."}

    async def complete(self, messages, tools=None, temperature=None, model=None):
        return {"content": "{}"}


class CampaignStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))

    def test_crud(self):
        cid = self.store.create_campaign("Study A", "Does eps matter?", "acc", True)
        c = self.store.get_campaign(cid)
        self.assertEqual(c["research_question"], "Does eps matter?")
        self.assertEqual(c["status"], "planned")
        sid = self.store.add_campaign_step(cid, 1, "Baseline", "experiment", "h", "p")
        self.store.update_campaign_step(sid, status="running", experiment_id=7)
        self.store.update_campaign_step(sid, status="done", best_run_id=3)
        steps = self.store.list_campaign_steps(cid)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["best_run_id"], 3)
        self.assertEqual(steps[0]["experiment_id"], 7)
        self.store.update_campaign(cid, status="done", report="# done")
        self.assertEqual(self.store.get_campaign(cid)["status"], "done")
        self.assertEqual(self.store.list_campaigns()[0]["steps"], 1)


class PlanParseTests(unittest.TestCase):
    def test_parse_steps_json(self):
        text = ('Sure! Here is the plan:\n[{"title": "Baseline", "kind": "experiment", '
                '"hypothesis": "h", "plan": "run baseline"}, '
                '{"title": "Ablation", "kind": "sweep", "hypothesis": "", "plan": "sweep eps"}]')
        steps = _parse_steps(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["title"], "Baseline")
        self.assertEqual(steps[1]["kind"], "sweep")

    def test_parse_steps_fallback(self):
        self.assertEqual(_parse_steps("no plan here"), [])

    def test_report_aggregates_steps(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        cid = store.create_campaign("Study", "Q", "acc", True)
        eid = store.create_experiment("[Study] Baseline", "", "acc", None, True)
        rid = store.add_run("p", "r", "done", 1.0, 2.0,
                            metrics={"acc": 0.7}, experiment_id=eid)
        store.add_campaign_step(cid, 1, "Baseline", "experiment", "h", "p")
        store.update_campaign_step(
            store.list_campaign_steps(cid)[0]["id"], status="done",
            experiment_id=eid, best_run_id=rid)
        c = store.get_campaign(cid)
        report = _campaign_report(store, c, store.list_campaign_steps(cid))
        self.assertIn("Baseline", report)
        self.assertIn("Best acc: 0.7", report)


class CampaignLoopTests(unittest.IsolatedAsyncioTestCase):
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
            reviewer_enabled = True

            def __init__(self, store, d, llm):
                self.store = store
                self.dir = d
                self.llm = llm

            async def maybe_compact(self):
                pass

        return _Rt(self.store, self.tmp, QuietLLM())

    def _coordinator(self):
        def persist(role, content, meta=None):
            return self.store.add_message(role, content, meta)

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
                model=r.get("model") or None, code=r.get("code"), env=r.get("env"))

        return Coordinator(QuietLLM(), self.ctx, emit=self._emit, persist=persist,
                           record=record, max_iters=4, mcp=None)

    def _build_llm_messages(self):
        msgs = [{"role": "system", "content": "You are Fox."}]
        for m in self.store.list_messages():
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    async def test_campaign_executes_steps_and_synthesizes(self):
        rt = self._rt()
        cid = rt.store.create_campaign("Study", "Question?", "acc", True)
        plan = [
            {"title": "Baseline", "kind": "experiment", "hypothesis": "h1", "plan": "run baseline"},
            {"title": "Tuned", "kind": "experiment", "hypothesis": "h2", "plan": "tune params"},
        ]
        wf = WorkflowTracker()
        result = await run_campaign(rt, self._coordinator(), self._build_llm_messages,
                                    cid, emit=self._emit, workflow=wf, plan_steps=plan)
        self.assertEqual(result["campaign"]["status"], "done")
        steps = rt.store.list_campaign_steps(cid)
        self.assertEqual(len(steps), 2)
        for s in steps:
            self.assertEqual(s["status"], "done")
            self.assertIsNotNone(s["experiment_id"])
            self.assertIsNotNone(s["best_run_id"])
        # One experiment per step; step 2's run chains off step 1's best run.
        exps = rt.store.list_experiments()
        self.assertEqual(len(exps), 2)
        s1, s2 = steps
        runs2 = rt.store.experiment_runs(s2["experiment_id"])
        self.assertTrue(runs2)
        self.assertEqual(runs2[-1]["parent_run_id"], s1["best_run_id"])
        # Synthesis report posted as a message + stored on the campaign.
        self.assertIn("Baseline", result["report"])
        self.assertIn("Tuned", result["report"])
        snap = wf.snapshot()
        self.assertEqual(snap["invoke"]["kind"], "campaign")
        self.assertEqual(snap["invoke"]["campaign_id"], cid)
        self.assertTrue(any(t == "assistant_message" for t, _ in self.emitted))

    async def test_campaign_resumes_from_step_2(self):
        rt = self._rt()
        cid = rt.store.create_campaign("Study", "Q", "acc", True)
        plan = [
            {"title": "Baseline", "kind": "experiment", "hypothesis": "", "plan": "p1"},
            {"title": "Tuned", "kind": "experiment", "hypothesis": "", "plan": "p2"},
        ]
        result = await run_campaign(rt, self._coordinator(), self._build_llm_messages,
                                    cid, emit=self._emit, resume_step=2, plan_steps=plan)
        steps = rt.store.list_campaign_steps(cid)
        self.assertEqual(steps[0]["status"], "planned")
        self.assertEqual(steps[0]["experiment_id"], None)
        self.assertEqual(steps[1]["status"], "done")
        # Only the resumed step created an experiment.
        self.assertEqual(len(rt.store.list_experiments()), 1)

    def _flaky(self, fails: int):
        class Ctx:
            experiment_id = ""
            parent_run_id = None
            message_id = ""

        class Flaky:
            def __init__(self):
                self.calls = 0
                self.check_abort = None
                self.ctx = Ctx()

            async def run_turn(self, messages):
                self.calls += 1
                if self.calls <= fails:
                    raise RuntimeError("transient failure")
                return {"text": "step result"}

        return Flaky()

    async def test_campaign_retries_step_on_transient_failure(self):
        rt = self._rt()
        cid = rt.store.create_campaign("Study", "Q", "acc", True)
        plan = [{"title": "Baseline", "kind": "experiment", "hypothesis": "", "plan": "p1"}]
        coord = self._flaky(fails=1)  # fails once, then succeeds
        result = await run_campaign(rt, coord, self._build_llm_messages,
                                    cid, emit=self._emit, plan_steps=plan)
        self.assertEqual(result["campaign"]["status"], "done")
        self.assertEqual(coord.calls, 2)
        steps = rt.store.list_campaign_steps(cid)
        self.assertEqual(steps[0]["status"], "done")

    async def test_campaign_gives_up_after_retries(self):
        rt = self._rt()
        cid = rt.store.create_campaign("Study", "Q", "acc", True)
        plan = [{"title": "Baseline", "kind": "experiment", "hypothesis": "", "plan": "p1"}]
        coord = self._flaky(fails=99)  # always fails
        result = await run_campaign(rt, coord, self._build_llm_messages,
                                    cid, emit=self._emit, plan_steps=plan)
        self.assertEqual(result["campaign"]["status"], "failed")
        self.assertIn("step 1 failed", result["stopped_reason"])
        steps = rt.store.list_campaign_steps(cid)
        self.assertEqual(steps[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
