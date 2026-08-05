from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ..paths import WORKBENCH_DIR

DEFAULT_CONFIG_PATH = Path("config.yaml")

# Default runtime data root: the workbench data dir (gitignored), so no
# ingested papers / graphs are committed to the repo.
_DEFAULT_DATA_ROOT = WORKBENCH_DIR / "research_knowledge_graphs"


def _workbench_ollama() -> dict[str, str]:
    """Fall back to the workbench's own LLM wiring (CONFIG) so the vendored
    app reaches Ollama in the same way as the rest of the workbench — e.g.
    inside the Docker image where Ollama is reachable via the relay at
    host.docker.internal:11435, not localhost:11434."""
    try:
        from ..state import CONFIG

        llm = CONFIG.get("llm", {})
        base = str(llm.get("tool_base_url", "")).rstrip("/")
        # The vendored LLM client speaks the native Ollama API (/api/chat,
        # /api/embed), not the OpenAI-compatible /v1 path.
        if base.endswith("/v1"):
            base = base[:-3]
        return {"base_url": base, "model": str(llm.get("model", ""))}
    except Exception:
        return {}


class Config:
    def __init__(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        self.data: dict[str, Any] = {}
        if Path(path).exists():
            with open(path) as f:
                self.data = yaml.safe_load(f) or {}
        if not self.data.get("directories", {}).get("root"):
            self.data.setdefault("directories", {})["root"] = str(_DEFAULT_DATA_ROOT)

    def _get(self, *keys: str, default: Any = None) -> Any:
        d = self.data
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k, {})
            else:
                return default
        return d if d != {} else default

    def _dir(self, key: str) -> Path | None:
        raw = self._get("directories", key, default="")
        return Path(raw) if raw else None

    @property
    def root_dir(self) -> Path:
        root = Path(self._get("directories", "root", default=str(_DEFAULT_DATA_ROOT)))
        return root

    @property
    def papers_dir(self) -> Path:
        return self._dir("papers") or self.root_dir / "papers"

    @property
    def graph_dir(self) -> Path:
        return self._dir("graph") or self.root_dir / "graph"

    @property
    def vault_dir(self) -> Path:
        return self._dir("vault") or self.root_dir / "vault"

    @property
    def arxiv_max_results(self) -> int:
        return int(self._get("arxiv", "max_results", default=10))

    @property
    def arxiv_download_pdf(self) -> bool:
        return bool(self._get("arxiv", "download_pdf", default=True))

    @property
    def ollama_base_url(self) -> str:
        env = os.environ.get("OLLAMA_BASE_URL")
        if env:
            return env
        yml = self._get("ollama", "base_url", default="")
        if yml:
            return str(yml)
        return _workbench_ollama().get("base_url") or "http://localhost:11434"

    @property
    def ollama_model(self) -> str:
        env = os.environ.get("OLLAMA_MODEL")
        if env:
            return env
        yml = self._get("ollama", "model", default="")
        if yml:
            return str(yml)
        return _workbench_ollama().get("model") or "llama3.2:3b"

    @property
    def ollama_fast_model(self) -> str:
        return os.environ.get("OLLAMA_FAST_MODEL") or str(
            self._get("ollama", "fast_model", default=self.ollama_model)
        )

    @property
    def ollama_embed_model(self) -> str:
        return os.environ.get("OLLAMA_EMBED_MODEL") or str(
            self._get("ollama", "embed_model", default="nomic-embed-text")
        )

    def resolve_model(self, model: str | None) -> str | None:
        if not model or model == "large":
            return self.ollama_model
        if model == "fast":
            return self.ollama_fast_model
        return model

    @property
    def ollama_max_tokens(self) -> int:
        return int(self._get("ollama", "max_tokens", default=8192))

    @property
    def ollama_temperature(self) -> float:
        return float(self._get("ollama", "temperature", default=0.1))

    @property
    def graph_similarity_threshold(self) -> float:
        return float(self._get("graph", "similarity_threshold", default=0.85))

    @property
    def rag_chunk_size(self) -> int:
        return int(self._get("rag", "chunk_size", default=512))

    @property
    def rag_chunk_overlap(self) -> int:
        return int(self._get("rag", "chunk_overlap", default=64))

    @property
    def rag_top_k(self) -> int:
        return int(self._get("rag", "top_k", default=5))

    @property
    def server_host(self) -> str:
        return str(self._get("server", "host", default="127.0.0.1"))

    @property
    def server_port(self) -> int:
        return int(self._get("server", "port", default=7777))

    @property
    def schedule_enabled(self) -> bool:
        return bool(self._get("schedule", "enabled", default=False))

    @property
    def schedule_check_minutes(self) -> int:
        return int(self._get("schedule", "check_minutes", default=60))

    @property
    def schedule_synthesize(self) -> bool:
        """Whether a scheduled build also runs synthesis (vs. only refreshing
        the corpus)."""
        return bool(self._get("schedule", "synthesize", default=True))

    @property
    def gpu_enabled(self) -> bool:
        # Default OFF in the workbench integration: the app talks to the
        # workbench's own Ollama (host relay), not self-launched per-GPU
        # instances. GPU monitoring still works regardless.
        return bool(self._get("gpu", "enabled", default=False))

    @property
    def gpu_device_count(self) -> int:
        return int(self._get("gpu", "device_count", default=2))

    @property
    def gpu_memory_fraction(self) -> float:
        return float(self._get("gpu", "memory_fraction", default=0.95))

    @property
    def gpu_parallel_papers(self) -> int:
        return int(self._get("gpu", "parallel_papers", default=2))

    def gpu_ollama_instance(self, gpu_id: int) -> dict[str, Any]:
        key = f"gpu_{gpu_id}"
        return dict(
            self._get("gpu", "ollama_instances", key, default={})
        )

    @property
    def gpu_embedding_device(self) -> int:
        return int(self._get("gpu", "embedding_device", default=0))

    @property
    def gpu_llm_device(self) -> int:
        return int(self._get("gpu", "llm_device", default=1))
