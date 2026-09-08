from __future__ import annotations

import json

import httpx

from .base import ChatMessage, ChatResponse, LLMProvider


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_s: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]

    def health(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                return True, "ok"
        except Exception as e:
            return False, str(e)

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        model = kwargs.get("model", self.model)
        temp = kwargs.get("temperature", 0.3)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temp},
        }
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            content = data.get("message", {}).get("content", "") or data.get("response", "")
            return ChatResponse(content=content, model=model, provider="ollama")

    def chat_stream(self, messages: list[ChatMessage], **kwargs):
        model = kwargs.get("model", self.model)
        temp = kwargs.get("temperature", 0.3)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "options": {"temperature": temp},
        }
        with httpx.Client(timeout=self.timeout_s) as c:
            with c.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        chunk = obj.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk
                        if obj.get("done"):
                            break
                    except Exception:
                        continue
