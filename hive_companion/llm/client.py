from __future__ import annotations

from hive.config import AppConfig
from .base import ChatMessage, ChatResponse
from .lmstudio import LMStudioProvider
from .ollama import OllamaProvider
from .nvidia import NvidiaProvider


def get_provider(cfg: AppConfig):
    """Auto-detect: Ollama -> LM Studio -> Nvidia NIM, else fallback."""
    llm = cfg.llm
    if llm.provider == "ollama":
        return OllamaProvider(llm.ollama_url, llm.ollama_model, llm.timeout_s)
    if llm.provider == "lmstudio":
        return LMStudioProvider(llm.lmstudio_url, llm.lmstudio_model, llm.timeout_s)
    if llm.provider == "nvidia":
        return NvidiaProvider(llm.nvidia_url, llm.nvidia_model, llm.timeout_s)

    # auto — local-first, prefers fastest healthy
    oll = OllamaProvider(llm.ollama_url, llm.ollama_model, llm.timeout_s)
    ok, _ = oll.health()
    if ok:
        return oll
    lms = LMStudioProvider(llm.lmstudio_url, llm.lmstudio_model, llm.timeout_s)
    ok2, _ = lms.health()
    if ok2:
        return lms
    nvd = NvidiaProvider(llm.nvidia_url, llm.nvidia_model, llm.timeout_s)
    ok3, _ = nvd.health()
    if ok3:
        return nvd
    return oll


def _fallback_model(prov, requested: str) -> str | None:
    try:
        models = prov.list_models()
        if not models:
            return None
        if requested in models:
            return requested
        base = requested.split(":")[0]
        for m in models:
            if m.startswith(base):
                return m
        return models[0]
    except Exception:
        return None


def chat(cfg: AppConfig, messages: list[ChatMessage], **kwargs) -> ChatResponse:
    prov = get_provider(cfg)
    temp = kwargs.pop("temperature", cfg.llm.temperature)
    max_tokens = kwargs.pop("max_tokens", cfg.llm.max_tokens)
    requested = kwargs.get("model", prov.model)
    try:
        return prov.chat(messages, temperature=temp, max_tokens=max_tokens, **kwargs)
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            fb = _fallback_model(prov, requested)
            if fb and fb != requested:
                kwargs["model"] = fb
                return prov.chat(messages, temperature=temp, max_tokens=max_tokens, **kwargs)
        raise


def chat_stream(cfg: AppConfig, messages: list[ChatMessage], **kwargs):
    prov = get_provider(cfg)
    temp = kwargs.pop("temperature", cfg.llm.temperature)
    max_tokens = kwargs.pop("max_tokens", cfg.llm.max_tokens)
    requested = kwargs.get("model", prov.model)
    try:
        yield from prov.chat_stream(messages, temperature=temp, max_tokens=max_tokens, **kwargs)
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            fb = _fallback_model(prov, requested)
            if fb and fb != requested:
                kwargs["model"] = fb
                yield from prov.chat_stream(messages, temperature=temp, max_tokens=max_tokens, **kwargs)
                return
        raise
