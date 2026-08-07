"""Kernel status + live execution endpoints for the web app.

Exposes the per-project Python kernel's live status snapshot so the UI can
render a kernel pill / status panel (state, pid, uptime, current code, remote
mode) and stream output as code runs.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..state import get_runtime

router = APIRouter()

# Live WebSocket clients per project, fed by the kernel's event subscribers.
_kernel_clients: dict[str, set[WebSocket]] = {}
_kernel_lock = asyncio.Lock()


def _fanout(name: str, event: str, payload: dict):
    """Dispatch a kernel event to all live WS clients of a project."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_do_fanout(name, event, payload))
    except RuntimeError:
        pass


async def _do_fanout(name: str, event: str, payload: dict):
    async with _kernel_lock:
        clients = list(_kernel_clients.get(name, set()))
    dead = []
    for ws in clients:
        try:
            await ws.send_json({"type": event, "payload": payload})
        except Exception:  # noqa: BLE001
            dead.append(ws)
    if dead:
        async with _kernel_lock:
            _kernel_clients.setdefault(name, set()).difference_update(dead)


def attach_kernel_events(runtime, name: str):
    """Subscribe the project's Python kernel to the WS fan-out for `name`."""
    try:
        return runtime.kernels.python.subscribe(
            lambda ev, pl: _fanout(name, ev, pl))
    except Exception:  # noqa: BLE001
        return None


@router.get("/api/projects/{name}/kernel/status")
async def kernel_status(name: str):
    """Live snapshot of the project's Python kernel (state, pid, uptime, …)."""
    rt = get_runtime(name)
    st = rt.kernels.python.status()
    st["remote"] = bool(getattr(rt.kernels.python, "remote", False))
    st["remote_url"] = getattr(rt.kernels.python, "url", None)
    return st


@router.post("/api/projects/{name}/kernel/execute")
async def kernel_execute(name: str, body: dict):
    """Run a snippet directly in the project kernel (Kernel view)."""
    code = (body.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "code required", "output": ""}
    rt = get_runtime(name)
    timeout = float(body.get("timeout", 30))
    return await rt.kernels.python.run_code(code, timeout=timeout, stream=True)


@router.websocket("/ws/projects/{name}/kernel")
async def kernel_ws(ws: WebSocket, name: str):
    """Stream the project kernel's live status/output events to the UI."""
    await ws.accept()
    async with _kernel_lock:
        _kernel_clients.setdefault(name, set()).add(ws)
    rt = get_runtime(name)
    unsub = attach_kernel_events(rt, name)
    try:
        # Push the current snapshot immediately, then forward live events.
        await ws.send_json({"type": "status",
                            "payload": rt.kernels.python.status()})
        while True:
            await ws.receive_text()  # keep-alive / ping-pong protocol
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        if unsub:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        async with _kernel_lock:
            _kernel_clients.setdefault(name, set()).discard(ws)
