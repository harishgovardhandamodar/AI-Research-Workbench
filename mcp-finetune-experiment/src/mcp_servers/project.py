"""Resolve the experiment project directory for the MCP servers.

Priority: the ``--project`` CLI arg set by the launcher, then the
``MCPFT_PROJECT_DIR`` env var, then the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_OVERRIDE: str | None = None


def set_project_dir(path: str) -> None:
    global _PROJECT_OVERRIDE
    _PROJECT_OVERRIDE = path


def project_dir() -> Path:
    raw = _PROJECT_OVERRIDE or os.environ.get("MCPFT_PROJECT_DIR") or os.getcwd()
    return Path(raw).resolve()
