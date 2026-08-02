"""Per-session kernel manager: owns the Python and R kernel instances."""

from __future__ import annotations

from pathlib import Path

from .python_kernel import PythonKernel
from .r_kernel import RKernel


class KernelManager:
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        self.python = PythonKernel(cwd=session_dir)
        self.r = RKernel(cwd=session_dir)
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
