"""B4 goal-scoping tests: goals scoped to an experiment only fire for that
experiment's runs, and an experiment's own goal_metric takes precedence over a
project-wide goal on the same metric."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from backend.main import goal_notices
from backend.store import ProjectStore


class StubRT:
    def __init__(self, store):
        self.store = store


class GoalScopingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ProjectStore(Path(self.tmp) / "workbench.db")
        self.rt = StubRT(self.store)

    def _run(self, rid, metrics, eid=None):
        return {"id": rid, "metrics": metrics, "experiment_id": eid}

    def _db_run(self, metrics, eid=None):
        return self.store.add_run(
            "prompt", "reply", "done", time.time(), time.time(),
            metrics=metrics, experiment_id=eid)

    def test_project_goal_fires_for_unscoped_run(self):
        self.store.add_goal("accuracy", 0.9, True, "90%")
        notices = goal_notices(self.rt, self._run(1, {"accuracy": 0.95}))
        self.assertEqual(len(notices), 1)
        self.assertIn("target reached", notices[0])

    def test_scoped_goal_only_fires_for_its_experiment(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.9, True)
        self.store.add_goal("loss", 0.2, False, "low loss", experiment_id=eid)
        scoped = goal_notices(self.rt, self._run(1, {"loss": 0.1}, eid=eid))
        self.assertEqual(len(scoped), 1)
        self.assertIn("low loss", scoped[0])
        other = goal_notices(self.rt, self._run(2, {"loss": 0.1}, eid=None))
        self.assertEqual(other, [])

    def test_scoped_goal_ignored_for_other_experiment(self):
        e1 = self.store.create_experiment("a", "h", "accuracy", 0.9, True)
        e2 = self.store.create_experiment("b", "h", "loss", 0.2, False)
        self.store.add_goal("loss", 0.2, False, "low loss", experiment_id=e2)
        notices = goal_notices(self.rt, self._run(1, {"loss": 0.1}, eid=e1))
        self.assertEqual(notices, [])

    def test_experiment_goal_takes_precedence_over_project_goal(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.9, True)
        self.store.add_goal("accuracy", 0.9, True, "90%")
        notices = goal_notices(self.rt, self._run(1, {"accuracy": 0.95}, eid=eid))
        self.assertEqual(notices, [])

    def test_new_best_cmp_uses_db_runs(self):
        eid = self.store.create_experiment("sweep", "h", "loss", 0.2, False)
        self.store.add_goal("accuracy", 0.9, True, "90%")
        self._db_run({"accuracy": 0.7}, eid=eid)
        notices = goal_notices(self.rt, self._run(9, {"accuracy": 0.8}, eid=eid))
        self.assertEqual(len(notices), 1)
        self.assertIn("new best", notices[0])
        self.assertIn("run #1", notices[0])


if __name__ == "__main__":
    unittest.main()
