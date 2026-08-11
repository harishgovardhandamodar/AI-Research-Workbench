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

import asyncio
import json
import os
import time
import urllib.request
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
                 max_tokens: int = 4096,
                 retries: int = 2, retry_backoff: float = 1.0):
        self.base_url = base_url
        self.tool_base_url = tool_base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Round-12: retry transient LLM failures with backoff so autonomous
        # turns survive Ollama restarts / timeouts instead of dying.
        self.retries = max(0, int(retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        # A bounded timeout + minimal retries so an unreachable endpoint fails
        # fast with a visible error instead of hanging silently for minutes.
        self._gateway = AsyncOpenAI(base_url=base_url, api_key=api_key,
                                    timeout=120.0, max_retries=1)
        self._tool = AsyncOpenAI(base_url=tool_base_url, api_key=api_key,
                                 timeout=120.0, max_retries=1)
        self._models_cache: list[dict] | None = None
        self._models_cache_ts: float = 0.0

    def _pick(self, tools: Optional[list]) -> AsyncOpenAI:
        return self._tool if tools else self._gateway

    @staticmethod
    def _transient(exc: Exception) -> bool:
        """True when the error looks transient (connection/timeout), so the
        request is worth retrying."""
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        msg = str(exc).lower()
        return any(k in msg for k in (
            "connection", "timeout", "timed out", "temporarily unavailable",
            "connection reset", "refused", "network error", "502", "503"))

    async def _run(self, client: AsyncOpenAI, params: dict):
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return await client.chat.completions.create(**params)
            except Exception as e:  # noqa: BLE001
                last = e
                if not self._transient(e) or attempt >= self.retries:
                    raise LLMError(f"LLM request failed: {e}") from e
                await asyncio.sleep(self.retry_backoff * (attempt + 1))
        raise LLMError(f"LLM request failed: {last}") from last

    async def list_models(self) -> list[dict]:
        # TTL cache: the model catalog is queried from many endpoints on page
        # loads; re-probing the LLM server every time is wasteful.
        now = time.monotonic()
        if self._models_cache is not None and now - self._models_cache_ts < 60.0:
            return list(self._models_cache)
        last = None
        for client in (self._gateway, self._tool):
            try:
                resp = await client.models.list()
                seen = {}
                for m in resp.data:
                    seen.setdefault(m.id, {"id": m.id, "owned_by": getattr(m, "owned_by", "")})
                models = list(seen.values())
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise LLMError(f"Cannot reach LLM server: {last}")
        # Enrich with native Ollama details (parameter size, quantization) so the
        # UI can label models instead of regex-guessing sizes. Best-effort.
        try:
            tags = await asyncio.to_thread(self._native_tags)
            for m in models:
                detail = tags.get(m["id"])
                if detail:
                    if detail.get("parameter_size"):
                        m["size"] = detail["parameter_size"]
                    if detail.get("quantization_level"):
                        m["quantization"] = detail["quantization_level"]
        except Exception:  # noqa: BLE001
            pass
        self._models_cache = models
        self._models_cache_ts = time.monotonic()
        return models

    def _native_tags(self) -> dict[str, dict]:
        """Query the direct Ollama native /api/tags endpoint for model details."""
        base = self.tool_base_url
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = base.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8") or "{}")
        out = {}
        for m in data.get("models") or []:
            d = m.get("details") or {}
            out[m.get("name") or ""] = {
                "parameter_size": d.get("parameter_size") or "",
                "quantization_level": d.get("quantization_level") or "",
            }
        return out

    def _params(self, messages, tools, temperature, model=None):
        params = dict(model=model or self.model, messages=messages,
                      temperature=temperature, max_tokens=self.max_tokens)
        if tools:
            params["tools"] = tools
        return params

    async def complete(self, messages: list[dict], tools: Optional[list] = None,
                       temperature: Optional[float] = None,
                       model: Optional[str] = None) -> dict:
        """Non-streaming completion. Returns full assistant message dict (may contain tool_calls)."""
        temp = temperature if temperature is not None else self.temperature
        client = self._pick(tools)
        resp = await self._run(client, self._params(messages, tools, temp, model))
        return self._msg_to_dict(resp.choices[0].message)

    async def stream(self, messages: list[dict], tools: Optional[list] = None,
                     temperature: Optional[float] = None,
                     on_delta=None,
                     model: Optional[str] = None) -> dict:
        """Stream a completion, invoking on_delta(str) for each token chunk.

        Returns the full assistant message dict (may contain tool_calls for the
        final completion of a streaming tool-calling model).
        """
        temp = temperature if temperature is not None else self.temperature
        client = self._pick(tools)
        params = dict(self._params(messages, tools, temp, model))
        params["stream"] = True
        resp = await self._run(client, params)

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
