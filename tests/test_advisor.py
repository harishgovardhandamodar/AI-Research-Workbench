"""Round-28 research advisor: typed suggestion categories + deterministic
experiment advisor (goal proposal, missing elements, improvements,
hyperparameters, data pipeline, model selection, finetune readiness)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.advisor import experiment_advisor
from backend.agents.reviewer import _normalize_suggestion, suggest_category


class TestSuggestionCategory(unittest.TestCase):
    def test_explicit_category_preserved(self):
        s = _normalize_suggestion({"title": "Try eps=1.0", "action": "a",
                                   "prompt": "p", "category": "hyperparameter"})
        self.assertEqual(s["category"], "hyperparameter")

    def test_keyword_hyperparameter_fallback(self):
        s = _normalize_suggestion({"title": "Try a lower learning rate"})
        self.assertEqual(s["category"], "hyperparameter")

    def test_keyword_data_fallback(self):
        s = _normalize_suggestion({"title": "Add more training data"})
        self.assertEqual(s["category"], "data")

    def test_keyword_finetune_fallback(self):
        s = _normalize_suggestion("Finetune the model on the project data")
        self.assertEqual(s["category"], "finetune")

    def test_unknown_defaults_to_other(self):
        s = _normalize_suggestion({"title": "Write a nicer report"})
        self.assertEqual(s["category"], "other")

    def test_suggest_category_helper(self):
        self.assertEqual(suggest_category(title="tune max_depth to 6"), "hyperparameter")


class TestExperimentAdvisor(unittest.TestCase):
    def setUp(self):
        from backend.store import ProjectStore

        self.tmp = tempfile.mkdtemp()
        self.store = ProjectStore(Path(self.tmp))
        self.eid = self.store.create_experiment(
            "acc boost", hypothesis="Does LR help?",
            goal_metric="accuracy", goal_target=0.9, higher_better=True,
            plan="try lr variants", model="qwen")

    def _run(self, metrics, dataset="real", model="qwen",
             config=None, tool_sequence=None):
        return self.store.add_run(
            prompt="run", reply="ok", status="done",
            started_at=1000, finished_at=1100,
            tool_sequence=tool_sequence or [],
            artifact_ids=[], metrics=metrics, experiment_id=self.eid,
            config=config or {}, label="run", kind="agent_run",
            model=model, dataset=dataset)

    def test_advisor_goal_alignment(self):
        self._run({"accuracy": 0.8}, config={"lr": 0.01})
        self._run({"accuracy": 0.85}, config={"lr": 0.001})
        a = experiment_advisor(self.store, self.eid)
        self.assertEqual(a["goal"]["metric"], "accuracy")
        self.assertEqual(a["goal"]["best"], 0.85)
        self.assertAlmostEqual(a["goal"]["pct_target"], 94.4, delta=0.1)
        self.assertFalse(a["goal"]["reached"])

    def test_advisor_reached_target(self):
        self._run({"accuracy": 0.95})
        a = experiment_advisor(self.store, self.eid)
        self.assertTrue(a["goal"]["reached"])

    def test_advisor_proposes_goal_metric_when_missing(self):
        eid = self.store.create_experiment("no goal exp")
        self.store.add_run(
            prompt="p", reply="r", status="done", started_at=1,
            finished_at=2, tool_sequence=[], artifact_ids=[],
            metrics={"f1": 0.7}, experiment_id=eid, config={}, label="",
            kind="agent_run", model="", dataset="")
        a = experiment_advisor(self.store, eid)
        self.assertEqual(a["goal"]["proposed"], "f1")
        self.assertIn("goal_metric", [m["key"] for m in a["missing"]])

    def test_advisor_missing_elements_covered(self):
        eid = self.store.create_experiment("bare exp")
        a = experiment_advisor(self.store, eid)
        keys = {m["key"] for m in a["missing"]}
        for k in ("hypothesis", "goal_metric", "plan", "model", "runs"):
            self.assertIn(k, keys)
        # goal_target is only flagged once a goal_metric exists.
        self.assertNotIn("goal_target", keys)

    def test_advisor_flags_target_when_metric_set(self):
        eid = self.store.create_experiment("targetless", goal_metric="acc")
        a = experiment_advisor(self.store, eid)
        keys = {m["key"] for m in a["missing"]}
        self.assertIn("goal_target", keys)

    def test_advisor_data_and_model(self):
        self._run({"accuracy": 0.8}, dataset="synthetic",
                  model="qwen", tool_sequence=[{"name": "read_csv", "ok": True}])
        a = experiment_advisor(self.store, self.eid)
        self.assertEqual(a["data"]["datasets"], ["synthetic"])
        self.assertIn("read_csv", a["data"]["tools"])
        self.assertEqual(a["model"]["pinned"], "qwen")
        self.assertIn("qwen", a["model"]["used"])

    def test_advisor_groups_suggestions_by_category(self):
        self._run({"accuracy": 0.8})
        self.store.add_suggestions(self.eid, 1, {
            "suggestions": [
                {"title": "Try lr=0.001", "category": "hyperparameter"},
                {"title": "Add more data", "category": "data"},
            ]})
        a = experiment_advisor(self.store, self.eid)
        cats = a["improvements"]["by_category"]
        self.assertIn("hyperparameter", cats)
        self.assertIn("data", cats)
        self.assertEqual(a["hyperparameters"]["suggestions"][0]["category"],
                         "hyperparameter")

    def test_advisor_finetune_readiness(self):
        self._run({"accuracy": 0.8})
        a = experiment_advisor(self.store, self.eid)
        # Hypothesis, goal, target, plan, model, dataset all set → ready.
        self.assertTrue(a["finetune"]["ready"])
        self.assertEqual(len(a["finetune"]["checklist"]), 4)

    def test_advisor_unknown_experiment(self):
        with self.assertRaises(KeyError):
            experiment_advisor(self.store, 9999)


if __name__ == "__main__":
    unittest.main()
