"""End-to-end core tests: store, data pipeline, trainer, eval, controller."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.pipeline import DataPipeline
from eval.harness import EvalHarness
from experiment.controller import ExperimentController
from experiment.store import ExperimentStore


class TestCoreFlow(unittest.IsolatedAsyncioTestCase):
    async def _exp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctl = ExperimentController(tmp.name)
        exp = ctl.create("demo", "Qwen/Qwen2.5-0.5B-Instruct",
                         paper={"reported_metrics": {"accuracy": 0.78}}, seed=0)
        return ctl, tmp.name, exp

    async def test_full_incremental_loop(self):
        ctl, root, exp = await self._exp()

        # Data
        dp = DataPipeline(Path(root) / "data")
        meta = dp.generate("data1", n_trajectories=30, n_teacher=30, n_rubric=10, seed=0)
        self.assertEqual(meta["records"], 70)
        dp.add_incremental("data1", [{"kind": "trajectory", "messages": [],
                                      "tool_calls": [], "expected": "x"}])
        splits = dp.split("data1", seed=0)
        self.assertEqual(splits["data1_train"]["records"], 56)
        self.assertTrue(dp.validate("data1_train")["valid"])
        self.assertRaises(Exception, dp.validate, "nope")

        # Stage 0 + train (simulate). init_experiment already created stage_0.
        ctl.store.update_stage("stage_0", data_hashes=[splits["data1_train"]["sha256"]])
        r0 = await ctl.trainer.start_stage("stage_0", new_data="data1_train",
                                           epochs=1, lr=5e-5, lora_rank=8)
        self.assertEqual(r0["adapter"]["metrics"]["mode"], "simulate")
        self.assertEqual(ctl.store.get_stage("stage_0")["status"], "done")
        self.assertTrue(ctl.trainer.list_checkpoints())

        # Eval + paper compare
        ev = ctl.harness.run("stage_0", subset=50)
        self.assertIn("accuracy", ev["metrics"])
        cmp = ctl.harness.compare_to_paper("stage_0")
        self.assertTrue(cmp["table"])

        # Stage 1 incremental from stage_0 adapter
        ctl.create_stage("stage_1", parent="stage_0")
        r1 = await ctl.trainer.start_stage("stage_1", from_adapter="stage_0-adapter",
                                           lora_rank=16)
        self.assertEqual(r1["adapter"]["from_adapter"], "stage_0-adapter")
        self.assertEqual(r1["adapter"]["hyperparams"]["lora_rank"], 16)

        # Hyperparams + config
        hp = ctl.trainer.set_hyperparams({"learning_rate": 2e-5, "method": "lora"})
        self.assertEqual(hp["learning_rate"], 2e-5)
        self.assertRaises(ValueError, ctl.trainer.set_hyperparams, {"bogus": 1})

        # Report + rollback
        rep = ctl.export_report("stage_1")
        self.assertTrue(rep["report_path"].endswith(".md"))
        removed = ctl.rollback_to_stage("stage_0")
        self.assertEqual(removed["removed_stages"], ["stage_1"])
        self.assertEqual(ctl.list_stages()["current"], "stage_0")

    async def test_resume(self):
        ctl, _, _ = await self._exp()
        ctl.create_stage("s0")
        await ctl.trainer.start_stage("s0")
        resumed = await ctl.trainer.resume("s0", steps=5)
        self.assertEqual(resumed["adapter"]["metrics"]["steps_total"], 45)
        self.assertEqual(ctl.trainer.get_status("s0")["status"], "done")


if __name__ == "__main__":
    unittest.main()
