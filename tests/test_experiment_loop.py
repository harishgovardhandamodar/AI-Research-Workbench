"""Improve-loop (B2) tests: bounded iteration, goal detection, suggestion reuse,
and the pure best_metric helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.experiment_loop import best_metric, run_improve_loop
from backend.permissions import PermissionManager
from backend.store import ProjectStore

from tests.test_coordinator import FakeKernels, ScriptedLLM


class StubCtx:
    """Minimal ctx stub the coordinator touches (message_id / experiment_id)."""

    def __init__(self):
        self.message_id = ""
        self.experiment_id = ""
        self.variant = None
        self.finished_variants = []
        self.last_artifact_ids = []
        self.last_metrics = None
        self.run_id = ""
        self.workflow = None


class ScriptedReviewer:
    """Yields a scripted sequence of reviews, then an empty one."""

    def __init__(self, reviews):
        self.reviews = list(reviews)
        self.calls = 0
        self.extras = []

    async def __call__(self, extra: str = ""):
        self.calls += 1
        self.extras.append(extra)
        if self.reviews:
            return self.reviews.pop(0)
        return {"findings": [], "suggestions": []}


class FakeLLM:
    """Serves tool batches then a final reply each turn (metrics: accuracy 0.9)."""

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        self.calls += 1
        if self.calls % 2 == 1:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": f"c{self.calls}",
                        "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": {"code": "print('accuracy: 0.9')"}},
                    }]}
        return {"role": "assistant", "content": f"Turn {self.calls} done."}


class TestImproveLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.ctx = ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.emitted = []

    async def _emit(self, t: str, p: dict):
        self.emitted.append((t, p))

    def _coordinator(self, llm, ctx=None, max_iters=6):
        return Coordinator(llm, ctx or self.ctx, emit=self._emit,
                           persist=lambda r, c, m: None,
                           record=self._persist_run,
                           max_iters=max_iters, mcp=None)

    def _persist_run(self, r: dict):
        # Mirror main.py's record wiring so loop-produced runs land in the store:
        # the coordinator pre-creates the row (r["id"]) and finish_run finalizes
        # it; without an id (legacy path) fall back to add_run.
        if r.get("id"):
            return self.store.finish_run(
                rid=int(r["id"]),
                reply=r.get("reply", ""),
                status=r.get("status", "done"),
                finished_at=r.get("finished_at"),
                tool_sequence=r.get("tool_sequence"),
                artifact_ids=r.get("artifact_ids"),
                metrics=r.get("metrics"),
                config=r.get("config"),
                label=r.get("label"),
                code=r.get("code"),
                env=r.get("env"),
                error=r.get("error") or None,
                review=r.get("review"))
        return self.store.add_run(
            prompt=r.get("prompt", ""),
            reply=r.get("reply", ""),
            status=r.get("status", "done"),
            started_at=r.get("started_at", 0.0),
            finished_at=r.get("finished_at", 0.0),
            tool_sequence=r.get("tool_sequence"),
            artifact_ids=r.get("artifact_ids"),
            metrics=r.get("metrics"),
            review=r.get("review"),
            experiment_id=r.get("experiment_id") or None,
            config=r.get("config"),
            label=r.get("label"))

    def _build_llm_messages(self):
        # Stub the runtime message builder: system + recent user messages.
        msgs = [{"role": "system", "content": "You are Fox."}]
        for m in self.store.list_messages():
            role = m["role"]
            meta = m.get("meta") or {}
            if role == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                if meta.get("tool_calls"):
                    msgs.append({"role": "assistant", "content": "",
                                 "tool_calls": meta["tool_calls"]})
                else:
                    msgs.append({"role": "assistant", "content": m["content"]})
            elif role == "tool":
                msgs.append({"role": "tool",
                             "tool_call_id": meta.get("tool_call_id", ""),
                             "content": m["content"]})
        return msgs

    async def test_best_metric_helper(self):
        runs = [
            {"id": 1, "metrics": {"acc": 0.8}},
            {"id": 2, "metrics": {"acc": 0.95}},
            {"id": 3, "metrics": {"loss": 1.2}},
        ]
        self.assertEqual(best_metric(runs, "acc", True), (0.95, 2))
        self.assertEqual(best_metric(runs, "acc", False), (0.8, 1))
        self.assertEqual(best_metric(runs, "missing", True), (None, None))

    async def test_loop_stops_at_goal(self):
        eid = self.store.create_experiment("eps sweep", "h", "accuracy", 0.9, True)
        review = {"findings": [], "suggestions": [
            {"title": "try eps=1.0",
             "action": "rerun with eps=1.0",
             "prompt": "Start variant run 'eps=1.0' with config {eps:1}, rerun."}]}
        reviewer = ScriptedReviewer([review, review])
        coordinator = self._coordinator(FakeLLM())
        result = await run_improve_loop(
            self.store, coordinator, self._build_llm_messages, reviewer,
            eid, "Improve the eps sweep.", emit=self._emit, iterations=5)
        self.assertTrue(result["goal_reached"])
        self.assertEqual(result["best"], 0.9)
        self.assertEqual(result["stopped_reason"], "goal reached")
        # Stopped at iteration 1 once the 0.9 goal was reached.
        self.assertEqual(len(result["iterations"]), 1)
        self.assertEqual(result["iterations"][0]["goal_metric_value"], 0.9)
        # Runs are attached to the experiment.
        self.assertEqual(len(self.store.experiment_runs(eid)), 1)

    async def test_loop_uses_suggestions_until_budget(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.99, True)
        review = {"findings": [], "suggestions": [
            {"title": "try higher eps",
             "action": "rerun with eps=2",
             "prompt": "Start variant run 'eps=2.0' with config {eps:2}, rerun."}]}
        # Each review suggests again, so the loop runs until the 2-iteration budget.
        reviewer = ScriptedReviewer([review, review, review])
        coordinator = self._coordinator(FakeLLM())
        result = await run_improve_loop(
            self.store, coordinator, self._build_llm_messages, reviewer,
            eid, "Improve it.", emit=self._emit, iterations=2)
        self.assertFalse(result["goal_reached"])
        self.assertEqual(len(result["iterations"]), 2)
        # Second iteration reused the first review's suggestion prompt.
        self.assertEqual(result["iterations"][1]["suggestion"]["title"], "try higher eps")
        self.assertEqual(len(self.store.experiment_runs(eid)), 2)
        # The reviewer received the goal-first context block each iteration.
        self.assertTrue(reviewer.extras)
        self.assertTrue(all("Experiment context" in e for e in reviewer.extras))

    async def test_loop_stops_when_no_suggestion(self):
        eid = self.store.create_experiment("quiet", "h", "accuracy", 0.99, True)
        reviewer = ScriptedReviewer([{"findings": [], "suggestions": []}])
        coordinator = self._coordinator(FakeLLM())
        result = await run_improve_loop(
            self.store, coordinator, self._build_llm_messages, reviewer,
            eid, "Improve it.", emit=self._emit, iterations=5)
        self.assertEqual(len(result["iterations"]), 1)
        self.assertEqual(result["stopped_reason"], "no further suggestions")
        self.assertIn("no further", result["summary"])

    async def test_loop_unknown_experiment(self):
        coordinator = self._coordinator(FakeLLM())
        result = await run_improve_loop(
            self.store, coordinator, self._build_llm_messages, None,
            999, "Improve it.", emit=self._emit)
        self.assertIn("not found", result["summary"])
        self.assertEqual(result["iterations"], [])

    async def test_loop_refuses_closed_experiment(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.9, True)
        self.store.update_experiment_status(eid, "completed")
        coordinator = self._coordinator(FakeLLM())
        result = await run_improve_loop(
            self.store, coordinator, self._build_llm_messages, None,
            eid, "Improve it.", emit=self._emit)
        self.assertEqual(result["iterations"], [])
        self.assertFalse(result["goal_reached"])
        self.assertIn("completed", result["summary"])
        self.assertIn("reopen", result["summary"])


if __name__ == "__main__":
    unittest.main()
