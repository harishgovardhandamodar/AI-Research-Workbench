"""Config: ~/.hive/config.toml + env overrides. No cloud keys required."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

try:
    import tomllib  # py 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w  # for writing
except ImportError:
    tomli_w = None  # type: ignore

Provider = Literal["ollama", "lmstudio", "nvidia", "auto"]

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_LMSTUDIO_URL = "http://localhost:1234/v1"
DEFAULT_NVIDIA_URL = "http://localhost:8011/v1"  # 8011 to avoid 8000/8001 conflicts (hive/ijcb, vcom-tunnel)

CONFIG_DIR = Path.home() / ".hive"
CONFIG_FILE = CONFIG_DIR / "config.toml"
DB_FILE = CONFIG_DIR / "hive.db"
# legacy experiments (personal-experiments) — local cache for dashboard
# priority: HIVE_EXPERIMENTS_DIR env > /personal-experiments (docker) > ~/codebase/personal-experiments
_candidates = []
if os.environ.get("HIVE_EXPERIMENTS_DIR"):
    _candidates.append(Path(os.environ["HIVE_EXPERIMENTS_DIR"]))
_candidates.append(Path("/personal-experiments"))
_candidates.append(Path.home() / "codebase" / "personal-experiments")
EXPERIMENTS_DIR = next((p for p in _candidates if p.exists()), None)


@dataclass
class LLMConfig:
    provider: Provider = "auto"
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = "llama3.1:8b"
    lmstudio_url: str = DEFAULT_LMSTUDIO_URL
    lmstudio_model: str = "qwen/qwen3.8-27b"
    nvidia_url: str = DEFAULT_NVIDIA_URL
    nvidia_model: str = "meta/llama3-8b-instruct"
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_s: int = 120


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    default_top_k: int = 10
    full_text_top: int = 3
    # no cloud API keys by design; web paper APIs are public


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config() -> AppConfig:
    data = _load_toml(CONFIG_FILE)
    llm_data = data.get("llm", {})

    # env overrides
    def env(k: str, default: str | None = None) -> str | None:
        return os.environ.get(k, default)

    cfg = AppConfig()
    cfg.llm.provider = llm_data.get("provider", env("HIVE_PROVIDER", cfg.llm.provider))  # type: ignore
    cfg.llm.ollama_url = llm_data.get("ollama_url", env("OLLAMA_BASE_URL", env("OLLAMA_URL", cfg.llm.ollama_url)))
    cfg.llm.ollama_model = llm_data.get("ollama_model", env("HIVE_OLLAMA_MODEL", cfg.llm.ollama_model))
    cfg.llm.lmstudio_url = llm_data.get("lmstudio_url", env("LMSTUDIO_BASE_URL", env("LMSTUDIO_URL", cfg.llm.lmstudio_url)))
    cfg.llm.lmstudio_model = llm_data.get("lmstudio_model", env("HIVE_LMSTUDIO_MODEL", cfg.llm.lmstudio_model))
    cfg.llm.nvidia_url = llm_data.get("nvidia_url", env("NVIDIA_BASE_URL", env("NVIDIA_URL", cfg.llm.nvidia_url)))
    cfg.llm.nvidia_model = llm_data.get("nvidia_model", env("HIVE_NVIDIA_MODEL", env("NVIDIA_MODEL", cfg.llm.nvidia_model)))
    cfg.llm.temperature = float(llm_data.get("temperature", env("HIVE_TEMPERATURE", cfg.llm.temperature)))
    cfg.llm.max_tokens = int(llm_data.get("max_tokens", env("HIVE_MAX_TOKENS", cfg.llm.max_tokens)))
    cfg.default_top_k = int(data.get("default_top_k", env("HIVE_TOP_K", cfg.default_top_k)))
    cfg.full_text_top = int(data.get("full_text_top", env("HIVE_FULLTEXT_TOP", cfg.full_text_top)))
    return cfg


def save_config(cfg: AppConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "default_top_k": cfg.default_top_k,
        "full_text_top": cfg.full_text_top,
        "llm": {
            "provider": cfg.llm.provider,
            "ollama_url": cfg.llm.ollama_url,
            "ollama_model": cfg.llm.ollama_model,
            "lmstudio_url": cfg.llm.lmstudio_url,
            "lmstudio_model": cfg.llm.lmstudio_model,
            "nvidia_url": cfg.llm.nvidia_url,
            "nvidia_model": cfg.llm.nvidia_model,
            "temperature": cfg.llm.temperature,
            "max_tokens": cfg.llm.max_tokens,
        },
    }
    if tomli_w is None:
        print("tomli-w not installed, cannot save config", file=sys.stderr)
        return
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(data, f)


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
