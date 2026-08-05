from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import requests

from .config import Config
from .gpu import GPUManager

logger = logging.getLogger(__name__)


class LLMInterface:
    def __init__(self, config: Config, gpu_mgr: GPUManager | None = None) -> None:
        self.config = config
        self.gpu_mgr = gpu_mgr
        self.base_url = config.ollama_base_url.rstrip("/")
        self._lock = threading.Lock()

    def _get_base_url(self, gpu_id: int | None = None) -> str:
        if gpu_id is not None and self.gpu_mgr and self.gpu_mgr.device_count() > 0:
            return self.gpu_mgr.get_ollama_url(gpu_id).rstrip("/")
        return self.base_url

    def _request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        retries: int = 3,
        gpu_id: int | None = None,
    ) -> dict[str, Any]:
        base_url = self._get_base_url(gpu_id)
        url = f"{base_url}/api/{endpoint}"
        for attempt in range(retries):
            try:
                resp = requests.post(url, json=payload, timeout=180)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                logger.warning(
                    "Ollama request failed to %s (attempt %d/%d, GPU %s): %s",
                    url, attempt + 1, retries,
                    str(gpu_id) if gpu_id is not None else "default",
                    e,
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(
            f"Ollama request to {endpoint} on GPU {gpu_id} failed after {retries} retries"
        )

    def generate(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        gpu_id: int | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": model or self.config.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None
                else self.config.ollama_temperature,
                "num_predict": max_tokens if max_tokens is not None
                else self.config.ollama_max_tokens,
            },
        }
        if gpu_id is None and self.gpu_mgr:
            gpu_id = self.gpu_mgr.get_next_llm_gpu()
        data = self._request("chat", payload, gpu_id=gpu_id)
        return data.get("message", {}).get("content", "")

    def generate_parallel(
        self,
        prompts: list[str],
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> list[str]:
        results: list[str] = [""] * len(prompts)

        def _run(idx: int, prompt: str) -> None:
            try:
                gpu_id = idx if self.gpu_mgr and self.gpu_mgr.device_count() > 0 else None
                results[idx] = self.generate(
                    prompt,
                    model=model,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    gpu_id=gpu_id,
                )
            except Exception as e:
                logger.error("Parallel generate task %d failed: %s", idx, e)
                results[idx] = ""

        threads = []
        for i, prompt in enumerate(prompts):
            t = threading.Thread(target=_run, args=(i, prompt), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=300)

        return results

    def extract_structured(
        self,
        prompt: str,
        model: str | None = None,
        gpu_id: int | None = None,
    ) -> dict[str, Any]:
        system = (
            "You are a precise information extraction system. "
            "Respond ONLY with valid JSON. No markdown, no explanation."
        )
        text = self.generate(prompt, model=model, system=system, temperature=0.0, gpu_id=gpu_id)
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        repaired = self._repair_json(text)
        if repaired is not None:
            logger.warning("Repaired truncated JSON from LLM (%d chars) keys=%s",
                           len(text), list(repaired.keys()))
            return repaired
        logger.error("Failed to parse JSON from LLM response (len=%d): %s",
                     len(text), text[:500])
        return {}

    @staticmethod
    def _repair_json(text: str) -> dict[str, Any] | None:
        if not text or text[0] != '{':
            return None
        text = text.strip()
        # Trim trailing non-JSON content after the last balanced '}'
        depth = 0
        last_balanced = -1
        in_str = False
        escaped = False
        for i, ch in enumerate(text):
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_balanced = i
        if last_balanced >= 0:
            text = text[:last_balanced + 1]
        text = re.sub(r',(\s*[}\]])', r'\1', text)
        text = re.sub(
            r':\s*(\d[\w.\-+]*[a-zA-Z][\w.\-+]*)\s*([,}\]])',
            r': "\1"\2',
            text,
        )
        text = re.sub(
            r'"(\w+)":\s*\{\s*("[^"]*"\s*(?:,\s*"[^"]*"\s*)*)\s*\}',
            r'"\1": [\2]',
            text,
        )
        # Remove stray quotes between closing brackets/braces (e.g. ]"}) → ]})
        text = re.sub(r'\]"\s*([\]}])', r']\1', text)
        text = re.sub(r'\}"\s*([\]}])', r'}\1', text)
        # Fix spurious "[" as first array element (missing comma before next string)
        text = re.sub(r'(?<=[\[,])\s*"\s*\[\s*"(?=[^,\]"\s])', '"', text)
        stack: list[str] = []
        in_str = False
        escaped = False
        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in '([{':
                stack.append(ch)
            elif ch == ')':
                if stack and stack[-1] == '(':
                    stack.pop()
            elif ch == ']':
                if stack and stack[-1] == '[':
                    stack.pop()
            elif ch == '}':
                if stack and stack[-1] == '{':
                    stack.pop()
        if in_str:
            text += '"'
        close_map = {'{': '}', '[': ']', '(': ')'}
        for ch in reversed(stack):
            text += close_map.get(ch, '}')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fields = {}
        for key in ('summary', 'notes', 'lineage_notes'):
            key_pattern = f'"{key}"'
            start = text.find(key_pattern)
            if start < 0:
                continue
            pos = start + len(key_pattern)
            while pos < len(text) and text[pos] in ' \t\n\r:':
                pos += 1
            if pos >= len(text) or text[pos] != '"':
                continue
            pos += 1
            value_chars: list[str] = []
            while pos < len(text):
                ch = text[pos]
                if ch == '\\':
                    pos += 1
                    if pos < len(text):
                        value_chars.append(text[pos])
                    pos += 1
                    continue
                if ch == '"':
                    break
                value_chars.append(ch)
                pos += 1
            if value_chars:
                fields[key] = ''.join(value_chars)
        if fields:
            return fields
        return None

    def embed(self, text: str, model: str | None = None, gpu_id: int | None = None) -> list[float]:
        payload = {
            "model": model or self.config.ollama_embed_model,
            "input": text,
        }
        if gpu_id is None and self.gpu_mgr:
            gpu_id = self.gpu_mgr.get_next_embed_gpu()
        data = self._request("embed", payload, gpu_id=gpu_id)
        return data.get("embeddings", [data.get("embedding", [])])[0] if isinstance(data.get("embeddings"), list) else data.get("embedding", [])

    def embed_parallel(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        results: list[list[float]] = [[] for _ in texts]

        def _run(idx: int, text: str) -> None:
            try:
                gpu_id = idx % max(self.gpu_mgr.device_count(), 1) if self.gpu_mgr else None
                results[idx] = self.embed(text, model=model, gpu_id=gpu_id)
            except Exception as e:
                logger.error("Parallel embed task %d failed: %s", idx, e)
                results[idx] = []

        threads = []
        for i, text in enumerate(texts):
            t = threading.Thread(target=_run, args=(i, text), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=120)

        return results

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        gpu_id: int | None = None,
    ) -> str:
        base_url = self._get_base_url(gpu_id)
        url = f"{base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": model or self.config.ollama_model,
            "messages": messages,
            "stream": False,
        }
        try:
            resp = requests.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except requests.RequestException as e:
            logger.error("Chat request failed on GPU %s: %s", str(gpu_id), e)
            return ""

    def health_check(self, gpu_id: int | None = None) -> bool:
        try:
            base_url = self._get_base_url(gpu_id)
            r = requests.get(f"{base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False
