"""Round-trip tests for the SQLite project store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.store import ProjectStore, connect_project_db
from backend.artifacts.store import ArtifactStore


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_messages_roundtrip(self):
        mid = self.store.add_message("user", "hello", {"tags": ["x"]})
        msgs = self.store.list_messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "hello")
        self.assertEqual(msgs[0]["meta"]["tags"], ["x"])
        self.assertEqual(self.store.get_message(mid)["id"], mid)

    def test_grants_roundtrip(self):
        self.store.set_grant("run_shell", "ls", "allow")
        self.assertEqual(self.store.get_grant("run_shell", "ls"), "allow")
        self.assertIsNone(self.store.get_grant("run_shell", "nope"))

    def test_settings_roundtrip(self):
        self.store.set_setting("k", "v")
        self.assertEqual(self.store.get_setting("k"), "v")
        self.assertEqual(self.store.get_setting("missing", "d"), "d")

    def test_runs_roundtrip_with_metrics_and_review(self):
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.9}, review={"findings": []})
        run = self.store.get_run(rid)
        self.assertEqual(run["metrics"], {"acc": 0.9})
        self.store.update_run_review(rid, {"findings": [{"severity": "warn",
                                                         "message": "x"}]})
        self.assertEqual(self.store.get_run(rid)["review"]["findings"][0]["severity"],
                         "warn")
        runs = self.store.list_runs()
        self.assertTrue(any(r["id"] == rid for r in runs))
        # persists across a reopen (migration is idempotent)
        store2 = ProjectStore(self.tmp)
        self.assertEqual(store2.get_run(rid)["metrics"], {"acc": 0.9})

    def test_goals_roundtrip(self):
        self.store.add_goal("accuracy", 0.9, True, "90%")
        goals = self.store.list_goals()
        self.assertEqual(len(goals), 1)
        self.assertTrue(goals[0]["higher_better"])
        self.assertTrue(self.store.delete_goal("accuracy"))
        self.assertFalse(self.store.delete_goal("accuracy"))

    def test_goals_experiment_scoping(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.9, True)
        self.store.add_goal("accuracy", 0.9, True, "90%")
        self.store.add_goal("loss", 0.2, False, "low loss", experiment_id=eid)
        self.assertEqual(len(self.store.list_goals()), 2)
        scoped = self.store.goals_for_experiment(eid)
        self.assertEqual([g["metric"] for g in scoped], ["accuracy", "loss"])
        self.assertEqual(scoped[1]["experiment_id"], eid)
        # deleting with an experiment_id only removes that scoped row
        self.assertTrue(self.store.delete_goal("loss", experiment_id=eid))
        self.assertFalse(self.store.delete_goal("loss", experiment_id=eid))
        self.assertEqual(len(self.store.list_goals()), 1)
        # project-wide delete still works
        self.assertTrue(self.store.delete_goal("accuracy"))
        # migration is idempotent across a reopen
        store2 = ProjectStore(self.tmp)
        self.assertEqual(store2.list_goals(), [])

    def test_workflow_runs_roundtrip(self):
        self.store.add_workflow_run({"title": "t", "status": "done", "pct": 100,
                                     "stages": [{"name": "s"}]})
        rows = self.store.list_workflow_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stages"][0]["name"], "s")

    def test_experiments_roundtrip(self):
        eid = self.store.create_experiment(
            "DP vs synthetic", "Synthetic beats DP at ε=1", "accuracy", 0.9, True)
        exp = self.store.get_experiment(eid)
        self.assertEqual(exp["name"], "DP vs synthetic")
        self.assertEqual(exp["goal_metric"], "accuracy")
        self.assertEqual(exp["goal_target"], 0.9)
        self.assertTrue(exp["higher_better"])
        self.assertEqual(exp["status"], "active")
        self.assertEqual(self.store.list_experiments()[0]["runs"], 0)

    def test_experiment_plan_roundtrip(self):
        plan = ("Hypothesis: larger eps trades utility for privacy.\n"
                "Try configs: eps=0.1, 1.0, 5.0; seeds 1..3.\n"
                "Stop when accuracy >= 0.9 on the goal metric.")
        eid = self.store.create_experiment("eps sweep", "h", "accuracy", 0.9,
                                           True, plan=plan)
        exp = self.store.get_experiment(eid)
        self.assertEqual(exp["plan"], plan)

    def test_experiment_status_transitions(self):
        eid = self.store.create_experiment("sweep", "h", "accuracy", 0.9, True)
        self.assertEqual(self.store.get_experiment(eid)["status"], "active")
        self.store.update_experiment_status(eid, "completed")
        self.assertEqual(self.store.get_experiment(eid)["status"], "completed")
        self.store.update_experiment_status(eid, "active")
        self.assertEqual(self.store.get_experiment(eid)["status"], "active")
        # default plan for legacy callers
        eid2 = self.store.create_experiment("no plan", "h", "acc", 0.5, True)
        self.assertEqual(self.store.get_experiment(eid2)["plan"], "")

    def test_experiment_runs_and_config_linkage(self):
        eid = self.store.create_experiment("var", "hyp", "acc", 0.9, True)
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.8},
                                 experiment_id=eid,
                                 config={"eps": 1.0, "seed": 42})
        run = self.store.get_run(rid)
        self.assertEqual(run["experiment_id"], eid)
        self.assertEqual(run["config"], {"eps": 1.0, "seed": 42})
        exps = self.store.list_experiments()
        self.assertEqual(exps[0]["runs"], 1)
        self.assertEqual(self.store.experiment_runs(eid)[0]["id"], rid)
        self.store.set_run_experiment(rid, None, {})
        self.assertIsNone(self.store.get_run(rid)["experiment_id"])
        self.assertEqual(self.store.experiment_runs(eid), [])

    def test_run_label_roundtrip(self):
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 label="eps=1.0", config={"eps": 1.0})
        run = self.store.get_run(rid)
        self.assertEqual(run["label"], "eps=1.0")
        self.assertEqual(run["config"], {"eps": 1.0})
        # set_run_experiment can carry a label too
        eid = self.store.create_experiment("v", "h", "acc", 0.9, True)
        self.store.set_run_experiment(rid, eid, {"eps": 2.0}, "eps=2.0")
        run = self.store.get_run(rid)
        self.assertEqual(run["label"], "eps=2.0")
        self.assertEqual(run["experiment_id"], eid)

    def test_run_kind_and_count(self):
        self.store.add_run("p1", "r1", "done", 1.0, 2.0, kind="agent_run")
        self.store.add_run("p2", "r2", "done", 3.0, 4.0, kind="notebook",
                           metrics={"acc": 0.9}, label="nb1")
        self.assertEqual(self.store.count_runs(), 2)
        run = self.store.get_run(2)
        self.assertEqual(run["kind"], "notebook")
        self.assertEqual(run["label"], "nb1")
        # default kind for legacy callers
        rid = self.store.add_run("p3", "r3", "done", 5.0, 6.0)
        self.assertEqual(self.store.get_run(rid)["kind"], "agent_run")

    def test_approval_log_roundtrip(self):
        self.store.log_approval("run_shell", "ls", "allow", True)
        self.store.log_approval("run_shell", "rm -rf /", "deny", False)
        log = self.store.list_approvals()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["decision"], "deny")
        self.assertEqual(log[1]["temporary"], 1)

    def test_single_connection_shared_and_wal(self):
        # ProjectStore and ArtifactStore must share one connection per db.
        arts = ArtifactStore(self.tmp)
        self.assertIs(arts._conn, self.store._conn)
        self.assertIs(connect_project_db(self.tmp), self.store._conn)
        # WAL journal mode is active.
        mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()
