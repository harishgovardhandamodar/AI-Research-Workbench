"""Launcher for the fine-tuning MCP servers.

Usage:
    python -m mcp_servers --server all      --transport stdio --project ./exp
    python -m mcp_servers --server dataset  --transport stdio --project ./exp
    python -m mcp_servers --server all      --transport http --host 0.0.0.0 --port 8788
    python -m mcp_servers --all             --transport stdio   # same as --server all

``--server`` selects: all (combined) | dataset | train | eval | experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo/src

from mcp_servers import SERVER_BUILDERS  # noqa: E402
from mcp_servers.project import set_project_dir  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fine-tuning MCP server.")
    parser.add_argument("--server", choices=sorted(SERVER_BUILDERS), default="all")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--project", default=None, help="Experiment project dir (default: cwd)")
    parser.add_argument("--all", action="store_true", help="Run the combined 'all' server")
    args = parser.parse_args()

    if args.all:
        args.server = "all"
    if args.project:
        set_project_dir(args.project)

    srv = SERVER_BUILDERS[args.server]()
    if args.transport == "http":
        asyncio.run(srv.run_http(args.host, args.port))
    else:
        asyncio.run(srv.run_stdio())


if __name__ == "__main__":
    main()
