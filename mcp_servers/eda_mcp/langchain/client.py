"""MCP client configuration for the five EDA servers, plus a helper to build a
``MultiServerMCPClient`` (requires ``langchain-mcp-adapters``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The five EDA MCP servers, run as separate stdio processes (per the design).
EDA_MCP_CONFIG = {
    "eda_profiler": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.eda_mcp.profiler"],
        "transport": "stdio",
    },
    "eda_univariate": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.eda_mcp.univariate"],
        "transport": "stdio",
    },
    "eda_multivariate": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.eda_mcp.multivariate"],
        "transport": "stdio",
    },
    "eda_visualizer": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.eda_mcp.visualizer"],
        "transport": "stdio",
    },
    "eda_report": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.eda_mcp.report"],
        "transport": "stdio",
    },
}


def _root() -> Path:
    return Path(__file__).resolve().parents[3]  # repo root


def mcp_env() -> dict:
    """Environment for the MCP subprocesses: repo root on PYTHONPATH and an
    explicit workspace so all five servers share the same DatasetStore."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_root()) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("FOX_EDA_WORKSPACE", str(_root() / "workbench" / "eda"))
    return env


def get_client(config: dict | None = None):
    """Build a ``MultiServerMCPClient`` for the EDA servers (or a custom config).

    Raises a helpful error when ``langchain-mcp-adapters`` is not installed.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "langchain-mcp-adapters is not installed. Run: "
            "pip install langchain langchain-mcp-adapters langchain-openai"
        ) from e
    cfg = config or EDA_MCP_CONFIG
    return MultiServerMCPClient({k: {**v, "env": mcp_env()} for k, v in cfg.items()})
