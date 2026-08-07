"""Remote kernel client: drives a real headless kernel server over HTTP + WS."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.kernels.manager import make_kernel_manager

from tests.test_kernel_server import UvicornServer
from backend.kernels.server import create_app


class TestRemoteKernel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.srv = UvicornServer(create_app(cwd=self.tmp))
        self.srv.__enter__()
        self.addAsyncCleanup(self._stop_srv)
        self.manager = make_kernel_manager(self.tmp,
                                           remote_url=self.srv.base_url)
        self.addAsyncCleanup(self.manager.stop)

    async def _stop_srv(self):
        await asyncio.to_thread(self.srv.__exit__, None, None, None)

    async def test_remote_execute(self):
        resp = await self.manager.python.run_code("x = 6 * 7\nprint(x)")
        self.assertTrue(resp.get("ok"), resp)
        self.assertIn("42", resp.get("output", ""))

    async def test_remote_state_persists(self):
        await self.manager.python.run_code("counter = 41")
        resp = await self.manager.python.run_code("counter += 1\nprint(counter)")
        self.assertIn("42", resp.get("output", ""))

    async def test_remote_error(self):
        resp = await self.manager.python.run_code("1 / 0")
        self.assertFalse(resp.get("ok"))
        self.assertIn("ZeroDivisionError", resp.get("error", ""))

    async def test_remote_variables(self):
        await self.manager.python.run_code("foo = [1, 2, 3]")
        vars_ = await self.manager.python.list_variables()
        self.assertIn("foo", vars_)

    async def test_remote_reset(self):
        await self.manager.python.run_code("marker = 1")
        await self.manager.python.reset()
        resp = await self.manager.python.run_code("print(marker)")
        self.assertFalse(resp.get("ok"))  # NameError after reset

    async def test_remote_env(self):
        env = await self.manager.get_env()
        self.assertIn("python", env)
        self.assertIn("r", env)

    async def test_remote_streaming_events(self):
        events = []

        def on_event(event: str, payload: dict):
            events.append((event, payload))

        self.manager.python.subscribe(on_event)
        resp = await self.manager.python.run_code("print('hi')", stream=True)
        self.assertTrue(resp.get("ok"), resp)
        outputs = [p.get("text") for e, p in events if e == "output"]
        self.assertTrue(any("hi" in (t or "") for t in outputs), events)

    async def test_remote_status(self):
        st = self.manager.python.status()
        self.assertIn("state", st)
        self.assertTrue(st.get("remote") or st.get("url"))


if __name__ == "__main__":
    unittest.main()
