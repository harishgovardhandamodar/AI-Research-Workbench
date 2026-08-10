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
    {
        "name": "autoresearch",
        "transport": "stdio",
        "command": "{python}",
        "args": ["mcp_servers/autoresearch_tools.py"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    # ---- Domain Knowledge LoRA fine-tuning + RAG verification ----
    # Packages use relative imports, so they run as modules (-m), not scripts.
    {
        "name": "dk_lora",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.dk_lora.server"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "ft_validate",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.ft_validate.server"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    # ---- Flint charts MCP (semantic chart spec -> rendered chart) ----
    # Node-based stdio server, installed in the container via
    # `npm install -g flint-chart-mcp` (bin: /usr/local/bin/flint-chart-mcp).
    {
        "name": "flint",
        "transport": "stdio",
        "command": "/usr/local/bin/flint-chart-mcp",
        "args": [],
        "env": {},
        "trusted": False,
    },
    # ---- Experiment planner MCP (plan -> propose -> confirm -> execute) ----
    {
        "name": "experiment_planner",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.experiment_planner.server"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    # ---- EDA suite (five servers sharing a disk-backed DatasetStore) ----
    {
        "name": "eda_profiler",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.eda_mcp.profiler"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "eda_univariate",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.eda_mcp.univariate"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "eda_multivariate",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.eda_mcp.multivariate"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "eda_visualizer",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.eda_mcp.visualizer"],
        "env": {"PYTHONPATH": str(ROOT)},
        "trusted": False,
    },
    {
        "name": "eda_report",
        "transport": "stdio",
        "command": "{python}",
        "args": ["-m", "mcp_servers.eda_mcp.report"],
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
                    from .paths import PROJECTS_DIR
                    env["FOX_PLAN_STORE"] = str(PROJECTS_DIR)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from .state import CONFIG  # late import avoids a cycle

                    mgmt = (CONFIG.get("management") or {}).get("repo_dir") or ""
                    if mgmt:
                        env["FOX_MGMT_REPO"] = str(Path(mgmt).expanduser().resolve())
                    # Give the git MCP server the configured GitHub repo so it can
                    # point `origin` at it before pushing (same as the app's push).
                    from . import experiment_repo

                    gurl = experiment_repo.github_remote_url()
                    if gurl:
                        env["FOX_MGMT_GITHUB_URL"] = gurl
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
                # Resource blocks may carry inline text — surface it rather than
                # collapsing to a bare uri placeholder.
                rtext = getattr(block, "text", "") or ""
                if rtext:
                    parts.append(rtext)
                else:
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


def _tool_params(schema: dict) -> list[dict]:
    """Collapse a tool's JSON input schema into an ordered param list."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = []
    for name, p in props.items():
        out.append({"name": str(name), "required": name in required,
                    "type": (p or {}).get("type", "")})
    # include required names not present in properties (lenient servers)
    for r in schema.get("required") or []:
        if r not in props:
            out.append({"name": str(r), "required": True, "type": ""})
    return out


class MCPRegistry:
    def __init__(self, servers: list[dict] | None = None):
        self._servers: dict[str, dict] = {}
        for s in servers or []:
            name = (s.get("name") or "").strip()
            if name:
                cfg = dict(s)
                cfg.setdefault("enabled", True)
                self._servers[name] = cfg
        self._conns: dict[str, MCPConnection] = {}
        self._available = _mcp_installed()
        self._status_cache: list[dict] | None = None
        self._status_cache_ts: float = 0.0

    # -- lifecycle ----------------------------------------------------------
    def connection(self, name: str) -> MCPConnection:
        if name not in self._conns:
            self._conns[name] = MCPConnection(self._servers.get(name, {"name": name}))
        return self._conns[name]

    def server_names(self) -> list[str]:
        return list(self._servers.keys())

    def enabled_servers(self) -> list[str]:
        return [n for n, c in self._servers.items() if c.get("enabled", True)]

    async def statuses(self) -> list[dict]:
        # Probe enabled servers concurrently (each stdio server spawns a
        # subprocess and does an MCP handshake; running them serially made the
        # Agents tab slow). Disabled servers are reported without being probed.
        # A short per-server timeout + a small cache keep it snappy.
        now = time.time()
        if self._status_cache and now - self._status_cache_ts < 5.0:
            return list(self._status_cache)

        names = list(self._servers)

        async def _probe(name: str) -> dict:
            cfg = self._servers[name]
            item = {"name": name, "transport": cfg.get("transport", "stdio"),
                    "enabled": bool(cfg.get("enabled", True)),
                    "trusted": bool(cfg.get("trusted", False)),
                    "ok": False, "error": None, "tools": [], "tool_catalog": []}
            if not self._available:
                item["error"] = "mcp package not installed"
                return item
            if not item["enabled"]:
                item["error"] = "disabled"
                return item
            conn = self.connection(name)
            try:
                tools = await asyncio.wait_for(conn.list_tools(), timeout=12.0)
                item["ok"] = True
                item["tools"] = [t.name for t in tools]
                item["tool_catalog"] = [
                    {"name": t.name,
                     "description": (getattr(t, "description", "") or "")[:300],
                     "read_only": bool(getattr(
                         getattr(t, "annotations", None), "read_only_hint", None)),
                     "params": _tool_params(getattr(t, "input_schema", None) or {})}
                    for t in tools]
            except asyncio.TimeoutError:
                item["error"] = "timed out probing server (12s)"
                await conn.close()
            except Exception as e:  # noqa: BLE001
                item["error"] = f"{type(e).__name__}: {e}"
            return item

        results = await asyncio.gather(*(_probe(n) for n in names))
        self._status_cache = results
        self._status_cache_ts = now
        return results

    def clear_status_cache(self) -> None:
        self._status_cache = None
        self._status_cache_ts = 0.0

    # -- agent integration --------------------------------------------------
    async def build_tools(self, ctx) -> tuple[list[dict], dict[str, ToolFn]]:
        """Return (llm_tool_schemas, {namespaced_name: async callable}) for all MCP tools."""
        schemas: list[dict] = []
        fns: dict[str, ToolFn] = {}
        if not self._available:
            return schemas, fns
        for sname in self.enabled_servers():
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
            # Fail fast on missing required args instead of a server-side error.
            schema = getattr(tool, "input_schema", None) or {}
            missing = [r for r in (schema.get("required") or []) if r not in args]
            if missing:
                return (f"[error] {full_name} is missing required argument(s): "
                        f"{', '.join(missing)}")
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


async def call_mcp_tool(registry: MCPRegistry, server: str, tool: str,
                        args: dict | None = None, *,
                        permissions=None, broker=None, emit=None,
                        workflow=None) -> tuple[str, bool]:
    """Deterministically invoke an MCP tool, honoring the workbench permission
    model. Returns (text, is_error).

    - read-only or trusted tools run without approval;
    - writable tools: an existing grant runs; an 'ask' goes to the
      ApprovalBroker when provided, otherwise the call is denied with a clear
      message. This mirrors ``MCPRegistry._make_caller`` so both the chat
      ``@mcp`` command and the REST endpoint share one policy.
    """
    if server not in registry._servers:
        return (f"[error] unknown MCP server '{server}' — "
                f"available: {list(registry._servers)}. Add it in Settings → MCP.",
                True)
    conn = registry.connection(server)
    try:
        tools = await conn.list_tools()
    except Exception as e:  # noqa: BLE001
        return (f"[error] MCP server '{server}' unreachable: {e} — check it's "
                "enabled in Settings → MCP and that the command/URL is valid.", True)
    match = next((t for t in tools if t.name == tool), None)
    if match is None:
        return (f"[error] tool '{server}__{tool}' not found on that server", True)
    # Basic input validation against the tool's JSON schema (required args).
    schema = getattr(match, "input_schema", None) or {}
    required = list(schema.get("required") or [])
    missing = [r for r in required if r not in (args or {})]
    if missing:
        props = {k: (schema.get("properties") or {}).get(k, {})
                 for k in required}
        return (f"[error] {server}__{tool} is missing required argument(s): "
                f"{', '.join(missing)}. Expected: {json.dumps(props, default=str)}",
                True)
    annotations = getattr(match, "annotations", None)
    read_only = bool(getattr(annotations, "read_only_hint", None)) if annotations else False
    trusted = bool(registry._servers.get(server, {}).get("trusted", False))
    key = f"{server}__{tool}"
    if not read_only and not trusted and permissions is not None:
        grant = permissions.check("mcp_tool", key)
        if grant == "ask":
            if broker is None:
                return (f"[denied] {key} may modify data or launch compute and "
                        "requires your approval.", True)
            if emit:
                try:
                    await emit("status", {"message":
                        f"⏸ Waiting for your approval to run {key}…"})
                except Exception:  # noqa: BLE001
                    pass
            ok, _ = await broker.request(
                "mcp_tool", key,
                f"MCP tool '{tool}' on server '{server}' may modify data or "
                "launch compute. Approve?")
            if not ok:
                return ("[denied by user]", True)
            permissions.record("mcp_tool", key, "allow")
        elif grant == "deny":
            return (f"[denied] {key} is blocked by the permission policy.", True)
    try:
        text, is_err = await conn.call_tool(tool, args or {})
    except Exception as e:  # noqa: BLE001
        return (f"[error] MCP tool '{tool}' failed: {type(e).__name__}: {e}", True)
    return (text or "(no output)", is_err)
