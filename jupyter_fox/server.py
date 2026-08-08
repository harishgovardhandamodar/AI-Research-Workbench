"""Jupyter server extension: embed the Fox workbench at /fox via a sidecar.

The workbench FastAPI app (uvicorn) runs as a subprocess on an ephemeral localhost
port. Tornado handlers on the Jupyter server proxy HTTP and WebSocket traffic to
it, so from the user's perspective the whole workbench lives under ``/fox`` inside
the Jupyter server (single origin, shared authentication).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

from tornado import httpclient, websocket, web

log = logging.getLogger("jupyter_fox")

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFIX = "/fox"


# --------------------------------------------------------------- sidecar -----
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class WorkbenchSidecar:
    def __init__(self):
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [
            sys.executable, "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning",
        ]
        self.proc = subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("Fox workbench sidecar started on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------ proxy -----
class FoxHTTPProxy(web.RequestHandler):
    def initialize(self, port: int):
        self._port = port

    async def _proxy(self):
        suffix = (self.path_args or [""])[0]
        path = "/" + suffix if suffix else "/"
        query = self.request.query
        target = f"http://127.0.0.1:{self._port}{path}"
        if query:
            target += "?" + query
        client = httpclient.AsyncHTTPClient()
        headers = {k: v for k, v in self.request.headers.items()
                   if k.lower() not in ("host", "connection", "content-length", "upgrade")}
        body = None if self.request.method in ("GET", "HEAD", "OPTIONS") \
            else (self.request.body or None)
        try:
            resp = await client.fetch(target, method=self.request.method,
                                      headers=headers, body=body,
                                      raise_error=False, follow_redirects=False,
                                      allow_nonstandard_methods=True)
        except httpclient.HTTPError as e:
            self.set_status(502)
            self.write(f"fox proxy error: {e}")
            return
        self.set_status(resp.code)
        for k, v in resp.headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection",
                             "upgrade", "keep-alive"):
                continue
            self.set_header(k, v)
        self.write(resp.body)

    async def get(self, *a): await self._proxy()
    async def post(self, *a): await self._proxy()
    async def put(self, *a): await self._proxy()
    async def delete(self, *a): await self._proxy()
    async def patch(self, *a): await self._proxy()


class FoxWebSocketProxy(websocket.WebSocketHandler):
    def initialize(self, port: int):
        self._port = port
        self._client: websocket.WebSocketClientConnection | None = None
        self._pump_task: asyncio.Task | None = None

    async def open(self, *args):
        suffix = (args or [""])[0]
        target = f"ws://127.0.0.1:{self._port}/ws/{suffix}"
        if self.request.query:
            target += "?" + self.request.query
        try:
            self._client = await websocket.websocket_connect(target)
        except Exception as e:  # noqa: BLE001
            log.warning("fox ws connect failed: %s", e)
            self.close()
            return
        self._pump_task = asyncio.ensure_future(self._pump())

    async def _pump(self):
        try:
            while True:
                msg = await self._client.read_message()
                if msg is None:
                    break
                if isinstance(msg, bytes):
                    self.write_message(msg, binary=True)
                else:
                    self.write_message(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.close()

    def on_message(self, message):
        if self._client is None:
            return
        try:
            if isinstance(message, bytes):
                self._client.write_message(message, binary=True)
            else:
                self._client.write_message(message)
        except Exception:  # noqa: BLE001
            pass

    def on_close(self):
        if self._pump_task:
            self._pump_task.cancel()
        if self._client:
            self._client.close()


# --------------------------------------------------------------- extension ----
def _load_jupyter_server_extension(server_app) -> None:
    try:
        import jupyter_server  # noqa: F401
    except ImportError:
        server_app.log.warning("jupyter_server not found; Fox extension disabled")
        return

    sidecar = WorkbenchSidecar()
    sidecar.start()

    host_pattern = r".*$"
    server_app.web_app.add_handlers(host_pattern, [
        (PREFIX + r"/ws/(.*)", FoxWebSocketProxy, {"port": sidecar.port}),
        (PREFIX + r"/(.*)", FoxHTTPProxy, {"port": sidecar.port}),
    ])
    server_app.web_app.add_handlers(host_pattern, [
        (PREFIX, FoxHTTPProxy, {"port": sidecar.port}),
    ])

    import atexit

    def _stop():
        sidecar.stop()
    atexit.register(_stop)

    server_app.log.info(
        "Local - Open - Agentic Experimentation Workbench mounted at %s (sidecar on 127.0.0.1:%s)",
        PREFIX, sidecar.port)


def _jupyter_server_extension_points() -> list[dict]:
    return [{"module": "jupyter_fox"}]
