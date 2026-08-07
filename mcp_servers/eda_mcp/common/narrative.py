"""Local-LLM narrative for EDA reports.

The EDA report's Executive Summary and Recommendations sections can be written
by a model. Only **local** models are ever contacted: the OpenAI-compatible
endpoint defaults to the workbench's local Ollama tool endpoint
(``FOX_TOOL_BASE_URL``, default ``http://127.0.0.1:11434/v1``) and the model
comes from ``FOX_MODEL``. If no endpoint/model is configured or the call fails,
callers fall back to rule-based text, so report generation always works offline.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = os.environ.get(
    "FOX_TOOL_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("FOX_MODEL", "")


def local_llm_available() -> bool:
    return bool(DEFAULT_MODEL)


def local_chat(system: str, user: str, max_tokens: int = 800,
               temperature: float = 0.2) -> str | None:
    """Ask a local model for narrative text. Returns None if unavailable/failed."""
    model = DEFAULT_MODEL
    if not model:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{DEFAULT_BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        return content.strip() if content else None
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
        return None


def _narrative(system: str, user: str, max_tokens: int = 800) -> str | None:
    return local_chat(system, user, max_tokens=max_tokens)


def write_summary(facts: dict[str, Any]) -> str | None:
    """Executive-summary paragraph from a local model (or None to fall back)."""
    if not _narrative_enabled(facts):
        return None
    prompt = (
        "You are a data scientist. Write a concise 3-6 sentence executive "
        "summary of this exploratory data analysis. Use plain prose; cite "
        "specific numbers. Data facts (JSON):\n"
        + json.dumps(facts, default=str)[:6000]
    )
    return _narrative("You write crisp executive summaries for EDA reports.", prompt)


def write_recommendations(overview: str) -> str | None:
    """Recommendations (cleaning, feature engineering, modeling) from a local model."""
    prompt = (
        "Based on the EDA overview below, list 4-7 concrete, actionable "
        "recommendations covering data cleaning, feature engineering and "
        "modeling. Use bullet points.\n\nEDA overview:\n"
        + overview[:6000]
    )
    return _narrative("You are an expert data scientist writing recommendations.",
                      prompt)


def _narrative_enabled(facts: dict[str, Any]) -> bool:
    # Only use the LLM when explicitly allowed AND a local model is configured.
    return bool(facts.get("use_llm")) and local_llm_available()
