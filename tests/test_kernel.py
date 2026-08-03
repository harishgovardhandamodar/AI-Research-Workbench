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


if __name__ == "__main__":
    unittest.main()
