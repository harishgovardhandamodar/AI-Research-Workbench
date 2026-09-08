"""NVIDIA NIM / TensorRT-LLM provider — OpenAI-compatible.

NIM typically at http://localhost:8000/v1 or :8001/v1
Also probes `nvidia-smi` for GPU inventory.
"""

from __future__ import annotations

import subprocess
import httpx

from .base import ChatMessage, ChatResponse, LLMProvider

DEFAULT_NIM_URLS = ["http://localhost:8011/v1", "http://localhost:8001/v1", "http://localhost:8000/v1"]  # 8011 first to avoid 8000/8001 conflicts

class NvidiaProvider(LLMProvider):
    name = "nvidia"

    def __init__(self, base_url: str = "http://localhost:8000/v1", model: str = "meta/llama3-8b-instruct", timeout_s: int = 120):
        self.base_url = base_url.rstrip("/")
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
                # also check nvidia-smi if available
                try:
                    out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
                    gpu = out.stdout.strip().splitlines()[0] if out.stdout else "gpu ok"
                except Exception:
                    gpu = "nvidia-smi not found"
                return True, f"ok ({gpu})"
        except Exception as e:
            msg = str(e)
            if "404" in msg or "Not Found" in msg:
                return False, "not running (optional — no GPU/NIM, use Ollama/LM Studio)"
            if "nvidia-smi" in msg or "Connection" in msg or "ConnectError" in msg:
                return False, "not running (optional — macOS has no Nvidia GPU)"
            return False, msg

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
            return ChatResponse(content=content, model=model, provider="nvidia",
                                prompt_tokens=usage.get("prompt_tokens"),
                                completion_tokens=usage.get("completion_tokens"))

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

def discover_nvidia_endpoints() -> list[tuple[str, list[str]]]:
    found = []
    for url in DEFAULT_NIM_URLS:
        try:
            p = NvidiaProvider(url)
            ok, _ = p.health()
            if ok:
                try:
                    models = p.list_models()
                except Exception:
                    models = []
                found.append((url, models))
        except Exception:
            continue
    return found

def nvidia_smi_info() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as e:
        return f"nvidia-smi unavailable: {e}"
    return "nvidia-smi no output"
