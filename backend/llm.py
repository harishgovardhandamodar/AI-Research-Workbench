"""OpenAI-compatible LLM client (hybrid routing for local models).

Routing policy:
  - Plain chat (no tools) and model listing -> cluster gateway (default
    http://localhost:8081/v1, the hive cluster from ~/WorkBook/Ollama-local-hives-cluster).
  - Tool-calling turns -> direct local Ollama (default http://127.0.0.1:11434/v1),
    because the hive gateway currently strips `tools` from requests.

Both endpoints are 100% local. The direct endpoint hosts the same models the
cluster routes to.
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

# Endpoints are overridable via env vars (the Docker image uses
# host.docker.internal to reach Ollama/gateway running on the host).
DEFAULT_BASE_URL = os.environ.get("FOX_BASE_URL", "http://localhost:8081/v1")
DEFAULT_TOOL_BASE_URL = os.environ.get("FOX_TOOL_BASE_URL", "http://127.0.0.1:11434/v1")
DEFAULT_MODEL = os.environ.get("FOX_MODEL", "qwen3.6:latest")
DEFAULT_MAX_ITERS = 12


class LLMError(RuntimeError):
    pass


class LLMClient:
    """Hybrid OpenAI-compatible client: gateway for plain chat, direct for tools."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 tool_base_url: str = DEFAULT_TOOL_BASE_URL,
                 api_key: str = "ollama",
                 model: str = DEFAULT_MODEL, temperature: float = 0.2,
                 max_tokens: int = 4096):
        self.base_url = base_url
        self.tool_base_url = tool_base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._gateway = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._tool = AsyncOpenAI(base_url=tool_base_url, api_key=api_key)

    def _pick(self, tools: Optional[list]) -> AsyncOpenAI:
        return self._tool if tools else self._gateway

    async def list_models(self) -> list[dict]:
        last = None
        for client in (self._gateway, self._tool):
            try:
                resp = await client.models.list()
                seen = {}
                for m in resp.data:
                    seen.setdefault(m.id, {"id": m.id, "owned_by": getattr(m, "owned_by", "")})
                return list(seen.values())
            except Exception as e:  # noqa: BLE001
                last = e
        raise LLMError(f"Cannot reach LLM server: {last}")

    def _params(self, messages, tools, temperature):
        params = dict(model=self.model, messages=messages, temperature=temperature,
                      max_tokens=self.max_tokens)
        if tools:
            params["tools"] = tools
        return params

    async def complete(self, messages: list[dict], tools: Optional[list] = None,
                       temperature: Optional[float] = None) -> dict:
        """Non-streaming completion. Returns full assistant message dict (may contain tool_calls)."""
        temp = temperature if temperature is not None else self.temperature
        client = self._pick(tools)
        try:
            resp = await client.chat.completions.create(**self._params(messages, tools, temp))
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e
        return self._msg_to_dict(resp.choices[0].message)

    async def stream(self, messages: list[dict], tools: Optional[list] = None,
                     temperature: Optional[float] = None,
                     on_delta=None) -> dict:
        """Stream a completion, invoking on_delta(str) for each token chunk.

        Returns the full assistant message dict (may contain tool_calls for the
        final completion of a streaming tool-calling model).
        """
        temp = temperature if temperature is not None else self.temperature
        client = self._pick(tools)
        try:
            resp = await client.chat.completions.create(
                **self._params(messages, tools, temp), stream=True)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e

        full = {"role": "assistant", "content": "", "tool_calls": []}
        try:
            async for chunk in resp:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    full["content"] += delta.content
                    if on_delta:
                        await on_delta(delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index if tc.index is not None else 0
                        while len(full["tool_calls"]) <= idx:
                            full["tool_calls"].append({"id": "", "type": "function",
                                                       "function": {"name": "", "arguments": ""}})
                        slot = full["tool_calls"][idx]
                        if tc.id:
                            slot["id"] += tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM stream failed: {e}") from e

        # Normalise argument JSON so callers can rely on a dict.
        for tc in full["tool_calls"]:
            try:
                tc["function"]["arguments"] = json.loads(
                    tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                tc["function"]["arguments"] = {}
        if not full["tool_calls"]:
            full.pop("tool_calls", None)
        return full

    @staticmethod
    def _msg_to_dict(msg) -> dict:
        d = {"role": "assistant", "content": msg.content or ""}
        tcs = getattr(msg, "tool_calls", None) or []
        if tcs:
            d["tool_calls"] = []
            for tc in tcs:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                d["tool_calls"].append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": args},
                })
        return d
