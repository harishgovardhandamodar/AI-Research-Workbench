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
    """Async wrapper around one kernel subprocess."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd or Path.cwd()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._stderr_tail: str = ""

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
    async def run_code(self, code: str, timeout: float = 30.0) -> dict:
        resp = await self._send({"op": "run_code", "code": code, "timeout": timeout})
        if not resp.get("ok"):
            return resp
        return resp

    async def list_variables(self) -> dict:
        resp = await self._send({"op": "list_variables"})
        return resp.get("variables", {})

    async def get_env(self) -> dict:
        resp = await self._send({"op": "get_env"})
        return resp.get("env", {})

    async def reset(self) -> dict:
        resp = await self._send({"op": "reset"})
        return resp

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        self._kill()
        if self._proc:
            try:
                await self._proc.wait()
            except ProcessLookupError:
                pass
