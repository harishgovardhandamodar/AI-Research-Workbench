"""Disk-backed store for ft-validate.

Layout:

    <workspace>/
      rag/<index_id>/chunks.json        source chunks
      rag/<index_id>/vectors.npy        embeddings (optional; hashing fallback)
      rag/<index_id>/meta.json          embedding model, count, created
      evalsets/<id>.json                EvalSet
      runs/<id>.json                    ValidationRun
      reports/<id>.md                   generated Markdown report

Shared across separate MCP server processes by reference (like the EDA and
dk-lora stores).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import DEFAULT_WORKSPACE, FT_VALIDATE_WORKSPACE_ENV
from .models import EvalSet, ValidationRun


def _atomic_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


class ValidateStore:
    """One ft-validate workspace."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get(FT_VALIDATE_WORKSPACE_ENV,
                                                DEFAULT_WORKSPACE)).expanduser()
        self.rag_dir = self.root / "rag"
        self.evalsets_dir = self.root / "evalsets"
        self.runs_dir = self.root / "runs"
        self.reports_dir = self.root / "reports"
        for d in (self.rag_dir, self.evalsets_dir, self.runs_dir,
                  self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- rag ----
    def save_rag_index(self, index_id: str, chunks: list[dict],
                       vectors: Any, embedding_model: str) -> dict:
        meta = {"index_id": index_id, "embedding_model": embedding_model,
                "chunk_count": len(chunks), "created_at": time.time()}
        _atomic_json(chunks, self.rag_dir / index_id / "chunks.json")
        if vectors is not None:
            import numpy as np
            np.save(self.rag_dir / index_id / "vectors.npy", vectors)
            meta["vector_dim"] = int(vectors.shape[1])
        else:
            meta["vector_dim"] = 0
        _atomic_json(meta, self.rag_dir / index_id / "meta.json")
        return meta

    def list_rag_indexes(self) -> list[dict]:
        out = []
        for d in sorted(self.rag_dir.iterdir()):
            if d.is_dir():
                meta = _read_json(d / "meta.json")
                if meta:
                    out.append(meta)
        return out

    def get_rag_index(self, index_id: str) -> tuple[list[dict], Any, dict] | None:
        """Return (chunks, vectors_or_None, meta)."""
        meta = _read_json(self.rag_dir / index_id / "meta.json")
        if meta is None:
            return None
        chunks = _read_json(self.rag_dir / index_id / "chunks.json") or []
        vectors = None
        vpath = self.rag_dir / index_id / "vectors.npy"
        if vpath.exists() and meta.get("vector_dim"):
            import numpy as np
            vectors = np.load(vpath)
        return chunks, vectors, meta

    # -------------------------------------------------------- evalsets ----
    def save_evalset(self, es: EvalSet) -> None:
        _atomic_json(es.model_dump(), self.evalsets_dir / f"{es.id}.json")

    def get_evalset(self, eval_set_id: str) -> EvalSet | None:
        raw = _read_json(self.evalsets_dir / f"{eval_set_id}.json")
        return EvalSet(**raw) if raw else None

    def list_evalsets(self) -> list[dict]:
        out = []
        for p in sorted(self.evalsets_dir.glob("*.json")):
            raw = _read_json(p)
            if raw:
                out.append({"id": raw["id"], "mode": raw["mode"],
                            "count": len(raw.get("questions", [])),
                            "created_at": raw.get("created_at", 0.0)})
        return out

    # ------------------------------------------------------------- runs ----
    def save_run(self, run: ValidationRun) -> None:
        _atomic_json(run.model_dump(), self.runs_dir / f"{run.id}.json")

    def update_run(self, run_id: str, **fields: Any) -> ValidationRun:
        raw = _read_json(self.runs_dir / f"{run_id}.json") or {}
        raw.update(fields)
        raw["updated_at"] = time.time()
        _atomic_json(raw, self.runs_dir / f"{run_id}.json")
        return ValidationRun(**raw)

    def get_run(self, run_id: str) -> ValidationRun | None:
        raw = _read_json(self.runs_dir / f"{run_id}.json")
        return ValidationRun(**raw) if raw else None

    def list_runs(self) -> list[dict]:
        out = []
        for p in sorted(self.runs_dir.glob("*.json")):
            raw = _read_json(p)
            if raw:
                out.append({"id": raw["id"], "eval_set_id": raw["eval_set_id"],
                            "status": raw["status"], "base_model": raw.get("base_model", ""),
                            "adapter_path": raw.get("adapter_path", ""),
                            "created_at": raw.get("created_at", 0.0),
                            "updated_at": raw.get("updated_at", 0.0)})
        return sorted(out, key=lambda r: -r["created_at"])
