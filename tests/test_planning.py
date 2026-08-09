"""Round-30: experiment planning — plan→steps, goal/plan proposal, store CRUD,
and the plan-step run intent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.planning import (default_plan, plan_to_steps, propose_plan,
                              step_prompt)
from backend.store import ProjectStore


class TestPlanToSteps(unittest.TestCase):
    def test_numbered_lines(self):
        steps = plan_to_steps("1. Load the data\n2. Sweep n_estimators\n3. Evaluate")
        self.assertEqual([s["title"] for s in steps],
                         ["Load the data", "Sweep n_estimators", "Evaluate"])

    def test_bullets_and_step_prefix(self):
        steps = plan_to_steps("- clean data\n* train a model\nStep 3: compare")
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[2]["title"], "compare")

    def test_paragraph_becomes_one_step(self):
        steps = plan_to_steps("Load the data, then sweep the learning rate and "
                              "evaluate the best model.")
        self.assertEqual(len(steps), 1)
        self.assertIn("sweep", steps[0]["plan"])

    def test_kind_classification(self):
        steps = plan_to_steps("1. clean the dataset\n2. sweep lr\n3. finetune\n4. evaluate")
        kinds = [s["kind"] for s in steps]
        self.assertEqual(kinds, ["data", "sweep", "finetune", "eval"])

    def test_empty_falls_back_to_hypothesis_baseline(self):
        steps = plan_to_steps("", hypothesis="Does dropout help?")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["title"], "Baseline")
        self.assertEqual(steps[0]["hypothesis"], "Does dropout help?")

    def test_empty_returns_empty(self):
        self.assertEqual(plan_to_steps("", ""), [])

    def test_caps_steps(self):
        text = "\n".join(f"{i}. step {i}" for i in range(1, 12))
        self.assertLessEqual(len(plan_to_steps(text)), 6)


class TestDefaultPlan(unittest.TestCase):
    def test_three_steps(self):
        plan = default_plan({"goal_metric": "acc", "hypothesis": "h"})
        self.assertEqual(len(plan), 3)
        self.assertIn("acc", plan[0]["plan"])


class TestStepPrompt(unittest.TestCase):
    def test_prompt_includes_step_and_goal(self):
        prompt = step_prompt({"goal_metric": "f1", "higher_better": True},
                             {"step_order": 2, "title": "Sweep lr",
                              "hypothesis": "", "plan": "try lr in [1e-3, 1e-4]"})
        self.assertIn("Plan step 2: Sweep lr", prompt)
        self.assertIn("f1", prompt)
        self.assertIn("1e-3", prompt)


class TestProposePlan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.eid = self.store.create_experiment(
            "exp", "Does LR help?", goal_metric="acc", goal_target=0.9,
            plan="1. baseline\n2. sweep lr")

    async def test_falls_back_when_llm_unavailable(self):
        class Boom:
            async def complete(self, *a, **k):
                raise RuntimeError("no llm")
        prop = await propose_plan(self.store, Boom(), self.store.get_experiment(self.eid))
        self.assertTrue(prop["steps"])
        self.assertEqual(prop["goal_metric"], "acc")

    async def test_parses_llm_proposal(self):
        class Fake:
            async def complete(self, *a, **k):
                return {"content": (
                    '{"goal_metric": "acc", "goal_target": 0.95, "higher_better": true, '
                    '"plan_text": "sweep lr then confirm", '
                    '"steps": [{"title": "Sweep lr", "kind": "sweep", '
                    '"hypothesis": "lower lr helps", "plan": "try [1e-3,1e-4]"}]}')}
        exp = self.store.get_experiment(self.eid)
        prop = await propose_plan(self.store, Fake(), exp)
        self.assertEqual(prop["goal_metric"], "acc")
        self.assertEqual(prop["goal_target"], 0.95)
        self.assertEqual(len(prop["steps"]), 1)
        self.assertEqual(prop["steps"][0]["kind"], "sweep")


class TestExperimentPlanStore(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.eid = self.store.create_experiment("e", "h", "acc", 0.9)

    def test_replace_and_list(self):
        ids = self.store.replace_experiment_plan(
            self.eid, [{"title": "a", "kind": "data", "plan": "clean"},
                       {"title": "b", "kind": "model"}])
        self.assertEqual(len(ids), 2)
        steps = self.store.list_experiment_steps(self.eid)
        self.assertEqual([s["title"] for s in steps], ["a", "b"])
        self.assertEqual(steps[0]["status"], "planned")

    def test_replace_clears_old(self):
        self.store.replace_experiment_plan(
            self.eid, [{"title": "a"}, {"title": "b"}])
        self.store.replace_experiment_plan(self.eid, [{"title": "c"}])
        steps = self.store.list_experiment_steps(self.eid)
        self.assertEqual([s["title"] for s in steps], ["c"])

    def test_update_step_status_and_run(self):
        ids = self.store.replace_experiment_plan(
            self.eid, [{"title": "a", "kind": "model", "plan": "train"}])
        sid = ids[0]
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0, metrics={"acc": 0.8},
                                 experiment_id=self.eid)
        self.store.update_experiment_step(sid, status="done", run_id=rid)
        step = self.store.get_experiment_step(sid)
        self.assertEqual(step["status"], "done")
        self.assertEqual(step["run_id"], rid)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get_experiment_step(9999))


if __name__ == "__main__":
    unittest.main()
