"""Round-trip tests for the SQLite project store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.store import ProjectStore


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

    def test_workflow_runs_roundtrip(self):
        self.store.add_workflow_run({"title": "t", "status": "done", "pct": 100,
                                     "stages": [{"name": "s"}]})
        rows = self.store.list_workflow_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stages"][0]["name"], "s")

    def test_approval_log_roundtrip(self):
        self.store.log_approval("run_shell", "ls", "allow", True)
        self.store.log_approval("run_shell", "rm -rf /", "deny", False)
        log = self.store.list_approvals()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["decision"], "deny")
        self.assertEqual(log[1]["temporary"], 1)


if __name__ == "__main__":
    unittest.main()
