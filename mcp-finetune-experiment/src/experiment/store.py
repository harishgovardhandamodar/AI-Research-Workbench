"""Experiment state store: versioned stages, adapters, checkpoints and config.

Everything is persisted as JSON under the experiment directory so each stage is
inspectable, rollback-able and branchable, and the whole history is retained for
the comparison/report tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common import epoch, hash_object, json_dump, json_load, now_iso


class ExperimentError(RuntimeError):
    pass


class ExperimentStore:
    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.project_dir / "experiment_state.json"

    # ------------------------------------------------------------- state ----
    def _load(self) -> dict:
        if not self.state_path.exists():
            return {
                "version": 1,
                "experiment": None,
                "stages": [],
                "adapters": [],
                "checkpoints": [],
                "config": {},
                "history": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        state = json_load(self.state_path)
        state.setdefault("stages", [])
        state.setdefault("adapters", [])
        state.setdefault("checkpoints", [])
        state.setdefault("config", {})
        state.setdefault("history", [])
        return state

    def _save(self, state: dict) -> dict:
        state["updated_at"] = now_iso()
        json_dump(self.state_path, state)
        return state

    def _mutate(self, fn) -> dict:
        state = self._load()
        out = fn(state)
        return self._save(state), out

    # --------------------------------------------------------- experiment ----
    def init_experiment(self, name: str, base_model: str, paper: dict | None = None,
                        seed: int = 0, config: dict | None = None) -> dict:
        def _fn(state: dict) -> dict:
            state["experiment"] = {
                "name": name,
                "base_model": base_model,
                "paper": paper or {},
                "seed": seed,
                "created_at": now_iso(),
            }
            state["config"] = {
                "base_model": base_model,
                "seed": seed,
                "training": {
                    "method": "lora",
                    "lora_rank": 8,
                    "lora_alpha": 16,
                    "learning_rate": 5e-5,
                    "epochs": 1,
                    "batch_size": 4,
                    "max_seq_len": 1024,
                    "quantization": None,
                },
                "eval": {"judge_model": None, "subset": 200},
            }
            if config:
                state["config"] = _deep_merge(state["config"], config)
            if not state["stages"]:
                self._add_stage(state, "stage_0", name="Base / initial SFT",
                                parent=None, config=state["config"])
            return state["experiment"]

        state, _ = self._mutate(_fn)
        return state["experiment"]

    def get_experiment(self) -> dict | None:
        return self._load().get("experiment")

    # ------------------------------------------------------------- stages ----
    def _add_stage(self, state: dict, stage_id: str, name: str, parent: str | None,
                   config: dict | None) -> dict:
        if any(s["id"] == stage_id for s in state["stages"]):
            raise ExperimentError(f"stage {stage_id} already exists")
        stage = {
            "id": stage_id,
            "name": name,
            "parent": parent,
            "status": "pending",      # pending -> training -> done / failed
            "config": config,
            "adapter_id": None,
            "data_hashes": [],
            "metrics": {},
            "created_at": now_iso(),
            "finished_at": None,
        }
        state["stages"].append(stage)
        return stage

    def create_stage(self, stage_id: str, name: str = "", parent: str | None = None,
                     config: dict | None = None, data_hashes: list[str] | None = None) -> dict:
        def _fn(state: dict) -> dict:
            stage = self._add_stage(state, stage_id, name or stage_id, parent, config)
            if data_hashes:
                stage["data_hashes"] = data_hashes
            state["history"].append({"action": "create_stage", "stage_id": stage_id,
                                     "at": now_iso()})
            return stage

        _, stage = self._mutate(_fn)
        return stage

    def get_stage(self, stage_id: str) -> dict:
        for s in self._load()["stages"]:
            if s["id"] == stage_id:
                return s
        raise ExperimentError(f"unknown stage: {stage_id}")

    def update_stage(self, stage_id: str, **fields) -> dict:
        def _fn(state: dict) -> dict:
            for s in state["stages"]:
                if s["id"] == stage_id:
                    s.update({k: v for k, v in fields.items() if v is not None})
                    if "finished_at" in fields or fields.get("status") in ("done", "failed"):
                        s["finished_at"] = s.get("finished_at") or now_iso()
                    return s
            raise ExperimentError(f"unknown stage: {stage_id}")

        _, stage = self._mutate(_fn)
        return stage

    def list_stages(self) -> list[dict]:
        return list(self._load()["stages"])

    def current_stage(self) -> dict | None:
        stages = self._load()["stages"]
        if not stages:
            return None
        for s in reversed(stages):
            if s["status"] in ("training", "done"):
                return s
        return stages[-1]

    def rollback_to_stage(self, stage_id: str) -> list[dict]:
        """Drop all stages created after `stage_id`; the experiment branches back."""
        def _fn(state: dict) -> list[dict]:
            ids = {s["id"]: i for i, s in enumerate(state["stages"])}
            if stage_id not in ids:
                raise ExperimentError(f"unknown stage: {stage_id}")
            idx = ids[stage_id]
            removed = state["stages"][idx + 1:]
            state["stages"] = state["stages"][: idx + 1]
            # Remove adapters that were created by rolled-back stages.
            adapter_ids = {s["adapter_id"] for s in removed if s["adapter_id"]}
            if adapter_ids:
                state["adapters"] = [a for a in state["adapters"] if a["id"] not in adapter_ids]
            state["history"].append({"action": "rollback", "to_stage": stage_id, "at": now_iso()})
            return removed

        _, removed = self._mutate(_fn)
        return removed

    # -------------------------------------------------- adapters / ckpts ----
    def add_adapter(self, adapter: dict) -> dict:
        def _fn(state: dict) -> dict:
            state["adapters"].append(adapter)
            return adapter

        _, a = self._mutate(_fn)
        return a

    def list_adapters(self) -> list[dict]:
        return list(self._load()["adapters"])

    def get_adapter(self, adapter_id: str) -> dict:
        for a in self._load()["adapters"]:
            if a["id"] == adapter_id:
                return a
        raise ExperimentError(f"unknown adapter: {adapter_id}")

    def add_checkpoint(self, checkpoint: dict) -> dict:
        def _fn(state: dict) -> dict:
            state["checkpoints"].append(checkpoint)
            return checkpoint

        _, c = self._mutate(_fn)
        return c

    def list_checkpoints(self) -> list[dict]:
        return list(self._load()["checkpoints"])

    def get_checkpoint(self, checkpoint_id: str) -> dict:
        for c in self._load()["checkpoints"]:
            if c["id"] == checkpoint_id:
                return c
        raise ExperimentError(f"unknown checkpoint: {checkpoint_id}")

    # ------------------------------------------------------------ config ----
    def get_config(self) -> dict:
        return self._load()["config"]

    def update_config(self, updates: dict) -> dict:
        def _fn(state: dict) -> dict:
            state["config"] = _deep_merge(state["config"], updates)
            state["history"].append({"action": "update_config", "updates": updates,
                                     "at": now_iso()})
            return state["config"]

        _, cfg = self._mutate(_fn)
        return cfg


def _deep_merge(base: dict, updates: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
