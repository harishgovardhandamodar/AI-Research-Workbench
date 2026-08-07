"""Headless kernel server: REST + WebSocket execution against a real uvicorn
instance (the production path), plus the remote client over HTTP/WS."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
import uvicorn

from backend.kernels.server import create_app


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornServer:
    """Run a FastAPI app with uvicorn in a background thread."""

    def __init__(self, app, host="127.0.0.1", port=0):
        self.app = app
        self.host = host
        self.port = port or free_port()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        config = uvicorn.Config(self.app, host=self.host, port=self.port,
                                log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = httpx.get(f"http://{self.host}:{self.port}/health",
                              timeout=1.0)
                if r.status_code == 200:
                    return self
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
        raise RuntimeError("uvicorn server did not start in time")

    def __exit__(self, *exc):
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)
        return False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TestHeadlessServer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = create_app(cwd=self.tmp)
        self.srv = UvicornServer(self.app)
        self.srv.__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.http = httpx.Client(base_url=self.srv.base_url, timeout=60.0)

    def tearDown(self):
        self.http.close()

    def test_health(self):
        r = self.http.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["kernel"]["state"], "idle")

    def test_status_snapshot(self):
        r = self.http.get("/api/kernel/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("state", body)
        self.assertIn("pid", body)
        self.assertIsNotNone(body["pid"])

    def test_execute(self):
        r = self.http.post("/api/kernel/execute",
                           json={"code": "x = 6 * 7\nprint(x)"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertIn("42", body.get("output", ""))

    def test_state_persists_across_requests(self):
        self.http.post("/api/kernel/execute", json={"code": "counter = 41"})
        r = self.http.post("/api/kernel/execute",
                           json={"code": "counter += 1\nprint(counter)"})
        self.assertIn("42", r.json().get("output", ""))

    def test_execute_empty_code(self):
        r = self.http.post("/api/kernel/execute", json={"code": "   "})
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("empty", body.get("error", ""))

    def test_execute_error(self):
        r = self.http.post("/api/kernel/execute", json={"code": "1 / 0"})
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("ZeroDivisionError", body.get("error", ""))

    def test_variables(self):
        self.http.post("/api/kernel/execute", json={"code": "ans = 42"})
        r = self.http.get("/api/kernel/variables")
        self.assertIn("ans", r.json()["variables"])

    def test_reset(self):
        self.http.post("/api/kernel/execute", json={"code": "marker = 1"})
        r = self.http.post("/api/kernel/reset")
        self.assertTrue(r.json()["ok"])
        r2 = self.http.post("/api/kernel/execute", json={"code": "print(marker)"})
        self.assertFalse(r2.json()["ok"])  # NameError after reset

    def test_env(self):
        r = self.http.get("/api/kernel/env")
        self.assertEqual(r.status_code, 200)
        env = r.json()["env"]
        self.assertIn("python", env)

    def test_streaming_events_via_websocket(self):
        import websockets

        url = self.srv.base_url.replace("http://", "ws://") + "/ws/kernel"
        with self.http.stream("GET", "/health") as _:  # keep client warm
            pass

        async def run():
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(
                    {"type": "execute", "code": "print('a'); print('b')",
                     "stream": True}))
                got_result = False
                for _ in range(20):
                    msg = json.loads(await ws.recv())
                    if msg.get("type") == "result":
                        got_result = True
                        self.assertTrue(msg["payload"]["ok"], msg["payload"])
                        break
                self.assertTrue(got_result)
                return

        import asyncio

        asyncio.run(run())

    def test_status_pushed_on_connect(self):
        import asyncio
        import websockets

        url = self.srv.base_url.replace("http://", "ws://") + "/ws/kernel"

        async def run():
            async with websockets.connect(url) as ws:
                msg = json.loads(await ws.recv())
                self.assertEqual(msg["type"], "status")
                self.assertEqual(msg["payload"]["state"], "idle")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
