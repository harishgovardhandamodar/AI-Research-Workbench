"""Nvidia-PAIR — local-first routing over all available models.

PAIR = Provider-Aware Intelligent Router
Discovers Ollama + LM Studio + NVIDIA NIM endpoints and all models,
then routes per-step or ensemble.

Local-first: prefers fastest local, falls back, never egresses.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List

from hive.config import AppConfig, load_config
from hive.llm.ollama import OllamaProvider
from hive.llm.lmstudio import LMStudioProvider
from hive.llm.nvidia import NvidiaProvider, discover_nvidia_endpoints, nvidia_smi_info

@dataclass
class ModelInfo:
    provider: str  # ollama | lmstudio | nvidia
    model: str
    url: str
    healthy: bool
    extra: str = ""

def discover_all(cfg: AppConfig | None = None) -> List[ModelInfo]:
    cfg = cfg or load_config()
    out: List[ModelInfo] = []
    # Ollama
    try:
        p = OllamaProvider(cfg.llm.ollama_url, cfg.llm.ollama_model)
        ok, msg = p.health()
        models = p.list_models() if ok else []
        if models:
            for m in models:
                out.append(ModelInfo("ollama", m, cfg.llm.ollama_url, True, msg))
        else:
            out.append(ModelInfo("ollama", cfg.llm.ollama_model, cfg.llm.ollama_url, ok, msg))
    except Exception as e:
        out.append(ModelInfo("ollama", cfg.llm.ollama_model, cfg.llm.ollama_url, False, str(e)))
    # LM Studio
    try:
        p = LMStudioProvider(cfg.llm.lmstudio_url, cfg.llm.lmstudio_model)
        ok, msg = p.health()
        models = p.list_models() if ok else []
        if models:
            for m in models:
                out.append(ModelInfo("lmstudio", m, cfg.llm.lmstudio_url, True, msg))
        else:
            out.append(ModelInfo("lmstudio", cfg.llm.lmstudio_model, cfg.llm.lmstudio_url, ok, msg))
    except Exception as e:
        out.append(ModelInfo("lmstudio", cfg.llm.lmstudio_model, cfg.llm.lmstudio_url, False, str(e)))
    # Nvidia NIM
    for url, models in discover_nvidia_endpoints():
        if models:
            for m in models:
                out.append(ModelInfo("nvidia", m, url, True, nvidia_smi_info()[:80]))
        else:
            out.append(ModelInfo("nvidia", "meta/llama3-8b-instruct", url, True, "nim ok"))
    if not any(m.provider == "nvidia" for m in out):
        # still report nvidia-smi even if no NIM
        out.append(ModelInfo("nvidia", "nvidia-smi", "nvidia-smi", False, nvidia_smi_info()[:120]))
    return out

def pick_best(cfg: AppConfig | None = None, prefer: str = "fastest") -> ModelInfo | None:
    """Pick best local model. prefer: fastest | largest | balanced."""
    models = discover_all(cfg)
    healthy = [m for m in models if m.healthy]
    if not healthy:
        return None
    # fastest: smallest ollama model first (1.5b, 3b, 8b)
    if prefer == "fastest":
        # sort by model size hint in name
        def speed_key(m: ModelInfo):
            name = m.model.lower()
            if "1.5b" in name or "1b" in name:
                return 0
            if "3b" in name:
                return 1
            if "7b" in name or "8b" in name:
                return 2
            if "13b" in name or "14b" in name:
                return 3
            if "27b" in name or "30b" in name:
                return 4
            if "70b" in name or "120b" in name:
                return 5
            return 3
        return sorted(healthy, key=speed_key)[0]
    if prefer == "largest":
        return sorted(healthy, key=lambda m: m.model)[-1]
    # balanced: prefer ollama 8b or lmstudio qwen
    for m in healthy:
        if "llama3.1:8b" in m.model:
            return m
    return healthy[0]

def get_provider_for(model_info: ModelInfo):
    if model_info.provider == "ollama":
        return OllamaProvider(model_info.url, model_info.model)
    if model_info.provider == "lmstudio":
        return LMStudioProvider(model_info.url, model_info.model)
    return NvidiaProvider(model_info.url, model_info.model)
