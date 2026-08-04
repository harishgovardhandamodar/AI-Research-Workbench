"""Model Context Protocol (MCP) integration: the workbench acts as an MCP Host.

Discovers tools from local (stdio) and remote (streamable HTTP) MCP servers and
merges them into the agent's tool set. Tool names are namespaced as
``<server>__<tool>`` so collisions are impossible and provenance is clear.

Approval policy (human-in-the-loop):
  - tools annotated ``readOnlyHint=True`` (or on a ``trusted`` server) run freely;
  - anything else (writable / compute / unknown) asks the user the first time and
    remembers the grant, matching the workbench's permission model.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from .artifacts.store import Artifact
from .paths import ROOT

ToolFn = Callable[..., Awaitable[str]]

DEFAULT_SERVERS = [
    {
        "name": "science",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/science_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "privacy",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/privacy_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "robustness",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/robustness_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "arxiv",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/arxiv_replication.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "graphrag",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/graphrag_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "github",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/github_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
]


def _resolve(command: str) -> str:
    return command.replace("{python}", sys.executable)


class MCPConnection:
    def __init__(self, config: dict):
        self.config = config
        self.name = config.get("name", "mcp")
        self._streams = None
        self._session = None
        self._tools: list | None = None
        self.error: str | None = None

    async def _connect(self):
        if self._session is not None:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client

            transport = self.config.get("transport", "stdio")
            if transport == "stdio":
                # Inherit the full parent environment (git needs PATH for the
                # github server) plus the server's own overrides, and tell the
                # github server where the experiment management repo lives.
                import os

                env = dict(os.environ)
                env.update(self.config.get("env") or {})
                try:
                    from .state import CONFIG  # late import avoids a cycle

                    mgmt = (CONFIG.get("management") or {}).get("repo_dir") or ""
                    if mgmt:
                        env["FOX_MGMT_REPO"] = str(Path(mgmt).expanduser().resolve())
                except Exception:  # noqa: BLE001
                    pass
                params = StdioServerParameters(
                    command=_resolve(self.config.get("command", "python3")),
                    args=self.config.get("args") or [],
                    env=env,
                    cwd=str(ROOT),
                )
                self._streams = stdio_client(params)
            else:
                url = self.config.get("url", "")
                headers = self.config.get("headers") or {}
                self._streams = streamable_http_client(url, headers=headers)
            self._read, self._write = await self._streams.__aenter__()
            self._session = ClientSession(self._read, self._write)
            await self._session.__aenter__()
            await self._session.initialize()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            await self.close()
            raise

    async def list_tools(self) -> list:
        await self._connect()
        if self._tools is None:
            res = await self._session.list_tools()
            self._tools = list(res.tools)
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        await self._connect()
        try:
            res = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments or {}),
                timeout=120.0)
        except asyncio.TimeoutError:
            # Kill the connection so a wedged server process can't block the
            # agent indefinitely; it will reconnect on the next call.
            await self.close()
            return ("[error] MCP tool call timed out after 120s (server "
                    "connection reset)"), True
        parts = []
        for block in getattr(res, "content", []) or []:
            btype = getattr(block, "type", "text")
            if btype == "text":
                parts.append(getattr(block, "text", ""))
            elif btype == "image":
                parts.append(f"[image {getattr(block, 'mimeType', '')}]")
            elif btype == "resource":
                parts.append(f"[resource {getattr(block, 'uri', '')}]")
        return "\n".join(p for p in parts if p), bool(getattr(res, "isError", False))

    async def close(self):
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self._streams is not None:
            try:
                await self._streams.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._streams = None
        self._tools = None


def _mcp_installed() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


def _persist_graph(ctx, tool_name: str, raw: str):
    """Auto-export a built arXiv knowledge graph into the project.

    Each paper graph lands in ``<project>/knowledge_graphs/<arxiv_id>.json``
    (merged graphs as ``corpus.json``) and is also registered as an artifact so
    it survives restarts and shows up in the UI.
    """
    try:
        g = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(g, dict) or g.get("error"):
        return
    graphs_dir = ctx.artifacts.project_dir / "knowledge_graphs"
    graphs_dir.mkdir(parents=True, exist_ok=True)
    if tool_name == "merge_knowledge_graphs":
        fname = "corpus.json"
    else:
        pid = str(g.get("paper_id", ""))
        aid = pid.removeprefix("paper:") if pid.startswith("paper:") else pid
        fname = f"{aid}.json" if aid else f"graph-{int(time.time())}.json"
    out = graphs_dir / fname
    out.write_text(json.dumps(g, indent=2))
    try:
        art = Artifact(kind="data", name=f"graph-{fname[:-5]}",
                       description=f"Persisted knowledge graph: {fname}",
                       code="", env={},
                       message_id=getattr(ctx, "message_id", ""),
                       run_id=getattr(ctx, "run_id", ""))
        ctx.artifacts.add_artifact(art, data=json.dumps(g).encode(),
                                   data_type="text")
    except Exception:  # noqa: BLE001
        pass


# Tools whose successful JSON result should be auto-persisted per project.
_PERSISTED_GRAPH_TOOLS = ("build_knowledge_graph_from_notes",
                          "merge_knowledge_graphs")


class MCPRegistry:
    def __init__(self, servers: list[dict] | None = None):
        self._servers: dict[str, dict] = {}
        for s in servers or []:
            name = (s.get("name") or "").strip()
            if name:
                self._servers[name] = dict(s)
        self._conns: dict[str, MCPConnection] = {}
        self._available = _mcp_installed()

    # -- lifecycle ----------------------------------------------------------
    def connection(self, name: str) -> MCPConnection:
        if name not in self._conns:
            self._conns[name] = MCPConnection(self._servers.get(name, {"name": name}))
        return self._conns[name]

    def server_names(self) -> list[str]:
        return list(self._servers.keys())

    async def statuses(self) -> list[dict]:
        out = []
        for name in self._servers:
            item = {"name": name, "transport": self._servers[name].get("transport", "stdio"),
                    "ok": False, "error": None, "tools": []}
            if not self._available:
                item["error"] = "mcp package not installed"
                out.append(item)
                continue
            conn = self.connection(name)
            try:
                tools = await conn.list_tools()
                item["ok"] = True
                item["tools"] = [t.name for t in tools]
            except Exception as e:  # noqa: BLE001
                item["error"] = f"{type(e).__name__}: {e}"
            out.append(item)
        return out

    # -- agent integration --------------------------------------------------
    async def build_tools(self, ctx) -> tuple[list[dict], dict[str, ToolFn]]:
        """Return (llm_tool_schemas, {namespaced_name: async callable}) for all MCP tools."""
        schemas: list[dict] = []
        fns: dict[str, ToolFn] = {}
        if not self._available:
            return schemas, fns
        for sname in self._servers:
            conn = self.connection(sname)
            try:
                tools = await conn.list_tools()
            except Exception:  # noqa: BLE001
                continue
            for t in tools:
                tname = t.name
                full = f"{sname}__{tname}"
                desc = getattr(t, "description", "") or ""
                input_schema = getattr(t, "input_schema", None) or {
                    "type": "object", "properties": {}}
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": full,
                        "description": (f"[MCP {sname}] {desc}" if desc
                                        else f"[MCP {sname}] Call tool {tname}"),
                        "parameters": input_schema,
                    },
                })
                fns[full] = self._make_caller(ctx, conn, sname, t)
        return schemas, fns

    def _make_caller(self, ctx, conn: MCPConnection, sname: str, tool) -> ToolFn:
        trusted = bool(self._servers.get(sname, {}).get("trusted", False))
        annotations = getattr(tool, "annotations", None)
        read_only = bool(getattr(annotations, "read_only_hint", None)) if annotations else False
        full_name = f"{sname}__{tool.name}"
        # Ingest/extract run in-process (not the stdio subprocess) so the host
        # can stream live progress sub-steps into the workflow tracker.
        inproc = (sname == "arxiv" and tool.name in ("ingest_arxiv_paper",
                                                     "extract_paper_text"))
        async def caller(**args) -> str:
            permissions = getattr(ctx, "permissions", None)
            approval = getattr(ctx, "approval", None)
            workflow = getattr(ctx, "workflow", None)
            if not read_only and not trusted and permissions is not None:
                key = f"{sname}__{tool.name}"
                grant = permissions.check("mcp_tool", key)
                if grant == "ask":
                    if approval is None:
                        return "[denied] MCP tool requires approval but no approval channel is available."
                    # Surface the approval request in the chat status line so the
                    # user knows the agent is waiting on them, not stuck.
                    emit_fn = getattr(ctx, "emit", None)
                    if emit_fn:
                        try:
                            await emit_fn("status", {"message":
                                f"⏸ Waiting for your approval to run {sname}__{tool.name}…"})
                        except Exception:  # noqa: BLE001
                            pass
                    stage = workflow.stage_for_tool(full_name) if workflow else None
                    if stage:
                        await workflow.update_stage(
                            stage, "waiting_approval",
                            detail="Waiting for your approval…",
                            message=f"Permission needed for {sname}__{tool.name}")
                    ok, temporary = await approval.request(
                        "mcp_tool", key,
                        f"MCP tool '{tool.name}' on server '{sname}' may modify data "
                        f"or launch compute. Approve?")
                    if not ok:
                        return "[denied by user]"
                    # A temporary approval is NOT remembered, so the next similar
                    # request still prompts (the ask is never silenced).
                    if not temporary:
                        permissions.record("mcp_tool", key, "allow")
                    if stage:
                        await workflow.update_stage(stage, "running",
                                                    detail="Approved — running…")
            if inproc:
                import importlib

                mod = importlib.import_module("mcp_servers.arxiv_replication")
                fn = getattr(mod, "_ingest_impl" if tool.name == "ingest_arxiv_paper"
                            else "_extract_impl")
                stage = workflow.stage_for_tool(full_name) if workflow else None

                async def progress(message: str, pct: float):
                    if workflow and stage:
                        await workflow.update_stage(stage, "running",
                                                    detail=message, pct=pct)

                args = dict(args)
                if tool.name == "ingest_arxiv_paper" and "work_dir" not in args:
                    args["work_dir"] = str(ROOT / "papers")
                args["progress"] = progress
                try:
                    text = await fn(**args)
                except Exception as e:  # noqa: BLE001
                    return f"[error] MCP tool '{tool.name}' failed: {type(e).__name__}: {e}"
                return f"[MCP:{sname}] {text}" if text else f"[MCP:{sname}] (no output)"
            try:
                text, is_err = await conn.call_tool(tool.name, args)
            except Exception as e:  # noqa: BLE001
                return f"[error] MCP tool '{tool.name}' failed: {type(e).__name__}: {e}"
            if (sname == "arxiv" and tool.name in _PERSISTED_GRAPH_TOOLS
                    and not is_err and text):
                try:
                    _persist_graph(ctx, tool.name, text)
                except Exception:  # noqa: BLE001
                    pass
            return f"[MCP:{sname}] {text}" if text else f"[MCP:{sname}] (no output)"

        caller.__name__ = full_name
        return caller

    async def close(self):
        for conn in self._conns.values():
            await conn.close()
        self._conns.clear()
