"""Round-7 tests: learnings & knowledge memory — store, capture from resolved
suggestions, and injection into experiment/reviewer context."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.reviewer import build_review_context
from backend.store import ProjectStore

from tests.test_experiment_loop import FakeLLM, ScriptedReviewer, StubCtx
from tests.test_goal_steering import GoalFirstContextTests  # noqa: F401  (pattern)


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                      metrics={"acc": 0.5}, experiment_id=self.eid)

    def test_add_and_list_by_experiment(self):
        lid = self.store.add_learning(self.eid, self.rid, "acc", 0.5, 0.8, 0.3, 1,
                                      "Tried X: acc 0.5->0.8 — improved.", "suggestion")
        self.assertIsNotNone(self.store.get_run(self.rid))
        ls = self.store.list_learnings(experiment_id=self.eid)
        self.assertEqual(len(ls), 1)
        self.assertEqual(ls[0]["delta"], 0.3)
        self.assertEqual(ls[0]["improved"], 1)
        self.assertTrue(self.store.delete_learning(lid))
        self.assertEqual(self.store.list_learnings(self.eid), [])

    def test_list_by_metric_across_experiments(self):
        other = self.store.create_experiment("o", "", "acc", 0.9, True)
        self.store.add_learning(other, None, "acc", 0.1, 0.2, 0.1, 1, "s", "suggestion")
        self.store.add_learning(self.eid, None, "f1", 0.1, 0.2, 0.1, 1, "s2", "suggestion")
        acc = self.store.list_learnings(metric="acc")
        self.assertEqual(len(acc), 1)

    def test_record_suggestion_learning(self):
        sid = self.store.add_suggestions(self.eid, self.rid, {
            "suggestions": [{"title": "try eps=2", "action": "a", "prompt": "p"}]})[0]
        rid2 = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                  metrics={"acc": 0.8}, experiment_id=self.eid)
        self.store.mark_suggestion_applied(sid, rid2)
        out = self.store.resolve_suggestion_outcome(sid)
        lid = self.store.record_suggestion_learning(out)
        self.assertIsNotNone(lid)
        ls = self.store.list_learnings(experiment_id=self.eid)
        self.assertEqual(len(ls), 1)
        self.assertIn("try eps=2", ls[0]["summary"])
        self.assertIn("improved", ls[0]["summary"])

    def test_record_suggestion_learning_ignores_unresolved(self):
        self.assertIsNone(self.store.record_suggestion_learning({"status": "pending"}))


class LearningInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def _runtime(self, dir=None):
        from backend.project_runtime import ProjectRuntime
        rt = object.__new__(ProjectRuntime)  # skip the heavy constructor
        rt.store = self.store
        rt.dir = dir or self.tmp
        return rt

    def _ctx(self):
        rt = self._runtime()
        self.store.add_message("user", "go", {"tags": []})
        msgs = rt.build_llm_messages()
        return msgs[0]["content"]

    def test_experiment_context_includes_prior_learnings(self):
        eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.store.add_learning(eid, None, "acc", 0.5, 0.8, 0.3, 1,
                                "Tried eps=2: acc 0.5->0.8 — improved.", "suggestion")
        ctx = self._ctx()
        self.assertIn("Prior learnings", ctx)
        self.assertIn("eps=2", ctx)

    def test_reviewer_context_includes_prior_learnings(self):
        eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.store.add_learning(eid, None, "acc", 0.5, 0.8, 0.3, 1,
                                "Tried eps=2: acc 0.5->0.8 — improved.", "suggestion")
        run = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.8}, experiment_id=eid)
        ctx = build_review_context(self.store, self.store.get_run(run))
        self.assertIn("Prior learnings", ctx)
        self.assertIn("eps=2", ctx)


class LearningLoopCaptureTests(unittest.IsolatedAsyncioTestCase):
    """The improve loop records a learning when it resolves an applied suggestion."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        from backend.artifacts.store import ArtifactStore
        from backend.agents.tools import ToolContext
        from backend.permissions import PermissionManager
        from tests.test_coordinator import FakeKernels
        self.artifacts = ArtifactStore(self.tmp)
        self.ctx = ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.emitted = []

    async def _emit(self, t, p):
        self.emitted.append((t, p))

    def _coordinator(self, llm):
        from backend.agents.coordinator import Coordinator
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

    async def test_loop_records_learnings(self):
        from backend.experiment_loop import run_improve_loop
        eid = self.store.create_experiment("eps sweep", "h", "accuracy", 0.99, True)
        review = {"findings": [], "suggestions": [
            {"title": "try eps=2", "action": "rerun",
             "prompt": "Start variant 'eps=2' with config {eps:2}, rerun."}]}
        reviewer = ScriptedReviewer([review, review, review])
        await run_improve_loop(
            self.store, self._coordinator(FakeLLM()), self._build_llm_messages,
            reviewer, eid, "Improve it.", emit=self._emit, iterations=2)
        ls = self.store.list_learnings(experiment_id=eid)
        self.assertTrue(ls, "applied suggestions should become learnings")
        self.assertIn("eps=2", ls[0]["summary"])


if __name__ == "__main__":
    unittest.main()
