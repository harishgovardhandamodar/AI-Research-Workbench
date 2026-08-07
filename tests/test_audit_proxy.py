"""End-to-end: a real MCP client talks to a real MCP server through the
agent-audit proxy, and every tool call lands in the audit store.

Spawns the downstream FastMCP server and the proxy as subprocesses, then
connects a client via stdio. Requires the `mcp` package (a runtime dependency
of the workbench).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

SERVER_SCRIPT = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mathserver")


@mcp.add_tool
def add(a: int, b: int) -> int:
    "Add two numbers"
    return a + b


@mcp.add_tool
def secret_tool(token: str) -> str:
    "Echo the token (must be redacted)"
    return f"got {token}"


if __name__ == "__main__":
    mcp.run("stdio")
'''


@pytest.mark.skipif(sys.version_info < (3, 11), reason="requires 3.11+")
def test_proxy_forwards_and_audits(tmp_path):
    asyncio.run(_run_proxy_test(tmp_path))


async def _run_proxy_test(tmp_path):
    audit_dir = tmp_path / "audit"
    server_file = tmp_path / "math_server.py"
    server_file.write_text(SERVER_SCRIPT)
    env = dict(os.environ)
    env["AUDIT_DIR"] = str(audit_dir)
    server_cmd = f"{sys.executable} {server_file}"

    from mcp.client.stdio import stdio_client as sc
    from mcp import ClientSession as CS, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "audit.cli", "proxy", "--server", server_cmd],
        env=env, cwd=str(Path(__file__).resolve().parent.parent))

    async with sc(params) as (read, write):
        async with CS(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "add" in names and "secret_tool" in names

            res = await session.call_tool("add", arguments={"a": 2, "b": 3})
            text = "".join(b.text for b in res.content if getattr(b, "type", "") == "text")
            assert "5" in text

            await session.call_tool("secret_tool",
                                    arguments={"token": "SUPERSECRETTOKEN123"})
            await asyncio.sleep(0.5)  # let the emitter drain

    # Verify the audit log.
    from audit import LocalAuditStore

    store = LocalAuditStore(audit_dir)
    events = store.query()
    assert len(events) >= 2
    add_events = [e for e in events if e["tool_name"] == "add"]
    sec_events = [e for e in events if e["tool_name"] == "secret_tool"]
    assert add_events and sec_events
    assert add_events[0]["result_summary"]["status"] == "ok"
    assert add_events[0]["source"] == "mcp_proxy"
    blob = json.dumps(sec_events[0])
    assert "SUPERSECRETTOKEN123" not in blob
    assert store.verify_chain()["ok"]
