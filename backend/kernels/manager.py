"""Per-session kernel manager: owns the Python and R kernel instances.

Kernels execute with the workbench repository root as their working directory so
that repo-relative paths (e.g. examples/experiments/...) resolve naturally. The
per-project `session_dir` remains the home for artifacts and user-created files.

Kernels can either run locally (embedded subprocess, the default) or against a
headless kernel server (:mod:`backend.kernels.server`) reached over HTTP +
WebSocket. Use :func:`make_kernel_manager` to pick a mode.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..paths import ROOT
from .python_kernel import PythonKernel
from .r_kernel import RKernel


def make_kernel_manager(session_dir: Path, *, remote_url: str | None = None) -> "KernelManager":
    """Build a kernel manager, local or remote.

    `remote_url` is a ``http(s)://host:port`` pointing at a ``fox-kernel``
    server; when given, the Python kernel runs on that server and its live
    status/output events are streamed back over WebSocket. The R kernel always
    runs locally (each R call already spawns a fresh Rscript).

    Falls back to the `FOX_KERNEL_URL` environment variable when unset.
    """
    url = remote_url or os.environ.get("FOX_KERNEL_URL")
    if url:
        from .remote import RemoteKernelManager
        return RemoteKernelManager(url, session_dir=session_dir)
    return KernelManager(session_dir)


class KernelManager:
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.workspace_dir = ROOT
        session_dir.mkdir(parents=True, exist_ok=True)
        self.python = PythonKernel(cwd=self.workspace_dir)
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
