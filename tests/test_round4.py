"""Round-4 tests: git-backed run lineage, full-code capture, per-run env,
run_diff code sections, and management-repo commit info."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.experiment_repo import _path_commit_info, restore_run, run_commit_info
from backend.experiments import run_diff
from backend.permissions import PermissionManager
from backend.store import ProjectStore

from tests.test_coordinator import FakeKernels


class ProvenanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))

    def test_add_run_provenance_fields(self):
        rid = self.store.add_run(
            "p", "r", "done", 1.0, 2.0,
            metrics={"acc": 0.7}, experiment_id=None,
            git_commit="abc123", code=[{"name": "run_python", "code": "print(1)"}],
            env={"python": "3.12", "numpy": "2.0"})
        run = self.store.get_run(rid, include_code=True)
        self.assertEqual(run["git_commit"], "abc123")
        self.assertEqual(run["env"]["python"], "3.12")
        self.assertEqual(run["code"][0]["code"], "print(1)")

    def test_code_excluded_from_bulk_rows(self):
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           code=[{"name": "run_python", "code": "secret"}],
                           env={"python": "3.12"})
        listed = self.store.list_runs()
        self.assertNotIn("code", listed[0])
        self.assertIn("env", listed[0])
        self.assertIn("git_commit", listed[0])

    def test_set_run_git_commit(self):
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0)
        self.store.set_run_git_commit(rid, "deadbeef")
        self.assertEqual(self.store.get_run(rid)["git_commit"], "deadbeef")


class FullCodeCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_records_full_code_and_env(self):
        tmp = Path(tempfile.mkdtemp())
        store = ProjectStore(tmp)
        artifacts = ArtifactStore(tmp)
        ctx = ToolContext(kernels=FakeKernels(), artifacts=artifacts, store=store,
                          permissions=PermissionManager(store))
        records = []

        class LLM:
            calls = 0

            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": {"code": "print('hi')\nx = 42"}}}]}
                return {"role": "assistant", "content": "done"}

        coord = Coordinator(LLM(), ctx, emit=None,
                            persist=lambda r, c, m: None,
                            record=lambda r: records.append(r), max_iters=2, mcp=None)
        await coord.run_turn([{"role": "user", "content": "run it"}])
        self.assertTrue(records)
        code = records[0].get("code") or []
        self.assertTrue(code)
        self.assertEqual(code[0]["name"], "run_python")
        self.assertIn("print('hi')", code[0]["code"])
        self.assertTrue(records[0].get("env"), "env snapshot should be captured")
        # The stored run carries the code when fetched in detail mode.
        rid = store.add_run(
            prompt="x", reply="y", status="done", started_at=1.0, finished_at=2.0,
            code=code, env=records[0].get("env"))
        self.assertEqual(store.get_run(rid, include_code=True)["code"][0]["code"],
                         "print('hi')\nx = 42")


class RunDiffCodeTests(unittest.TestCase):
    def test_code_diff_generated(self):
        a = {"id": 1, "label": "base", "config": {}, "metrics": {"acc": 0.7},
             "prompt": "p", "tool_sequence": [{"name": "run_python", "ok": True}],
             "code": [{"name": "run_python",
                       "code": "import numpy\na = 1\nprint(a)"}]}
        b = {"id": 2, "label": "tuned", "config": {}, "metrics": {"acc": 0.8},
             "prompt": "p", "tool_sequence": [{"name": "run_python", "ok": True}],
             "code": [{"name": "run_python",
                       "code": "import numpy\na = 2\nprint(a)"}]}
        d = run_diff(a, b)
        self.assertTrue(d["code"]["available"])
        self.assertEqual(len(d["code"]["diffs"]), 1)
        patch = d["code"]["diffs"][0]["patch"]
        self.assertIn("-a = 1", patch)
        self.assertIn("+a = 2", patch)

    def test_code_added_run(self):
        a = {"id": 1, "label": "a", "config": {}, "metrics": {}, "prompt": "p",
             "tool_sequence": [], "code": []}
        b = {"id": 2, "label": "b", "config": {}, "metrics": {}, "prompt": "p",
             "tool_sequence": [{"name": "run_python", "ok": True}],
             "code": [{"name": "run_python", "code": "print('new')"}]}
        d = run_diff(a, b)
        self.assertEqual(d["code"]["diffs"][0]["added"], 1)
        self.assertEqual(d["code"]["diffs"][0]["removed"], 0)


class GitLineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "mgmt"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True,
                       capture_output=True)

    def _write_run_and_commit(self, rid=1):
        p = self.repo / "fox" / "proj" / "runs" / f"{rid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"id": %d}' % rid)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"run #{rid}"], cwd=self.repo, check=True,
                       capture_output=True)

    def test_path_commit_info(self):
        self._write_run_and_commit()
        info = _path_commit_info(self.repo, "proj", 1)
        self.assertTrue(info["commit_full"])
        self.assertEqual(info["message"], "run #1")
        self.assertTrue(len(info["commit"]) > 0)

    def test_run_commit_info_files(self):
        self._write_run_and_commit()
        info = run_commit_info(self.repo, "proj", 1, "")
        self.assertTrue(info["commit_full"])
        self.assertTrue(any("fox/proj" in f for f in info["files"]))

    def test_restore_run_checkouts_artifacts(self):
        # Build a commit that snapshots fox/proj/artifacts.
        art = self.repo / "fox" / "proj" / "artifacts" / "fig1.png"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_bytes(b"PNGDATA")
        self._write_run_and_commit()
        proj_dir = self.tmp / "proj"
        proj_dir.mkdir(exist_ok=True)

        class _Rt:
            name = "proj"
            dir = proj_dir
            store = ProjectStore(proj_dir)

        rt = _Rt()
        rid = rt.store.add_run("p", "r", "done", 1.0, 2.0,
                               metrics={"acc": 0.5}, config={"eps": 1})
        rt.store.set_run_git_commit(rid, "HEAD")
        # Point the module at our tmp repo.
        import backend.experiment_repo as er
        orig = er.management_repo_dir
        er.management_repo_dir = lambda: self.repo
        try:
            res = restore_run(rt, rid)
        finally:
            er.management_repo_dir = orig
        self.assertTrue(res["ok"], res.get("message"))
        self.assertIn("fig1.png", res["restored"])
        self.assertTrue((proj_dir / "artifacts" / "fig1.png").exists())
        # A 'restore' child run was forked.
        child = rt.store.get_run(res["run_id"])
        self.assertEqual(child["kind"], "restore")
        self.assertEqual(child["parent_run_id"], rid)


if __name__ == "__main__":
    unittest.main()
