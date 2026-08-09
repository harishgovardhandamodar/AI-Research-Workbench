"""Model loading + generation for ft-validate.

``load_models`` validates a base model id and (optionally) a PEFT adapter path,
checks the local backend (Unsloth preferred, transformers + PEFT fallback), and
registers "model handles" (ids) to pass to ``run_rag_verification``. Actual
weights are loaded inside the verification *subprocess* so the MCP server stays
responsive and the 120 s tool-call timeout is never hit.

When no heavy backend (torch/unsloth/transformers) is installed — e.g. on a
CPU-only machine or in CI — verification falls back to a deterministic
"evidence-based" answerer: each question is answered from the retrieved RAG
chunks. That keeps the whole validation pipeline runnable and testable offline.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from .models import UnslothConfig
from .store import ValidateStore

ModelSpec = dict  # {"model_id": ..., "kind": "base"|"adapter", ...}


def _available(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def backend_status() -> dict:
    """Detect which inference backends are importable in this environment."""
    return {
        "unsloth": _available("unsloth"),
        "torch": _available("torch"),
        "transformers": _available("transformers"),
        "peft": _available("peft"),
        "sentence_transformers": _available("sentence_transformers"),
        "ollama": _ollama_present(),
    }


def _ollama_present() -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(
            os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/api/tags",
            method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return bool(json.loads(resp.read().decode()).get("models"))
    except Exception:  # noqa: BLE001
        return False


def _safe_adapter_path(path: str) -> str:
    """Validate an adapter path exists and resolve it (traversal-safe)."""
    if not path or not path.strip():
        raise ValueError("adapter_path is required (or pass None to compare "
                         "base against itself)")
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"adapter path does not exist: {path} — point at the "
                         "adapter dir produced by dk-lora (export_adapter)")
    return str(p)


def load_models(store: ValidateStore, base_model: str,
                adapter_path: str | None = None,
                unsloth_config: dict | None = None,
                model_id: str = "") -> dict:
    """Validate a base model + optional adapter and register model handles.

    Returns ``model_ids`` to pass to ``run_rag_verification`` (``["base"]`` or
    ``["base", "adapter"]``) plus backend availability and a config snapshot.
    """
    if not base_model or not base_model.strip():
        raise ValueError("base_model is required (HuggingFace id, e.g. "
                         "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit)")
    cfg = UnslothConfig(**unsloth_config) if unsloth_config else UnslothConfig()
    backend = backend_status()
    models: list[ModelSpec] = [
        {"model_id": model_id or "base", "kind": "base",
         "path": base_model, "config": cfg.model_dump()},
    ]
    if adapter_path:
        safe = _safe_adapter_path(adapter_path)
        models.append({"model_id": "adapter", "kind": "adapter",
                       "path": safe, "base": base_model, "config": cfg.model_dump()})

    # Persist a reusable model registration so the subprocess can re-read it.
    reg_id = model_id or "models"
    store.rag_dir.parent.mkdir(parents=True, exist_ok=True)
    import json as _j
    reg_path = store.runs_dir.parent / "models" / f"{reg_id}.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(_j.dumps({"model_ids": [m["model_id"] for m in models],
                                  "models": models, "base_model": base_model,
                                  "adapter_path": adapter_path}), encoding="utf-8")

    # Verify the adapter actually looks like a PEFT adapter.
    adapter_note = ""
    if adapter_path:
        safe = _safe_adapter_path(adapter_path)
        has_config = (Path(safe) / "adapter_config.json").exists()
        has_weights = any((Path(safe) / f).exists()
                          for f in ("adapter_model.bin", "adapter_model.safetensors"))
        adapter_note = (f"adapter_ok={has_config and has_weights} "
                        f"(config={has_config}, weights={has_weights})")

    return {
        "model_ids": [m["model_id"] for m in models],
        "backend": backend,
        "base_model": base_model,
        "adapter_path": adapter_path,
        "adapter_note": adapter_note,
        "fallback": (not backend["unsloth"] and not backend["transformers"]
                     and not backend["ollama"]),
        "generation": "evidence-based" if (
            not backend["unsloth"] and not backend["transformers"] and not backend["ollama"])
        else "local-model",
    }


def generation_mode(store: ValidateStore, base_model: str,
                    adapter_path: str | None = None) -> str:
    """Decide how the verification job will generate answers."""
    backend = backend_status()
    if backend["unsloth"] or backend["transformers"]:
        return "hf"
    if backend["ollama"]:
        return "ollama"
    return "evidence"
