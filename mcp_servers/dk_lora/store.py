"""Disk-backed catalog for dk-lora.

Keeps artifacts, chunks, datasets, configs and jobs in a workspace directory:

    <workspace>/
      index.json                    dataset_id -> {path, meta, created}
      artifacts/<id>.json           normalized document text + metadata
      chunks/<id>.json              chunk text + provenance
      datasets/<id>.jsonl           training examples (Alpaca / pretrain)
      datasets/<id>.meta.json       mode + quality stats
      configs/<id>.json             validated TrainingConfig
      jobs/<id>.json                job state + result

Everything is local, JSON on disk, atomic writes, keyed by id so separate MCP
server processes can share the same workspace by reference (mirrors the EDA
MCP's DatasetStore design).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import DK_LORA_WORKSPACE_ENV, DEFAULT_WORKSPACE
from .models import Artifact, Chunk, Dataset, DatasetExample, JobRecord, TrainingConfig


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
    except Exception:  # noqa: BLE001 - corrupt file treated as missing
        return None


class Workspace:
    """One dk-lora workspace: the id-keyed catalog + artifact storage."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get(DK_LORA_WORKSPACE_ENV,
                                                DEFAULT_WORKSPACE)).expanduser()
        self.artifacts_dir = self.root / "artifacts"
        self.chunks_dir = self.root / "chunks"
        self.datasets_dir = self.root / "datasets"
        self.configs_dir = self.root / "configs"
        self.jobs_dir = self.root / "jobs"
        self.adapters_dir = self.root / "adapters"
        for d in (self.artifacts_dir, self.chunks_dir, self.datasets_dir,
                  self.configs_dir, self.jobs_dir, self.adapters_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- index ----
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _read_index(self) -> dict:
        return _read_json(self._index_path()) or {}

    def _write_index(self, index: dict) -> None:
        _atomic_json(index, self._index_path())

    def _touch_index(self, kind: str, item_id: str, label: str) -> None:
        idx = self._read_index()
        idx.setdefault(kind, {})[item_id] = {
            "label": label, "created_at": time.time(),
        }
        self._write_index(idx)

    def list_entries(self, kind: str) -> list[dict]:
        idx = self._read_index()
        return [{"id": k, **{kk: vv for kk, vv in v.items()}}
                for k, v in idx.get(kind, {}).items()]

    # ---------------------------------------------------------- artifacts ----
    def add_artifact(self, art: Artifact) -> None:
        _atomic_json(art.model_dump(), self.artifacts_dir / f"{art.id}.json")
        self._touch_index("artifacts", art.id, art.title or art.path)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        raw = _read_json(self.artifacts_dir / f"{artifact_id}.json")
        return Artifact(**raw) if raw else None

    def list_artifacts(self, filter_: str = "") -> list[dict]:
        out = [self.get_artifact(k) for k in self.list_entries("artifacts")]
        out = [a for a in out if a is not None]
        if filter_:
            f = filter_.lower()
            out = [a for a in out
                   if f in a.title.lower() or f in a.path.lower()
                   or f in a.file_type.lower()]
        return [{"id": a.id, "path": a.path, "file_type": a.file_type,
                 "title": a.title, "size_bytes": a.size_bytes,
                 "created_at": a.created_at,
                 "metadata": {k: v for k, v in a.metadata.items()
                              if k not in ("text",)}} for a in out]

    # ------------------------------------------------------------- chunks ----
    def add_chunks(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            _atomic_json(c.model_dump(), self.chunks_dir / f"{c.id}.json")
        self._touch_index("chunks", chunks[0].artifact_id,
                          chunks[0].source_path)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        raw = _read_json(self.chunks_dir / f"{chunk_id}.json")
        return Chunk(**raw) if raw else None

    def list_chunks(self, artifact_ids: list[str] | None = None) -> list[Chunk]:
        entries = self._read_index().get("chunks", {})
        out: list[Chunk] = []
        for aid in entries:
            if artifact_ids and aid not in artifact_ids:
                continue
            for path in self.chunks_dir.glob(f"{aid}--*.json"):
                raw = _read_json(path)
                if raw:
                    out.append(Chunk(**raw))
        return sorted(out, key=lambda c: (c.artifact_id, c.index))

    # ----------------------------------------------------------- datasets ----
    def save_dataset(self, ds: Dataset) -> None:
        with open(self.datasets_dir / f"{ds.id}.jsonl", "w", encoding="utf-8") as f:
            for ex in ds.examples:
                f.write(json.dumps(ex.model_dump(), ensure_ascii=False, default=str) + "\n")
        _atomic_json({"mode": ds.mode, "quality": ds.quality,
                      "created_at": ds.created_at, "count": len(ds.examples)},
                     self.datasets_dir / f"{ds.id}.meta.json")
        self._touch_index("datasets", ds.id, ds.id)

    def get_dataset(self, dataset_id: str) -> Dataset | None:
        path = self.datasets_dir / f"{dataset_id}.jsonl"
        meta = _read_json(self.datasets_dir / f"{dataset_id}.meta.json")
        if not path.exists():
            return None
        examples: list[DatasetExample] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    examples.append(DatasetExample(**json.loads(line)))
        mode = (meta or {}).get("mode", "mixed")
        return Dataset(id=dataset_id, mode=mode, examples=examples,
                       quality=(meta or {}).get("quality", 0.0),
                       created_at=(meta or {}).get("created_at", 0.0))

    def list_datasets(self) -> list[dict]:
        out = []
        for d in self.list_entries("datasets"):
            ds = self.get_dataset(d["id"])
            out.append({"id": d["id"], "mode": ds.mode if ds else "?",
                        "count": len(ds.examples) if ds else 0,
                        "quality": ds.quality if ds else 0.0,
                        "created_at": d.get("created_at", 0.0)})
        return out

    # ------------------------------------------------------------ configs ----
    def save_config(self, cfg: TrainingConfig) -> None:
        _atomic_json(cfg.model_dump(), self.configs_dir / f"{cfg.id}.json")
        self._touch_index("configs", cfg.id, cfg.id)

    def get_config(self, config_id: str) -> TrainingConfig | None:
        raw = _read_json(self.configs_dir / f"{config_id}.json")
        return TrainingConfig(**raw) if raw else None

    # --------------------------------------------------------------- jobs ----
    def add_job(self, job: JobRecord) -> None:
        _atomic_json(job.model_dump(), self.jobs_dir / f"{job.id}.json")
        self._touch_index("jobs", job.id, job.kind)

    def update_job(self, job_id: str, **fields: Any) -> JobRecord:
        path = self.jobs_dir / f"{job_id}.json"
        raw = _read_json(path) or {}
        raw.update(fields)
        raw["updated_at"] = time.time()
        _atomic_json(raw, path)
        return JobRecord(**raw)

    def get_job(self, job_id: str) -> JobRecord | None:
        raw = _read_json(self.jobs_dir / f"{job_id}.json")
        return JobRecord(**raw) if raw else None

    def list_jobs(self) -> list[dict]:
        out = []
        for j in self.list_entries("jobs"):
            job = self.get_job(j["id"])
            if job:
                out.append({"id": job.id, "kind": job.kind, "status": job.status,
                            "output_dir": job.output_dir,
                            "created_at": job.created_at,
                            "updated_at": job.updated_at,
                            "error": job.error})
        return sorted(out, key=lambda j: -j["created_at"])
