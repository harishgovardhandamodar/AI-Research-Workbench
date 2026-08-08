"""Round-2 goal-steering tests: objective refinement, goal-first context,
focus experiment, distance-to-target ranking, and reviewer grounding."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from backend.agents.reviewer import Reviewer, build_review_context
from backend.experiments import rank_runs
from backend.store import ProjectStore, _UNSET


class UpdateExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_edit_fields_in_place(self):
        eid = self.store.create_experiment("old name", "old hyp", "acc", 0.8, True,
                                           plan="old plan")
        self.store.update_experiment(
            eid, name="new name", hypothesis="new hyp",
            goal_metric="f1", goal_target=0.95, higher_better=True, plan="new plan")
        e = self.store.get_experiment(eid)
        self.assertEqual(e["name"], "new name")
        self.assertEqual(e["hypothesis"], "new hyp")
        self.assertEqual(e["goal_metric"], "f1")
        self.assertEqual(e["goal_target"], 0.95)
        self.assertEqual(e["plan"], "new plan")

    def test_edit_bumps_updated_at(self):
        eid = self.store.create_experiment("e", "", "acc", 0.8, True)
        first = self.store.get_experiment(eid)["updated_at"]
        time.sleep(0.01)
        self.store.update_experiment(eid, hypothesis="better hypothesis")
        self.assertGreater(self.store.get_experiment(eid)["updated_at"], first)

    def test_clear_goal_target(self):
        eid = self.store.create_experiment("e", "", "acc", 0.8, True)
        self.store.update_experiment(eid, goal_target=_UNSET)
        self.assertIsNone(self.store.get_experiment(eid)["goal_target"])

    def test_run_bumps_experiment_updated_at(self):
        eid = self.store.create_experiment("e", "", "acc", 0.8, True)
        first = self.store.get_experiment(eid)["updated_at"]
        time.sleep(0.01)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.7}, experiment_id=eid)
        self.assertGreater(self.store.get_experiment(eid)["updated_at"], first)


class PatchExperimentValidationTests(unittest.TestCase):
    """Exercise the PATCH route logic with a stubbed runtime."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("e", "", "acc", 0.8, True)

    def _patch(self, body: dict):
        from backend import routers
        from backend.routers import runs as runs_mod

        orig = runs_mod.get_runtime
        runs_mod.get_runtime = lambda name: _FakeRt(self.store)
        try:
            return asyncio.run(
                runs_mod.update_project_experiment("proj", self.eid, body))
        finally:
            runs_mod.get_runtime = orig

    def test_edit_objective_fields(self):
        r = self._patch({"name": "n2", "hypothesis": "h2",
                         "goal_metric": "f1", "goal_target": 0.9,
                         "higher_better": True, "plan": "p2"})
        e = r["experiment"]
        self.assertEqual(e["name"], "n2")
        self.assertEqual(e["goal_metric"], "f1")
        self.assertEqual(e["goal_target"], 0.9)

    def test_target_requires_metric(self):
        with self.assertRaises(HTTPException):
            self._patch({"goal_metric": "", "goal_target": 0.9})

    def test_bad_target_rejected(self):
        with self.assertRaises(HTTPException):
            self._patch({"goal_metric": "f1", "goal_target": "not-a-number"})

    def test_clear_target(self):
        r = self._patch({"goal_metric": "acc", "goal_target": None})
        self.assertIsNone(r["experiment"]["goal_target"])


class _FakeRt:
    def __init__(self, store):
        self.store = store


class RankRunsTargetTests(unittest.TestCase):
    def _runs(self):
        return [
            {"id": 1, "metrics": {"acc": 0.7}, "config": {}, "label": "a"},
            {"id": 2, "metrics": {"acc": 0.9}, "config": {}, "label": "b"},
            {"id": 3, "metrics": {"acc": 0.6}, "config": {}, "label": "c"},
        ]

    def test_to_target_column(self):
        rank = rank_runs(self._runs(), "acc", True, goal_target=0.95)
        self.assertEqual(rank["goal_target"], 0.95)
        by_id = {r["run_id"]: r for r in rank["rows"]}
        self.assertAlmostEqual(by_id[1]["to_target"], 0.25)
        self.assertAlmostEqual(by_id[2]["to_target"], 0.05)
        self.assertAlmostEqual(by_id[2]["pct_target"], 0.9 / 0.95 * 100)

    def test_reached_row(self):
        runs = [{"id": 9, "metrics": {"acc": 1.0}, "config": {}, "label": "x"}]
        rank = rank_runs(runs, "acc", True, goal_target=0.95)
        self.assertAlmostEqual(rank["rows"][0]["to_target"], -0.05)

    def test_no_target_omits_columns(self):
        rank = rank_runs(self._runs(), "acc", True)
        self.assertIsNone(rank["goal_target"])
        self.assertNotIn("to_target", rank["rows"][0])


class GoalFirstContextTests(unittest.TestCase):
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

    def test_context_merges_goals_panel_goals(self):
        eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.store.add_goal("f1", 0.85, True, "f1 score", eid)
        self.store.add_goal("loss", 0.1, False, "loss", None)  # project-wide
        ctx = self._ctx()
        self.assertIn("Additional goals", ctx)
        self.assertIn("f1", ctx)
        self.assertIn("loss", ctx)

    def test_context_lists_project_data(self):
        self.store.create_experiment("e", "", "acc", 0.9, True)
        (self.tmp / "titanic.csv").write_text("a,b\n1,2\n")
        (self.tmp / "data").mkdir(exist_ok=True)
        (self.tmp / "data" / "extra.csv").write_text("x\n1\n")
        ctx = self._ctx()
        self.assertIn("Available project data", ctx)
        self.assertIn("titanic.csv", ctx)
        self.assertIn("data/extra.csv", ctx)

    def test_context_reports_distance_to_target(self):
        eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.45}, experiment_id=eid)
        ctx = self._ctx()
        self.assertIn("50% of target", ctx)

    def test_context_reports_target_reached(self):
        eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.95}, experiment_id=eid)
        ctx = self._ctx()
        self.assertIn("target reached", ctx)

    def test_context_cross_experiment_memory(self):
        a = self.store.create_experiment("A", "", "acc", 0.9, True)
        b = self.store.create_experiment("B", "", "acc", 0.8, True)
        self.store.set_setting("focus_experiment_id", str(a))
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.5}, experiment_id=a)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.99}, experiment_id=b)
        ctx = self._ctx()
        self.assertIn("Focused experiment context", ctx)
        self.assertIn("A", ctx)
        self.assertIn("Best acc across experiments", ctx)
        self.assertIn("0.99", ctx)

    def test_focus_prefers_focused_experiment(self):
        a = self.store.create_experiment("A", "", "acc", 0.9, True)
        self.store.create_experiment("B", "", "f1", 0.8, True)
        self.store.set_setting("focus_experiment_id", str(a))
        ctx = self._ctx()
        self.assertIn("Focused experiment context", ctx)
        self.assertIn("A", ctx)


class ReviewerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("e", "", "acc", 0.9, True)
        self.rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                      metrics={"acc": 0.8}, experiment_id=self.eid)

    def test_build_review_context_has_goal_and_metrics(self):
        run = self.store.get_run(self.rid)
        ctx = build_review_context(self.store, run)
        self.assertIn("Experiment context", ctx)
        self.assertIn("acc", ctx)
        self.assertIn("This run's metrics", ctx)
        self.assertIn("0.8", ctx)

    def test_review_prompt_includes_context(self):
        captured = {}

        class FakeLLM:
            async def complete(self, messages, temperature=None, tools=None):
                captured["prompt"] = messages[0]["content"]
                return {"content": "{}"}

        self.store.add_message("user", "go", {"tags": []})
        rev = Reviewer(FakeLLM(), self.store)
        asyncio.run(rev.review(build_review_context(self.store, self.store.get_run(self.rid))))
        self.assertIn("Experiment context", captured["prompt"])
        self.assertIn("This run's metrics", captured["prompt"])
        self.assertIn("Transcript:", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
