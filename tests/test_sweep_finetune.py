"""Round-29: parameter sweep launch (grid expansion) + finetune launch flow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.finetune import (finetune_script, finetune_summary,
                              normalize_finetune_config, validate_finetune)
from backend.sweep import (expand_sweep_grid, suggest_grid_from_config,
                           validate_sweep_request)


class TestSweepGrid(unittest.TestCase):
    def test_cartesian_expansion(self):
        out = expand_sweep_grid({"lr": [1e-3, 1e-4], "bs": [8, 16]})
        self.assertEqual(len(out), 4)
        self.assertIn({"lr": 1e-3, "bs": 8}, out)
        self.assertIn({"lr": 1e-4, "bs": 16}, out)

    def test_explicit_configs_win(self):
        out = expand_sweep_grid({"lr": [1e-3, 1e-4]},
                                configs=[{"a": 1}, {"a": 2}])
        self.assertEqual(out, [{"a": 1}, {"a": 2}])

    def test_empty_grid_falls_back_to_single_config(self):
        self.assertEqual(expand_sweep_grid({}), [{}])
        self.assertEqual(expand_sweep_grid({"lr": []}), [{}])

    def test_suggest_grid_from_config(self):
        g = suggest_grid_from_config({"lr": 0.1, "seed": 42, "n_estimators": 100})
        self.assertIn("lr", g)
        self.assertNotIn("seed", g)  # seed is excluded
        self.assertEqual(len(g["lr"]), 3)
        self.assertIn(0.1, g["lr"])

    def test_validate_sweep_request(self):
        self.assertTrue(validate_sweep_request("", [{}]))
        self.assertTrue(validate_sweep_request("report_metric('a', 1)", []))
        self.assertEqual(validate_sweep_request(
            "config = {}; report_metric('a', config['x'])", [{"x": 1}]), "")


class TestFinetuneConfig(unittest.TestCase):
    def test_defaults_merge(self):
        cfg = normalize_finetune_config({"base_model": "x", "dataset": "d"})
        self.assertEqual(cfg["epochs"], 3)
        self.assertEqual(cfg["lora_r"], 0)
        self.assertEqual(cfg["base_model"], "x")

    def test_validation(self):
        self.assertEqual(
            validate_finetune(normalize_finetune_config({})),
            "A base model is required (e.g. distilbert-base-uncased).")
        cfg = normalize_finetune_config({"base_model": "b"})
        self.assertEqual(
            validate_finetune(cfg),
            "A training dataset file is required (a path readable by pandas).")
        cfg2 = normalize_finetune_config({"base_model": "b", "dataset": "d",
                                          "epochs": 0})
        self.assertEqual(validate_finetune(cfg2), "epochs must be >= 1.")

    def test_script_generation(self):
        cfg = normalize_finetune_config({
            "base_model": "distilbert-base-uncased", "dataset": "data/train.csv",
            "epochs": 2, "lora_r": 4,
        })
        script = finetune_script(cfg)
        self.assertIn("distilbert-base-uncased", script)
        self.assertIn("report_metric", script)
        self.assertIn("lora_cfg", script)  # LoRA block present when lora_r > 0
        self.assertIn("report_dataset", script)

    def test_script_no_lora(self):
        cfg = normalize_finetune_config({"base_model": "b", "dataset": "d"})
        script = finetune_script(cfg)
        self.assertNotIn("lora_cfg", script)

    def test_summary(self):
        cfg = normalize_finetune_config({
            "base_model": "b", "dataset": "d", "epochs": 5, "lora_r": 0})
        s = finetune_summary(cfg)
        self.assertIn("Finetune launch", s)
        self.assertIn("full finetune", s)


class TestFinetuneTool(unittest.IsolatedAsyncioTestCase):
    """The run_finetune tool records a kind=finetune run with the generated
    training script + config, mirroring how the UI launches one."""

    def setUp(self):
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.permissions import PermissionManager
        from backend.store import ProjectStore
        from tests.test_round3 import PoolKernels

        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("ft", "", "acc", 0.9, True)
        self.base = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                       metrics={"acc": 0.5},
                                       experiment_id=self.eid)
        self.ctx = ToolContext(kernels=PoolKernels(),
                               artifacts=ArtifactStore(self.tmp),
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.ctx.experiment_id = str(self.eid)
        self.ctx.parent_run_id = self.base

    async def test_finetune_tool_records_run(self):
        from backend.agents.tools import _run_finetune
        out = await _run_finetune(
            self.ctx, base_model="distilbert-base-uncased", dataset="data/train.csv",
            epochs=2, learning_rate=3e-5, batch_size=16, lora_r=4)
        self.assertIn("Finetune launch", out)
        runs = self.store.experiment_runs(self.eid)
        ft = [r for r in runs if r["kind"] == "finetune"]
        self.assertEqual(len(ft), 1)
        self.assertEqual(ft[0]["config"]["base_model"], "distilbert-base-uncased")
        self.assertEqual(ft[0]["config"]["lora_r"], 4)
        self.assertEqual(ft[0]["dataset"], "data/train.csv")
        full = self.store.get_run(ft[0]["id"], include_code=True)
        self.assertIn("lora_cfg", full["code"][0]["code"])

    async def test_finetune_tool_validates(self):
        from backend.agents.tools import _run_finetune
        out = await _run_finetune(self.ctx, base_model="", dataset="d")
        self.assertTrue(out.startswith("[error]"))
        self.assertEqual(
            [r for r in self.store.experiment_runs(self.eid) if r["kind"] == "finetune"],
            [])


class _StubRuntime:
    def __init__(self, store):
        self.store = store

    def list_messages(self, limit=None):
        return self.store.list_messages(limit)


class TestLaunchJobIntent(unittest.IsolatedAsyncioTestCase):
    """The UI-launch intent helper records a user message + an assistant reply
    and runs the sweep/finetune tool against the resolved experiment."""

    def setUp(self):
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.permissions import PermissionManager
        from backend.store import ProjectStore
        from tests.test_round3 import PoolKernels

        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("sweep exp", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0, metrics={"acc": 0.5},
                           experiment_id=self.eid)
        self.ctx = ToolContext(kernels=PoolKernels(),
                               artifacts=ArtifactStore(self.tmp),
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.events = []
        self.rt = _StubRuntime(self.store)

    async def _coord(self):
        from backend.agents.tools import build_tools
        coord = type("Coord", (), {})()
        coord.ctx = self.ctx
        coord.tools = build_tools(self.ctx)
        return coord

    async def _emit(self, event, payload=None):
        self.events.append((event, payload or {}))

    async def test_sweep_intent_launches(self):
        from backend.main import _launch_experiment_job
        coord = await self._coord()
        await _launch_experiment_job(
            self.rt, coord, self._emit, "", "run_sweep", str(self.eid),
            {"sweep": {"code": "report_metric('acc', config['eps']/10)",
                       "grid": {"eps": [1, 2]}, "label_prefix": "eps"}},
            ["parameter sweep"])
        kinds = {e for e, _ in self.events}
        self.assertIn("user_message", kinds)
        self.assertIn("assistant_message", kinds)
        self.assertIn("done", kinds)
        runs = self.store.experiment_runs(self.eid)
        sweep = [r for r in runs if r["kind"] == "sweep"]
        self.assertEqual(len(sweep), 2)
        self.assertEqual(sorted(r["config"]["eps"] for r in sweep), [1, 2])

    async def test_finetune_intent_launches(self):
        from backend.main import _launch_experiment_job
        coord = await self._coord()
        await _launch_experiment_job(
            self.rt, coord, self._emit, "", "finetune", str(self.eid),
            {"finetune": {"base_model": "b", "dataset": "d", "epochs": 2,
                          "learning_rate": 2e-5, "batch_size": 8, "lora_r": 0}},
            ["finetune"])
        kinds = {e for e, _ in self.events}
        self.assertIn("assistant_message", kinds)
        runs = self.store.experiment_runs(self.eid)
        ft = [r for r in runs if r["kind"] == "finetune"]
        self.assertEqual(len(ft), 1)
        self.assertEqual(ft[0]["config"]["base_model"], "b")

    async def test_intent_missing_experiment_errors(self):
        from backend.main import _launch_experiment_job
        coord = await self._coord()
        from backend.store import ProjectStore
        empty = _StubRuntime(ProjectStore(Path(tempfile.mkdtemp())))
        await _launch_experiment_job(
            empty, coord, self._emit, "", "run_sweep", "99999",
            {"sweep": {"code": "x", "configs": [{}]}}, ["parameter sweep"])
        kinds = {e for e, _ in self.events}
        self.assertIn("error", kinds)


if __name__ == "__main__":
    unittest.main()
