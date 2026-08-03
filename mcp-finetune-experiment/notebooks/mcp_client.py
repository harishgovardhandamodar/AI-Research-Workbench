"""Thin MCP client used by the notebook: spawn the servers and ``call`` tools.

Usage (from the notebook):

    from mcp_client import Client
    mcp = Client(project_dir="..")
    await mcp.connect()                # starts the combined server (all tools)
    await mcp.list_tools()             # see every available tool
    await mcp.call("mcp.dataset.generate", {"name": "d1", "seed": 0})
    await mcp.close()

Connect over **stdio** (default) or **Streamable HTTP** with ``transport="http"``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running the client from the notebooks/ dir without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SERVERS = ("dataset", "train", "eval", "experiment")


class Client:
    def __init__(self, project_dir: str | Path = ".", transport: str = "stdio",
                 http_port: int = 8788):
        self.project_dir = Path(project_dir).resolve()
        self.transport = transport
        self.http_port = http_port
        self._sessions: list = []
        self._procs: list = []
        self._transport_ctxs: list = []

    # ------------------------------------------------------------- connect ----
    async def connect(self, servers: tuple[str, ...] = SERVERS) -> "Client":
        from mcp import ClientSession

        for name in servers:
            if self.transport == "stdio":
                await self._connect_stdio(name, ClientSession)
            else:
                await self._connect_http(name, ClientSession)
        return self

    async def _connect_stdio(self, name: str, ClientSession):
        from mcp.client.stdio import stdio_client, StdioServerParameters

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_servers", "--server", name, "--transport", "stdio",
                  "--project", str(self.project_dir)],
            env={"PYTHONPATH": str(SRC), "MCPFT_PROJECT_DIR": str(self.project_dir)},
        )
        ctx = stdio_client(params)
        read, write = await ctx.__aenter__()
        session = await ClientSession(read, write).__aenter__()
        await session.initialize()
        self._transport_ctxs.append(ctx)
        self._sessions.append(session)

    async def _connect_http(self, name: str, ClientSession):
        from mcp.client.streamable_http import streamable_http_client

        port = self.http_port + SERVERS.index(name)
        ctx = streamable_http_client(f"http://127.0.0.1:{port}/mcp")
        read, write = await ctx.__aenter__()
        session = await ClientSession(read, write).__aenter__()
        await session.initialize()
        self._transport_ctxs.append(ctx)
        self._sessions.append(session)

    # ---------------------------------------------------------------- tools ----
    async def list_tools(self) -> list[dict]:
        out = []
        for session in self._sessions:
            res = await session.list_tools()
            for t in res.tools:
                out.append({"name": t.name, "description": t.description,
                            "schema": t.inputSchema})
        return out

    async def call(self, name: str, arguments: dict | None = None):
        # Find the session that exposes this tool.
        for session in self._sessions:
            res = await session.list_tools()
            if any(t.name == name for t in res.tools):
                result = await session.call_tool(name, arguments or {})
                return _decode(result)
        raise KeyError(f"no server exposes tool {name!r}; did you connect()?")

    async def close(self):
        for session in reversed(self._sessions):
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        for ctx in reversed(self._transport_ctxs):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def _decode(result) -> dict:
    """MCP call_tool returns text content and/or structuredContent."""
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        return sc
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    if texts:
        import json

        try:
            return json.loads(texts[0])
        except Exception:  # noqa: BLE001
            return {"text": texts[0]}
    return {"text": ""}
