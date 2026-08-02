"""Coordinator agent: a generalist scientific assistant that orchestrates tools
(run_python / run_r / run_shell / save_artifact) in a persistent sandboxed kernel,
emitting streaming events to the client."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from ..llm import LLMClient
from .approval import ApprovalBroker
from .tools import ToolContext, build_tools, get_tool_schemas

SYSTEM_PROMPT = """\
You are Fox, an open-source AI science workbench running fully on the user's machine.

You are a scientific research assistant that works hands-on with code. You help
computational biologists, chemists, physicists and data scientists run real analyses.

Working style:
- You solve problems by writing and running code in a persistent, sandboxed Python
  kernel (numpy, pandas, scipy, matplotlib available). Variables persist across calls.
- Use run_python for computation, data analysis and figures. Use matplotlib to make
  clear, well-labelled publication-style figures.
- Save important results (tables, summaries, data) with save_artifact so they become
  auditable artifacts.
- Use run_shell only when necessary; prefer the Python kernel. Shell commands that
  touch the network or are destructive will ask the user for permission.
- When a figure is produced, the kernel records its exact code and environment so it
  can be reproduced. Prefer to reference artifacts by their id.
- Be rigorous: cite numbers you actually computed. If you don't know, say so.
- Keep the user informed of what you're doing at each step. Be concise in prose.

Privacy: everything stays on the user's machine unless they explicitly approve a
shell command that touches the network.
"""


class Coordinator:
    def __init__(self, llm: LLMClient, ctx: ToolContext,
                 emit: Callable[[str, dict], Awaitable[None]] | None = None,
                 persist: Callable[[str, str, dict], None] | None = None,
                 max_iters: int = 8):
        self.llm = llm
        self.ctx = ctx
        self.emit = emit or (lambda t, p: None)
        self.persist = persist or (lambda r, c, m: None)
        self.max_iters = max_iters
        self.tools = build_tools(ctx)

    async def _on_delta(self, text: str):
        await self.emit("stream_delta", {"text": text})

    async def run_turn(self, messages: list[dict]) -> dict:
        """Run one agent turn over `messages` (already ending with the user message).

        Intermediate assistant/tool messages are persisted via `persist`.
        Returns {"text": final assistant text}."""
        tools = get_tool_schemas()
        for _ in range(self.max_iters):
            full = await self.llm.stream(messages, tools, on_delta=self._on_delta)
            if not full.get("tool_calls"):
                return {"text": full.get("content", "")}

            assistant_msg = {
                "role": "assistant",
                "content": full.get("content", "") or "",
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]}}
                    for tc in full["tool_calls"] if tc.get("function")
                ],
            }
            messages.append(assistant_msg)
            self.persist("assistant", assistant_msg["content"],
                         {"tool_calls": assistant_msg["tool_calls"]})

            for tc in full["tool_calls"]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments", {}) or {}
                if name not in self.tools:
                    result = f"[error] unknown tool: {name}"
                    ok = False
                else:
                    await self.emit("tool_start", {"id": tc.get("id"), "name": name,
                                                   "args": args, "ok": True})
                    try:
                        if name == "run_shell":
                            result = await self.tools[name](command=args.get("command", ""),
                                                            timeout=args.get("timeout", 30))
                        else:
                            result = await self.tools[name](**args)
                        ok = not result.startswith("[error]")
                    except Exception as e:  # noqa: BLE001
                        result = f"[error] {type(e).__name__}: {e}"
                        ok = False
                    await self.emit("tool_result", {"id": tc.get("id"), "name": name,
                                                    "output": result, "ok": ok})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
                self.persist("tool", result, {"name": name, "tool_call_id": tc.get("id", "")})

        return {"text": ""}
