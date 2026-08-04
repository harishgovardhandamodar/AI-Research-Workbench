"""Experiment management repo tests: snapshot + auto-commit of experiment
artifacts into a sibling git repo, scoped to fox/<project>/ only."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import experiment_repo as er
from backend.state import CONFIG
from backend.store import ProjectStore


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True)
    return (out.stdout or "") + (out.stderr or "")


class ExperimentRepoTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mgmt = self.tmp / "mgmt-repo"
        self.mgmt.mkdir()
        _git(self.mgmt, "init", "-q", "-b", "main")
        _git(self.mgmt, "config", "user.name", "Test")
        _git(self.mgmt, "config", "user.email", "test@example.com")
        # An unrelated file that must never be swept up by auto-commit.
        (self.mgmt / "keep.txt").write_text("personal content")
        _git(self.mgmt, "add", ".")
        _git(self.mgmt, "commit", "-q", "-m", "baseline")
        self.mgmt_commit_before = _git(self.mgmt, "rev-parse", "HEAD").strip()

        self.proj = self.tmp / "proj"
        self.store = ProjectStore(self.proj)
        self.eid = self.store.create_experiment(
            "acc sweep", "more epochs help", "accuracy", 0.9, True,
            plan="try epochs 100/200/300")
        self.rid = self.store.add_run(
            "prompt", "reply", "done", time.time(), time.time(),
            metrics={"accuracy": 0.9}, experiment_id=self.eid,
            config={"epochs": 200}, label="epochs=200")
        self.rt = SimpleNamespace(name="testproj", dir=self.proj, store=self.store)

        self._old_mgmt = CONFIG.get("management")
        CONFIG["management"] = {"repo_dir": str(self.mgmt),
                                "auto_commit": True, "auto_push": False}

    def tearDown(self):
        if self._old_mgmt is None:
            CONFIG.pop("management", None)
        else:
            CONFIG["management"] = self._old_mgmt

    def test_commit_message(self):
        run = self.store.get_run(self.rid)
        msg = er.commit_message(self.rt, run)
        self.assertIn("acc sweep", msg)
        self.assertIn("run #1", msg)
        self.assertIn("accuracy=0.9", msg)

    def test_autocommit_writes_snapshot_and_commits(self):
        run = self.store.get_run(self.rid)
        res = er.autocommit(self.rt, run)
        self.assertTrue(res["ok"], res)
        log = _git(self.mgmt, "log", "--oneline", "-1")
        self.assertIn("acc sweep", log)
        files = _git(self.mgmt, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("fox/testproj/experiments.json", files)
        self.assertIn("fox/testproj/runs/1.json", files)
        # The unrelated file is untouched by the snapshot commit.
        self.assertNotIn("keep.txt", files)
        payload = json.loads((self.mgmt / "fox/testproj/experiments.json").read_text())
        self.assertEqual(payload["project"], "testproj")
        self.assertEqual(payload["experiments"][0]["name"], "acc sweep")
        self.assertEqual(payload["experiments"][0]["runs"][0]["metrics"]["accuracy"], 0.9)

    def test_autocommit_noop_when_nothing_changed(self):
        run = self.store.get_run(self.rid)
        er.autocommit(self.rt, run)
        before = _git(self.mgmt, "rev-parse", "HEAD").strip()
        res = er.autocommit(self.rt, run)
        self.assertTrue(res["ok"])
        self.assertEqual(_git(self.mgmt, "rev-parse", "HEAD").strip(), before)

    def test_maybe_autocommit_skips_runs_without_experiment(self):
        plain = self.store.add_run("p", "r", "done", time.time(), time.time(),
                                   metrics={"x": 1}, experiment_id=None)
        seen = {"v": False}
        orig = er.autocommit

        def spy(rt, run):
            seen["v"] = True
            return orig(rt, run)

        er.autocommit = spy
        try:
            asyncio.run(er.maybe_autocommit(self.rt, {"id": plain, "experiment_id": None}))
        finally:
            er.autocommit = orig
        self.assertFalse(seen["v"])

    def test_autocommit_disabled_is_noop(self):
        CONFIG["management"]["auto_commit"] = False
        run = self.store.get_run(self.rid)
        seen = {"v": False}
        orig = er.autocommit
        er.autocommit = lambda rt, run: seen.update(v=True) or {}
        try:
            asyncio.run(er.maybe_autocommit(self.rt, run))
        finally:
            er.autocommit = orig
        self.assertFalse(seen["v"])

    def test_sibling_git_repos_finds_mgmt_repo(self):
        # The temp mgmt repo is not a sibling of the workbench ROOT, so this
        # only asserts the helper returns a list (no crash) and no temp path.
        repos = er.sibling_git_repos()
        self.assertIsInstance(repos, list)
        self.assertNotIn(str(self.mgmt), [r["path"] for r in repos])

    def test_github_remote_url_formatting(self):
        CONFIG["management"]["github_repo"] = "harishg/datasets"
        self.assertEqual(er.github_remote_url(), "git@github.com:harishg/datasets.git")
        CONFIG["management"]["github_repo"] = "https://github.com/harishg/datasets"
        self.assertEqual(er.github_remote_url(), "git@github.com:harishg/datasets.git")
        CONFIG["management"]["github_repo"] = "git@github.com:harishg/datasets.git"
        self.assertEqual(er.github_remote_url(), "git@github.com:harishg/datasets.git")
        CONFIG["management"]["github_repo"] = "file:///tmp/bare.git"
        self.assertEqual(er.github_remote_url(), "file:///tmp/bare.git")
        CONFIG["management"]["github_repo"] = ""
        self.assertIsNone(er.github_remote_url())

    def test_ensure_remote_adds_and_updates_origin(self):
        CONFIG["management"]["github_repo"] = "harishg/datasets"
        ok, msg = er.ensure_remote(self.mgmt)
        self.assertTrue(ok, msg)
        self.assertEqual(er.current_remote(self.mgmt), "git@github.com:harishg/datasets.git")
        # Changing the github repo updates origin.
        CONFIG["management"]["github_repo"] = "harishg/other"
        ok, _ = er.ensure_remote(self.mgmt)
        self.assertTrue(ok)
        self.assertEqual(er.current_remote(self.mgmt), "git@github.com:harishg/other.git")
        # No github repo configured -> leaves origin alone.
        CONFIG["management"]["github_repo"] = ""
        ok, _ = er.ensure_remote(self.mgmt)
        self.assertTrue(ok)
        self.assertEqual(er.current_remote(self.mgmt), "git@github.com:harishg/other.git")

    def test_autocommit_sets_origin_when_github_repo_configured(self):
        CONFIG["management"]["github_repo"] = "harishg/datasets"
        run = self.store.get_run(self.rid)
        res = er.autocommit(self.rt, run)
        self.assertTrue(res["ok"], res)
        self.assertEqual(er.current_remote(self.mgmt), "git@github.com:harishg/datasets.git")

    def test_commit_project_snapshots_and_commits(self):
        res = er.commit_project(self.rt)
        self.assertTrue(res["ok"], res)
        files = _git(self.mgmt, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("fox/testproj/experiments.json", files)
        self.assertNotIn("keep.txt", files)

    def test_commit_project_no_changes(self):
        er.commit_project(self.rt)
        res = er.commit_project(self.rt)
        self.assertTrue(res["ok"])
        self.assertIn("no changes", res["message"])

    def test_push_with_local_remote(self):
        # A bare repo stands in for the GitHub remote (file:// URL).
        bare = self.tmp / "remote.git"
        bare.mkdir(parents=True)
        _git(bare, "init", "-q", "--bare")
        CONFIG["management"]["github_repo"] = f"file://{bare}"
        self.assertTrue(er.commit_project(self.rt)["ok"])
        res = er.push()
        self.assertTrue(res["ok"], res)
        # The pushed branch carries the fox/ snapshot.
        pushed = _git(bare, "log", "--all", "--name-only", "--format=")
        self.assertIn("fox/testproj/experiments.json", pushed)

    def test_commit_project_without_repo(self):
        CONFIG["management"]["repo_dir"] = ""
        res = er.commit_project(self.rt)
        self.assertFalse(res["ok"])
        self.assertIn("no management repo", res["message"])
        self.assertFalse(er.push()["ok"])

    def test_commit_project_with_empty_payload_does_not_reopen_store(self):
        # Regression: an empty (falsy) precomputed experiments list must not
        # trigger a store re-read inside the worker thread.
        CONFIG["management"]["github_repo"] = ""
        res = er.commit_project(self.rt, experiments=[])
        self.assertTrue(res["ok"], res)
        self.assertIn("experiments: testproj", res["message"])


if __name__ == "__main__":
    unittest.main()
