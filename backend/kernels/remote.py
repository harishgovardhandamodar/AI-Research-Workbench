"""Remote kernel client: drive a headless kernel server over HTTP + WebSocket.

A drop-in async proxy for the local :class:`PythonKernel`: instead of spawning a
subprocess it sends requests to a ``fox-kernel`` server (see
``backend/kernels/server.py``) and subscribes to its status/output events, so
the workbench reflects the live status of a kernel running on a remote host.

    kernels = RemoteKernelManager(url="http://localhost:8891", session_dir=...)
    await kernels.python.run_code("print('hi')", stream=True)
    await kernels.python.status()
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .r_kernel import RKernel


class KernelClientError(RuntimeError):
    pass


class RemotePythonKernel:
    """Async HTTP+WS client for a headless kernel server's Python kernel."""

    def __init__(self, url: str, cwd: Path | None = None):
        self.url = url.rstrip("/")
        self.cwd = cwd or Path.cwd()
        self._ws: Any | None = None
        self._listeners: list = []
        self._ws_task: asyncio.Task | None = None
        self._last_status: dict = {
            "state": "unknown", "remote": True, "url": self.url,
            "pid": None, "cwd": str(self.cwd),
            "current_code": "", "last_ok": None, "last_error": None,
            "last_duration_ms": 0.0, "exec_count": 0, "uptime": 0.0,
        }

    # -- HTTP helpers --------------------------------------------------------
    async def _http(self, method: str, path: str,
                    body: dict | None = None) -> dict:
        import httpx

        async with httpx.AsyncClient() as client:
            if method == "POST":
                resp = await client.post(self.url + path, json=body or {},
                                         timeout=120.0)
            else:
                resp = await client.get(self.url + path, timeout=30.0)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            raise KernelClientError(data.get("detail") or data.get("error")
                                    or f"HTTP {resp.status_code} from {path}")
        return data

    # -- events --------------------------------------------------------------
    def subscribe(self, listener) -> callable:
        self._listeners.append(listener)
        return (lambda: self._listeners.remove(listener)
                if listener in self._listeners else None)

    def _notify(self, event: str, payload: dict):
        for ln in list(self._listeners):
            try:
                ln(event, payload)
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_ws(self):
        if self._ws is not None:
            return
        import websockets

        proto = "wss" if self.url.startswith("https") else "ws"
        path = self.url.replace("http://", "").replace("https://", "").split("/", 1)
        hostport = path[0]
        url = f"{proto}://{hostport}/ws/kernel"
        self._ws = await websockets.connect(url, max_size=None)
        self._ws_task = asyncio.create_task(self._ws_read_loop())

    async def _ws_read_loop(self):
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                payload = msg.get("payload") or {}
                if mtype == "status":
                    payload = dict(payload, remote=True, url=self.url)
                    self._last_status = payload
                elif mtype == "idle":
                    self._last_status.update({
                        "state": "idle", "current_code": "",
                        "last_ok": payload.get("last_ok"),
                        "last_duration_ms": payload.get("duration_ms"),
                    })
                self._notify(mtype, payload)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            self._ws = None

    # -- public API (mirrors PythonKernel) -----------------------------------
    async def run_code(self, code: str, timeout: float = 30.0,
                       stream: bool = False) -> dict:
        try:
            await self._ensure_ws()
        except Exception:  # noqa: BLE001
            self._ws = None
        # If we have a live WS we stream through it; the server pushes the
        # authoritative busy/output/idle events. Otherwise fall back to REST.
        if self._ws is not None:
            await self._ws.send(json.dumps({
                "type": "execute", "code": code,
                "timeout": timeout, "stream": bool(stream)}))
            return await asyncio.wait_for(self._next_result(), timeout + 5)
        self._last_status.update({"state": "busy", "current_code": code})
        self._notify("busy", {"code": code})
        try:
            return await self._http("POST", "/api/kernel/execute",
                                    {"code": code, "timeout": timeout,
                                     "stream": bool(stream)})
        finally:
            self._last_status.update({"state": "idle", "current_code": ""})
            self._notify("idle", {})

    async def _next_result(self) -> dict:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        prev = list(self._listeners)

        def on_msg(event: str, payload: dict):
            if event == "result" and not fut.done():
                fut.set_result(payload)

        self._listeners.append(on_msg)
        try:
            return await fut
        finally:
            if on_msg in self._listeners:
                self._listeners.remove(on_msg)
            # restore original listeners (defensive; _ws_read_loop notified them)
            if prev and prev[-1] is not on_msg:
                self._listeners[:] = prev + [l for l in self._listeners
                                             if l not in prev]
            else:
                self._listeners[:] = prev

    async def list_variables(self) -> dict:
        data = await self._http("GET", "/api/kernel/variables")
        return data.get("variables", {})

    async def get_env(self) -> dict:
        data = await self._http("GET", "/api/kernel/env")
        return data.get("env", {})

    async def reset(self) -> dict:
        return await self._http("POST", "/api/kernel/reset")

    def status(self) -> dict:
        return self._last_status

    async def stop(self):
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None


class RemoteKernelManager:
    """Drop-in for :class:`KernelManager` backed by a remote headless server.

    `python` is a RemotePythonKernel; R stays local (each R call already spawns
    a fresh Rscript, so it needs no server).
    """

    def __init__(self, url: str, session_dir: Path):
        self.url = url
        self.session_dir = session_dir
        self.workspace_dir = Path(session_dir)
        self.python = RemotePythonKernel(url, cwd=self.workspace_dir)
        self.r = RKernel(cwd=self.workspace_dir)
        self._env_cache: dict | None = None

    async def get_env(self) -> dict:
        if self._env_cache is None:
            env = await self.python.get_env()
            env.update(await self.r.get_env())
            self._env_cache = env
        return self._env_cache

    async def reset(self):
        self._env_cache = None
        await self.python.reset()
        await self.r.reset()

    async def stop(self):
        await self.python.stop()
        await self.r.stop()
