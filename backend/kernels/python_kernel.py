"""Manager for the persistent, sandboxed Python kernel.

Spawns `worker.py` as a subprocess and talks JSONL over stdin/stdout. Kernel state
(variables, loaded modules, matplotlib figures) persists across `run_code` calls
until `reset`. A fresh kernel is spawned per project session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

WORKER_PATH = Path(__file__).parent / "worker.py"


class KernelError(RuntimeError):
    pass


class PythonKernel:
    """Async wrapper around one kernel subprocess.

    Tracks lifecycle/execution status (idle/busy, uptime, current code) and
    broadcasts structured events to subscribers, so a headless kernel server
    or the web UI can reflect the live state of execution.
    """

    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd or Path.cwd()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._stderr_tail: str = ""
        # --- status / observability ---------------------------------------
        self._listeners: list = []
        self._state = "idle"          # idle | busy
        self._current_code: str = ""  # code of the execution in flight (if busy)
        self._started_at: float = 0.0
        self._last_start: float = 0.0
        self._last_duration_ms: float = 0.0
        self._last_ok: bool | None = None
        self._last_error: str | None = None
        self._exec_count = 0
        # How many times the subprocess died and was restarted mid-session
        # (kernel state loss is surfaced to the run record / logs).
        self.restarts = 0

    # -- status / events -----------------------------------------------------
    def subscribe(self, listener) -> callable:
        """Register a listener invoked as listener(event, payload).

        Events: "started" | "stopped" | "busy" | "idle" | "output" |
        "execution_done". Returns an unsubscribe callable.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self, event: str, payload: dict):
        for ln in list(self._listeners):
            try:
                ln(event, payload)
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict:
        """Snapshot of the kernel's live status for UIs / headless clients."""
        uptime = 0.0
        if self._started_at:
            uptime = asyncio.get_event_loop().time() - self._started_at \
                if asyncio.get_event_loop().is_running() else 0.0
        return {
            "state": self._state,
            "pid": self._proc.pid if self._proc and self._proc.returncode is None else None,
            "cwd": str(self.cwd),
            "uptime": round(uptime, 2),
            "current_code": self._current_code,
            "last_ok": self._last_ok,
            "last_error": self._last_error,
            "last_duration_ms": round(self._last_duration_ms, 2),
            "exec_count": self._exec_count,
            "restarts": self.restarts,
        }

    # -- lifecycle ----------------------------------------------------------
    async def _start(self):
        if self._proc and self._proc.returncode is None:
            return
        self._pending.clear()
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(WORKER_PATH),
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Default StreamReader limit is 64 KiB; base64 figure responses in
            # run_code results routinely exceed that, so allow large lines.
            limit=64 * 1024 * 1024,
        )
        self._started_at = asyncio.get_event_loop().time()
        self._state = "idle"
        self._notify("started", {"pid": self._proc.pid})
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        assert self._proc is not None
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        stderr_task = asyncio.create_task(self._read_stderr())
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("frame") == "output":
                    # Streaming stdout emitted while code runs.
                    self._notify("output", {"text": msg.get("text", ""),
                                            "id": msg.get("id")})
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        finally:
            stderr_task.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(KernelError(f"kernel process exited"
                                                  f"{self._err_suffix()}"))
            self._pending.clear()

    async def _read_stderr(self):
        assert self._proc is not None and self._proc.stderr is not None
        buf = []
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                buf.append(line.decode(errors="replace").rstrip())
                if len(buf) > 40:
                    buf.pop(0)
        except asyncio.CancelledError:
            pass
        finally:
            self._stderr_tail = "\n".join(buf[-25:])

    def _err_suffix(self) -> str:
        if self._stderr_tail:
            return f":\n{self._stderr_tail[:1500]}"
        return ""

    async def _send(self, req: dict, timeout: float = 60.0) -> dict:
        await self._lock.acquire()
        try:
            if self._proc is None or self._proc.returncode is not None:
                # The subprocess died (or never started). If it was alive before,
                # this restart destroys kernel state — surface it.
                if self._proc is not None:
                    self.restarts += 1
                    self._notify("reset", {"ok": False, "reason": "restarted",
                                           "restarts": self.restarts})
                await self._start()
            rid = uuid4().hex
            req["id"] = rid
            fut = asyncio.get_event_loop().create_future()
            self._pending[rid] = fut
            assert self._proc and self._proc.stdin
            self._proc.stdin.write((json.dumps(req) + "\n").encode())
            await self._proc.stdin.drain()
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                # Kill and restart; kernel state is lost on a hard timeout.
                self._pending.pop(rid, None)
                self._kill()
                raise KernelError("Kernel timed out and was restarted (state lost)")
        finally:
            self._lock.release()

    def _kill(self):
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        self._proc = None

    # -- public API ----------------------------------------------------------
    async def run_code(self, code: str, timeout: float = 30.0,
                       stream: bool = False) -> dict:
        """Execute code in the persistent kernel.

        When `stream` is true the kernel streams stdout as execution progresses
        (each chunk broadcast to subscribers as an "output" event) — used by the
        headless kernel server and live execution overlays.
        """
        self._state = "busy"
        self._current_code = code
        self._last_start = asyncio.get_event_loop().time()
        self._notify("busy", {"code": code, "pid": self._proc.pid if self._proc else None})
        try:
            resp = await self._send({"op": "run_code", "code": code,
                                     "timeout": timeout, "stream": bool(stream)})
            self._last_ok = bool(resp.get("ok"))
            self._last_error = resp.get("error")
            self._exec_count += 1
            if not resp.get("ok"):
                return resp
            return resp
        finally:
            self._last_duration_ms = \
                (asyncio.get_event_loop().time() - self._last_start) * 1000.0
            self._state = "idle"
            self._current_code = ""
            self._notify("idle", {"last_ok": self._last_ok,
                                  "duration_ms": self._last_duration_ms})
            self._notify("execution_done", self.status())

    async def list_variables(self) -> dict:
        resp = await self._send({"op": "list_variables"})
        return resp.get("variables", {})

    async def get_env(self) -> dict:
        resp = await self._send({"op": "get_env"})
        return resp.get("env", {})

    async def reset(self) -> dict:
        self._notify("busy", {"code": "", "reason": "reset"})
        try:
            resp = await self._send({"op": "reset"})
            self._last_ok = bool(resp.get("ok"))
            self._last_error = resp.get("error")
            self._exec_count += 1
            return resp
        finally:
            self._state = "idle"
            self._current_code = ""
            self._notify("reset", {"ok": self._last_ok})
            self._notify("idle", {"last_ok": self._last_ok,
                                  "duration_ms": self._last_duration_ms})

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        self._kill()
        self._state = "stopped"
        self._notify("stopped", {"pid": None})
        if self._proc:
            try:
                await self._proc.wait()
            except ProcessLookupError:
                pass
