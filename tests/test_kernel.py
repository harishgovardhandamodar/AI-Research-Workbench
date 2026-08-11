"""Kernel protocol round-trip: the persistent Python subprocess worker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.kernels.python_kernel import PythonKernel


class TestKernelProtocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.kernel = PythonKernel(cwd=self.tmp)
        await self.kernel._start()
        self.addAsyncCleanup(self.kernel.stop)

    async def test_run_code_roundtrip(self):
        resp = await self.kernel.run_code("x = 40\nprint(x + 2)")
        self.assertTrue(resp.get("ok"), resp)
        self.assertIn("42", resp.get("output", ""))

    async def test_state_persists_across_calls(self):
        await self.kernel.run_code("counter = 1")
        resp = await self.kernel.run_code("counter += 1\nprint(counter)")
        self.assertIn("2", resp.get("output", ""))

    async def test_error_reported(self):
        resp = await self.kernel.run_code("raise ValueError('boom')")
        self.assertFalse(resp.get("ok"))
        self.assertIn("boom", resp.get("error", ""))

    async def test_variables_roundtrip(self):
        await self.kernel.run_code("foo = [1, 2, 3]")
        resp = await self.kernel.list_variables()
        self.assertIn("foo", resp)

    async def test_reset_clears_state(self):
        await self.kernel.run_code("secret = 1")
        await self.kernel.reset()
        resp = await self.kernel.run_code("print(secret)")
        self.assertFalse(resp.get("ok"))  # name error after reset

    async def test_report_metric_structured(self):
        resp = await self.kernel.run_code(
            "report_metric('acc', 0.91)\nreport_metric('rmse', 1.25)")
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp.get("metrics"), {"acc": 0.91, "rmse": 1.25})

    async def test_report_metric_step_key(self):
        resp = await self.kernel.run_code("report_metric('loss', 0.5, step=3)")
        self.assertEqual(resp.get("metrics"), {"loss[step=3]": 0.5})

    async def test_report_metric_invalid_value_rejected(self):
        resp = await self.kernel.run_code("report_metric('acc', 'high')")
        self.assertFalse(resp.get("ok"))
        self.assertIn("must be a number", resp.get("error", ""))

    async def test_metrics_cleared_after_call(self):
        await self.kernel.run_code("report_metric('acc', 0.5)")
        resp = await self.kernel.run_code("x = 1")
        self.assertEqual(resp.get("metrics"), {})

    async def test_restart_count_surfaces(self):
        await self.kernel.run_code("x = 1")
        self.assertEqual(self.kernel.restarts, 0)
        # Kill the subprocess; the next call auto-restarts and counts it.
        self.kernel._proc.kill()
        await self.kernel._proc.wait()
        resp = await self.kernel.run_code("print('fresh')")
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(self.kernel.restarts, 1)
        self.assertIn("restarts", self.kernel.status())
        self.assertEqual(self.kernel.status()["restarts"], 1)


if __name__ == "__main__":
    unittest.main()
