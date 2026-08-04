"""A small FastMCP-like wrapper over the low-level ``mcp`` SDK Server.

Enables the declarative ``@server.tool(...)`` style used across the scaffold and
lets a single server run over stdio or Streamable HTTP.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)

log = logging.getLogger("mcpft.server")

Handler = Callable[..., Awaitable[Any]]


class ToolServer:
    def __init__(self, name: str, version: str = "0.1.0",
                 instructions: str | None = None):
        self.name = name
        self._tools: list[tuple[Tool, Handler]] = []
        self._resources: list[tuple[Resource, Callable[[], str]]] = []
        self._server = Server(
            name,
            version=version,
            instructions=instructions,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_read_resource=self._read_resource,
        )

    # ---------------------------------------------------------- registration --
    def tool(self, name: str, description: str, schema: dict,
             handler: Handler | None = None) -> Handler:
        def reg(fn: Handler) -> Handler:
            self._tools.append((Tool(name=name, description=description,
                                     inputSchema=schema), fn))
            return fn

        return reg(handler) if handler is not None else reg

    def resource(self, uri: str, name: str, mime_type: str,
                 loader: Callable[[], str]) -> None:
        self._resources.append(
            (Resource(uri=uri, name=name, mimeType=mime_type, description=""), loader))

    # ------------------------------------------------------------ handlers ----
    async def _list_tools(self, ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=[t for t, _ in self._tools])

    async def _call_tool(self, ctx, params: CallToolRequestParams) -> CallToolResult:
        args = dict(params.arguments or {})
        for tool, handler in self._tools:
            if tool.name == params.name:
                try:
                    result = await handler(**args)
                except Exception as exc:  # noqa: BLE001
                    log.warning("tool %s failed: %r", params.name, exc)
                    return self._result({"error": f"{type(exc).__name__}: {exc}"},
                                        is_error=True)
                return self._result(result)
        return self._result({"error": f"unknown tool: {params.name}"}, is_error=True)

    async def _list_resources(self, ctx, params) -> ListResourcesResult:
        return ListResourcesResult(resources=[r for r, _ in self._resources])

    async def _read_resource(self, ctx, params: ReadResourceRequestParams) -> ReadResourceResult:
        for resource, loader in self._resources:
            if resource.uri == params.uri:
                return ReadResourceResult(contents=[
                    TextResourceContents(uri=resource.uri, mimeType=resource.mimeType,
                                         text=loader())])
        return ReadResourceResult(contents=[])

    # -------------------------------------------------------------- results ----
    @staticmethod
    def _result(data: Any, is_error: bool = False) -> CallToolResult:
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=data,
            isError=is_error,
        )

    # ----------------------------------------------------------- transports ----
    async def run_stdio(self) -> None:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            init = self._server.create_initialization_options()
            await self._server.run(read_stream, write_stream, init)

    def http_app(self):
        from mcp.server.streamable_http import streamable_http_app

        return streamable_http_app(self._server)

    async def run_http(self, host: str = "0.0.0.0", port: int = 8788) -> None:
        import uvicorn

        config = uvicorn.Config(self.http_app(), host=host, port=port, log_level="info")
        await uvicorn.Server(config).serve()
