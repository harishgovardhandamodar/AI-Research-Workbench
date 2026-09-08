from __future__ import annotations

import httpx

from .base import ChatMessage, ChatResponse, LLMProvider


class LMStudioProvider(LLMProvider):
    """LM Studio exposes OpenAI-compatible /v1/chat/completions."""

    name = "lmstudio"

    def __init__(self, base_url: str, model: str, timeout_s: int = 120):
        self.base_url = base_url.rstrip("/")
        # LM Studio base is typically http://localhost:1234/v1
        self.model = model
        self.timeout_s = timeout_s

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self.base_url}/models")
            r.raise_for_status()
            data = r.json()
            return [m["id"] for m in data.get("data", [])]

    def health(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{self.base_url}/models")
                r.raise_for_status()
                return True, "ok"
        except Exception as e:
            return False, str(e)

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        model = kwargs.get("model", self.model)
        temp = kwargs.get("temperature", 0.3)
        max_tokens = kwargs.get("max_tokens", 4096)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temp,
            "max_tokens": max_tokens,
            "stream": False,
        }
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(f"{self.base_url}/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return ChatResponse(
                content=content,
                model=model,
                provider="lmstudio",
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )

    def chat_stream(self, messages: list[ChatMessage], **kwargs):
        import json

        model = kwargs.get("model", self.model)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", 0.3),
            "max_tokens": kwargs.get("max_tokens", 4096),
            "stream": True,
        }
        with httpx.Client(timeout=self.timeout_s) as c:
            with c.stream("POST", f"{self.base_url}/chat/completions", json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        obj = json.loads(line)
                        delta = obj["choices"][0].get("delta", {}).get("content")
                        if delta:
                            yield delta
                    except Exception:
                        continue
