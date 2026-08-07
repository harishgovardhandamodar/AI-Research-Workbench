"""Headless kernel server: run the execution kernel as a standalone app.

Lets the persistent Python (+ optional R) kernel live in its own process,
exposing a REST + WebSocket API so any client — the Fox workbench, a remote
frontend, a notebook, or a CLI — can execute code and watch the live status of
execution (idle/busy, running code, streamed stdout, variables, uptime).

Run it:

    python -m backend.kernels.server --port 8891
    fox-kernel --port 8891 --cwd /path/to/workspace

Endpoints
---------
REST:
    GET    /health                          -> {"ok": true, "kernel": ...}
    GET    /api/kernel/status               -> live kernel status snapshot
    GET    /api/kernel/variables            -> {name: description}
    GET    /api/kernel/env                  -> environment/package versions
    POST   /api/kernel/execute              -> {code, timeout?, stream?} -> result
    POST   /api/kernel/reset                -> clear kernel state
WebSocket:
    WS     /ws/kernel                       -> streaming status + output events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .manager import KernelManager
from .python_kernel import KernelError


class ExecuteBody(BaseModel):
    code: str
    timeout: float | None = 30.0
    stream: bool | None = False


class KernelServer:
    """Owns one KernelManager and fans out its status events to WebSocket clients."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.kernels = KernelManager(self.cwd)
        self._clients: set[WebSocket] = set()
        self._unsub_py: callable | None = None

    async def start(self):
        self._unsub_py = self.kernels.python.subscribe(self._on_event)
        await self.kernels.get_env()

    async def stop(self):
        if self._unsub_py:
            self._unsub_py()
        await self.kernels.stop()
        self._clients.clear()

    def _on_event(self, event: str, payload: dict):
        asyncio.create_task(self._broadcast({"type": event, "payload": payload}))

    async def _broadcast(self, msg: dict):
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                self._clients.discard(ws)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)
        await ws.send_json({"type": "status", "payload": self.kernels.python.status()})

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def execute(self, code: str, timeout: float = 30.0,
                      stream: bool = False) -> dict:
        if not (code or "").strip():
            return {"ok": False, "error": "empty code", "output": ""}
        try:
            return await self.kernels.python.run_code(code, timeout=timeout,
                                                      stream=bool(stream))
        except KernelError as e:
            return {"ok": False, "error": str(e), "output": ""}


# ---------------------------------------------------------------- app -------

def create_app(cwd: Path | None = None) -> FastAPI:
    server = KernelServer(cwd)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await server.start()
        app.state.kernel_server = server
        yield
        await server.stop()

    app = FastAPI(title="Fox headless kernel server", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"ok": True, "kernel": server.kernels.python.status()}

    @app.get("/api/kernel/status")
    async def status():
        return server.kernels.python.status()

    @app.get("/api/kernel/variables")
    async def variables():
        return {"variables": await server.kernels.python.list_variables()}

    @app.get("/api/kernel/env")
    async def env():
        return {"env": await server.kernels.get_env()}

    @app.post("/api/kernel/execute")
    async def execute(body: ExecuteBody):
        return await server.execute(body.code, timeout=body.timeout or 30.0,
                                    stream=bool(body.stream))

    @app.post("/api/kernel/reset")
    async def reset():
        resp = await server.kernels.python.reset()
        return {"ok": bool(resp.get("ok"))}

    @app.websocket("/ws/kernel")
    async def kernel_ws(ws: WebSocket):
        await server.connect(ws)
        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "execute":
                    res = await server.execute(
                        msg.get("code", ""),
                        timeout=float(msg.get("timeout", 30) or 30),
                        stream=bool(msg.get("stream", False)))
                    try:
                        await ws.send_json({"type": "result", "payload": res})
                    except Exception:  # noqa: BLE001
                        pass
                elif mtype == "reset":
                    await server.kernels.python.reset()
                elif mtype == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            server.disconnect(ws)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fox-kernel",
        description="Run the Fox execution kernel as a headless server app.")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=8891, help="bind port")
    parser.add_argument("--cwd", default=None, help="kernel working directory")
    args = parser.parse_args(argv)

    import uvicorn

    cwd = Path(args.cwd).resolve() if args.cwd else None
    app = create_app(cwd)
    print(f"Fox headless kernel server on http://{args.host}:{args.port} "
          f"(cwd={cwd or Path.cwd()})", file=sys.stderr, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
