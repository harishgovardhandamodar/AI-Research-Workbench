"""R kernel. Uses `Rscript` if available; otherwise reports a clear error.

This is the MVP version: each `run_code` invocation runs a fresh `Rscript -e`
process, so the environment is not yet persistent across calls (unlike the Python
kernel). A persistent R session is a Phase-1+ improvement.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


class RUnavailableError(RuntimeError):
    pass


class RKernel:
    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd or Path.cwd()
        self._rscript = shutil.which("Rscript")
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._rscript is not None

    async def run_code(self, code: str, timeout: float = 30.0) -> dict:
        if not self._rscript:
            raise RUnavailableError(
                "Rscript not found. Install R (https://www.r-project.org) to use the R kernel."
            )
        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                self._rscript, "--vanilla", "-e", code,
                cwd=str(self.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise
            text = out.decode(errors="replace")
            err_text = err.decode(errors="replace")
            return {
                "ok": proc.returncode == 0,
                "output": text,
                "error": err_text if proc.returncode != 0 else "",
                "figures": [],
                "variables": {},
            }

    async def get_env(self) -> dict:
        return {
            "r": "available" if self.available else "not installed",
            "r_persistent": False,
            "r_note": ("R runs a fresh Rscript process per call, so variables "
                       "do not persist between calls (unlike the Python kernel)."),
        }

    async def reset(self) -> dict:
        return {"ok": True}

    async def stop(self):
        return None
