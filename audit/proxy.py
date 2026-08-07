"""Transparent local MCP proxy.

Sits between any MCP client (Claude Desktop, Cursor, custom hosts) and any MCP
server (stdio subprocess or streamable HTTP) and audits every request:

  * ``tools/call`` and ``resources/read`` are intercepted, redacted and
    persisted as :class:`audit.models.AuditEvent`.
  * ``tools/list`` / ``prompts/*`` are forwarded unchanged.
  * The proxy works over both transports:

      - stdio → target: the client spawns ``agent-audit proxy --server …``
      - HTTP → target: ``agent-audit proxy --server … --http :8010`` serves a
        streamable-HTTP MCP endpoint on ``/mcp``.

Claude Desktop config::

    "mcpServers": {
      "my-server": {
        "command": "agent-audit",
        "args": ["proxy", "--server", "python", "-m", "my_mcp_server"]
      }
    }
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from .emitter import AuditEmitter
from .models import AuditEvent
from .policy import risk_tier_for, severity_for_tier
from .redaction import redact
from .store import LocalAuditStore


def _default_store_dir() -> Path:
    return Path(os.environ.get("AUDIT_DIR", Path.home() / ".agent-audit"))


class AuditMCPProxy:
    def __init__(self, server_cmd: str | None = None,
                 server_args: list[str] | None = None,
                 http_url: str | None = None, http_headers: dict | None = None,
                 agent_id: str = "proxy-client",
                 store: LocalAuditStore | None = None,
                 env: dict | None = None):
        self.server_cmd = server_cmd
        self.server_args = server_args or []
        self.http_url = http_url
        self.http_headers = http_headers or {}
        self.agent_id = agent_id
        self.env = env
        self.store = store or LocalAuditStore(_default_store_dir())
        self.emitter = AuditEmitter(self.store)
        self._downstream = None
        self._down_session = None

    # ------------------------------------------------------------ downstream ---
    async def _connect_downstream(self):
        if self._down_session is not None:
            return
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if self.http_url:
            streams = streamable_http_client(self.http_url, headers=self.http_headers or None)
        elif self.server_cmd:
            command, args = _split_command(self.server_cmd, self.server_args)
            base_env = dict(os.environ)
            if self.env:
                base_env.update(self.env)
            params = StdioServerParameters(command=command, args=args, env=base_env,
                                           cwd=str(Path.cwd()))
            streams = stdio_client(params)
        else:
            raise RuntimeError("proxy needs --server <cmd> or --http <url>")
        read, write = await streams.__aenter__()
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        self._streams = streams
        self._down_session = session

    async def close_downstream(self):
        if self._down_session is not None:
            try:
                await self._down_session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._down_session = None
        if getattr(self, "_streams", None) is not None:
            try:
                await self._streams.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._streams = None

    # ------------------------------------------------------------------ audit ---
    def _audit(self, name: str, arguments: dict | None, started: float,
               status: str, error: str | None = None,
               result_is_error: bool = False) -> None:
        duration_ms = (time.perf_counter() - started) * 1000.0
        tier = risk_tier_for(name)
        event = AuditEvent(
            agent_id=self.agent_id, source="mcp_proxy",
            mcp_server=getattr(self, "_server_label", None),
            method="tools/call", tool_name=name,
            arguments_redacted=redact(arguments or {}),
            result_summary=AuditEvent.result_summary_for(
                status=status, error=error,
                size=None),
            duration_ms=duration_ms,
            severity=severity_for_tier(tier) if status == "ok" else "critical",
            tags=["mcp_proxy", tier],
        )
        try:
            asyncio.get_running_loop().create_task(self.emitter.emit(event))
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- handlers ---
    def _build_server(self, server_label: str):
        import mcp
        from mcp.server import Server
        from mcp.types import (CallToolRequest, CallToolResult, ListToolsRequest,
                               ListToolsResult, ReadResourceRequest,
                               ReadResourceRequestParams, TextContent)

        self._server_label = server_label
        server = Server("agent-audit-proxy")

        async def on_list_tools(ctx, params) -> ListToolsResult:
            await self._connect_downstream()
            res = await self._down_session.list_tools()
            return res

        async def on_call_tool(ctx, params) -> CallToolResult:
            await self._connect_downstream()
            name = params.name
            args = params.arguments
            started = time.perf_counter()
            try:
                res = await self._down_session.call_tool(name, arguments=args or {})
                is_err = bool(getattr(res, "isError", False))
                self._audit(name, args, started, "error" if is_err else "ok",
                            result_is_error=is_err)
                return res
            except Exception as e:  # noqa: BLE001
                self._audit(name, args, started, "error", error=f"{type(e).__name__}: {e}")
                return CallToolResult(
                    content=[TextContent(type="text",
                                         text=f"[audit-proxy] tool failed: {type(e).__name__}: {e}")],
                    isError=True)

        async def on_read_resource(ctx, params) -> Any:
            await self._connect_downstream()
            started = time.perf_counter()
            try:
                res = await self._down_session.read_resource(params.uri)
                self._audit(f"resource:{params.uri}", {"uri": params.uri}, started, "ok")
                return res
            except Exception as e:  # noqa: BLE001
                self._audit(f"resource:{params.uri}", {"uri": params.uri},
                            started, "error", error=f"{type(e).__name__}: {e}")
                raise

        async def on_prompts_list(ctx, params) -> Any:
            await self._connect_downstream()
            return await self._down_session.send_request(
                "prompts/list", {}, result_type=mcp.types.ListPromptsResult)

        async def on_prompts_get(ctx, params) -> Any:
            await self._connect_downstream()
            return await self._down_session.send_request(
                "prompts/get", params.model_dump(mode="python"),
                result_type=mcp.types.GetPromptResult)

        async def on_resources_list(ctx, params) -> Any:
            await self._connect_downstream()
            return await self._down_session.send_request(
                "resources/list", {}, result_type=mcp.types.ListResourcesResult)

        # Method strings + params types registered explicitly (the low-level
        # Server API takes (method, params_type, handler)). List-style methods
        # use PaginatedRequestParams; call/read/get use their own params models.
        server.add_request_handler("tools/list", mcp.types.PaginatedRequestParams,
                                   on_list_tools)
        server.add_request_handler("tools/call", mcp.types.CallToolRequestParams,
                                   on_call_tool)
        server.add_request_handler("resources/read", mcp.types.ReadResourceRequestParams,
                                   on_read_resource)
        server.add_request_handler("resources/list", mcp.types.PaginatedRequestParams,
                                   on_resources_list)
        server.add_request_handler("prompts/list", mcp.types.PaginatedRequestParams,
                                   on_prompts_list)
        server.add_request_handler("prompts/get", mcp.types.GetPromptRequestParams,
                                   on_prompts_get)

        return server

    # ---------------------------------------------------------------- entry ---
    async def run_stdio(self, server_label: str = "stdio"):
        from mcp.server.stdio import stdio_server

        server = self._build_server(server_label)
        self.emitter.start()
        # Connect downstream BEFORE serving so the spawn/initialize happens
        # outside any request-handler cancel scope (avoids nested task-group
        # teardown issues in the SDK's anyio-based transports).
        try:
            await self._connect_downstream()
        except Exception as e:  # noqa: BLE001
            print(f"[agent-audit] warning: downstream not reachable at startup: {e}",
                  file=sys.stderr)
        async with stdio_server() as (read, write):
            await server.run(read, write,
                             server.create_initialization_options())
        await self.close_downstream()
        await self.emitter.stop()

    async def run_http(self, host: str = "127.0.0.1", port: int = 8010,
                       server_label: str = "http"):
        import uvicorn

        server = self._build_server(server_label)
        self.emitter.start()
        try:
            await self._connect_downstream()
        except Exception as e:  # noqa: BLE001
            print(f"[agent-audit] warning: downstream not reachable at startup: {e}",
                  file=sys.stderr)
        app = server.streamable_http_app()
        config = uvicorn.Config(app, host=host, port=port,
                                log_level="info", lifespan="off")
        await uvicorn.Server(config).serve()


def _split_command(cmd: str, extra: list[str]) -> tuple[str, list[str]]:
    """Split a command string like "python -m my.server --flag" honoring quotes."""
    tokens = shlex.split(cmd)
    if not tokens:
        raise ValueError("empty --server command")
    return tokens[0], tokens[1:] + list(extra)
