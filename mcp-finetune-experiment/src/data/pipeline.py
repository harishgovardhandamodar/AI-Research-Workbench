"""Dataset pipeline: generate, list, inspect, add-incremental, split, validate.

Each dataset is a JSONL file under ``data/`` whose lines look like::

    {
      "id": "traj-0007",
      "kind": "trajectory" | "teacher" | "rubric",
      "messages": [{"role": "user"|"assistant", "content": "..."}],
      "tool_calls": [{"name": "mcp.dataset.inspect", "arguments": {...}}],
      "expected": "...",        # expected assistant action / label
      "label": "...",           # rubric label (for RFT / GRPO-style rewards)
      "source": "synthetic|paper|augmented",
      "meta": {...}
    }

All files are content-hashed so stages can record exactly which data they used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import hash_object, now_iso, rng, sha256_file

VALID_KINDS = {"trajectory", "teacher", "rubric"}


class DatasetError(RuntimeError):
    pass


class DataPipeline:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ listing ----
    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.data_dir.glob("*.jsonl")):
            out.append(self._meta(p))
        return out

    def _meta(self, path: Path) -> dict:
        count = sum(1 for _ in self._iter(path))
        return {
            "name": path.stem,
            "path": str(path.relative_to(self.data_dir)),
            "records": count,
            "sha256": sha256_file(path),
            "modified": path.stat().st_mtime,
        }

    def _path(self, name: str) -> Path:
        name = Path(name).name
        if not name.endswith(".jsonl"):
            name += ".jsonl"
        return self.data_dir / name

    def _iter(self, path: Path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    # -------------------------------------------------------------- inspect ----
    def inspect(self, name: str, limit: int = 10) -> dict:
        p = self._path(name)
        if not p.exists():
            raise DatasetError(f"dataset not found: {name}")
        records = list(self._iter(p))
        kinds: dict[str, int] = {}
        for r in records:
            kinds[r.get("kind", "?")] = kinds.get(r.get("kind", "?"), 0) + 1
        return {
            "name": p.stem,
            "path": str(p.relative_to(self.data_dir)),
            "records": len(records),
            "sha256": sha256_file(p),
            "kinds": kinds,
            "sample": records[: max(0, int(limit))],
        }

    # ------------------------------------------------------------- generate ----
    def generate(self, name: str, n_trajectories: int = 200,
                 n_teacher: int = 200, n_rubric: int = 100, seed: int = 0,
                 template: dict | None = None) -> dict:
        """Deterministically generate synthetic tool-using-agent data.

        ``template`` can supply per-kind canned examples (e.g. the paper's tool
        schemas or prompt format); otherwise a generic MCP tool-using template
        is used. Re-running with the same seed reproduces the same file.
        """
        p = self._path(name)
        r = rng(f"{name}:{seed}")
        template = template or {}
        records: list[dict] = []
        tools = template.get("tools") or [
            {"name": "mcp.dataset.inspect", "args": {"path": "..."}},
            {"name": "mcp.train.get_metrics", "args": {}},
            {"name": "mcp.eval.compare_to_paper", "args": {}},
        ]
        for i in range(n_trajectories):
            ti = r.choice(tools)
            records.append({
                "id": f"traj-{seed:04d}-{i:04d}",
                "kind": "trajectory",
                "messages": [
                    {"role": "user", "content": f"Complete task {i} by calling the right tool."},
                    {"role": "assistant", "content": f"Call {ti['name']} with correct arguments."},
                ],
                "tool_calls": [{"name": ti["name"], "arguments": ti["args"]}],
                "expected": ti["name"],
                "source": template.get("source", "synthetic"),
                "meta": {"seed": seed, "i": i},
            })
        for i in range(n_teacher):
            ti = r.choice(tools)
            records.append({
                "id": f"teach-{seed:04d}-{i:04d}",
                "kind": "teacher",
                "messages": [
                    {"role": "user", "content": f"Demonstrate tool use for step {i}."},
                    {"role": "assistant", "content": "I will call the tool now."},
                ],
                "tool_calls": [{"name": ti["name"], "arguments": ti["args"]}],
                "expected": json.dumps({"tool": ti["name"]}),
                "source": template.get("source", "synthetic"),
                "meta": {"seed": seed, "i": i},
            })
        for i in range(n_rubric):
            ok = r.random() > 0.2
            records.append({
                "id": f"rubr-{seed:04d}-{i:04d}",
                "kind": "rubric",
                "messages": [
                    {"role": "user", "content": f"Judge completion of task {i}."},
                    {"role": "assistant", "content": "The answer is correct." if ok else "Wrong tool called."},
                ],
                "expected": "correct" if ok else "incorrect",
                "label": 1.0 if ok else 0.0,
                "source": template.get("source", "synthetic"),
                "meta": {"seed": seed, "i": i},
            })
        r.shuffle(records)
        with open(p, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return self._meta(p)

    # -------------------------------------------------- add incremental ----
    def add_incremental(self, name: str, records: list[dict], append: bool = True) -> dict:
        """Append (or create) new examples without rebuilding existing data."""
        if not isinstance(records, list) or not records:
            raise DatasetError("records must be a non-empty list")
        p = self._path(name)
        existing = sum(1 for _ in self._iter(p)) if p.exists() and append else 0
        with open(p, "a" if (append and p.exists()) else "w", encoding="utf-8") as fh:
            for i, rec in enumerate(records, start=existing):
                rec = dict(rec)
                rec.setdefault("id", f"{name}-{i:05d}")
                rec.setdefault("kind", "trajectory")
                if rec["kind"] not in VALID_KINDS:
                    raise DatasetError(f"invalid kind: {rec['kind']}")
                rec.setdefault("source", "augmented")
                rec.setdefault("meta", {})
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return self._meta(p)

    # --------------------------------------------------------------- split ----
    def split(self, name: str, train_frac: float = 0.8, val_frac: float = 0.1,
              seed: int = 0, overwrite: bool = False) -> dict:
        if not (0.0 <= train_frac <= 1.0 and 0.0 <= val_frac <= 1.0):
            raise DatasetError("fractions must be in [0,1]")
        p = self._path(name)
        if not p.exists():
            raise DatasetError(f"dataset not found: {name}")
        records = list(self._iter(p))
        r = rng(f"split:{name}:{seed}")
        r.shuffle(records)
        n = len(records)
        n_train = int(n * train_frac)
        n_val = int(n * val_frac)
        parts = {
            f"{name}_train": records[:n_train],
            f"{name}_val": records[n_train:n_train + n_val],
            f"{name}_test": records[n_train + n_val:],
        }
        out = {}
        for part_name, rows in parts.items():
            fp = self._path(part_name)
            if fp.exists() and not overwrite:
                raise DatasetError(f"{part_name} already exists (use overwrite=True)")
            with open(fp, "w", encoding="utf-8") as fh:
                for rec in rows:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out[part_name] = self._meta(fp)
        return out

    # ------------------------------------------------------------ validate ----
    def validate(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            raise DatasetError(f"dataset not found: {name}")
        errors: list[dict] = []
        n = 0
        kinds: dict[str, int] = {}
        for i, rec in enumerate(self._iter(p)):
            n += 1
            if not isinstance(rec, dict):
                errors.append({"line": i, "error": "record is not an object"})
                continue
            kind = rec.get("kind")
            if kind not in VALID_KINDS:
                errors.append({"line": i, "error": f"invalid kind {kind!r}"})
            else:
                kinds[kind] = kinds.get(kind, 0) + 1
            if "id" not in rec:
                errors.append({"line": i, "error": "missing id"})
        return {
            "name": p.stem,
            "records": n,
            "valid": not errors,
            "errors": errors[:50],
            "kinds": kinds,
            "sha256": sha256_file(p),
            "validated_at": now_iso(),
        }
