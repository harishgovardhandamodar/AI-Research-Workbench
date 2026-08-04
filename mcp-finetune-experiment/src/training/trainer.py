"""Incremental fine-tuning: every stage loads the previous adapter/checkpoint.

Two execution modes:
  * **simulate** (default when torch/transformers/peft are not installed) —
    produces deterministic, hash-seeded metrics/checkpoints so the entire
    notebook loop (stage -> train -> eval -> compare -> next stage) is fully
    exercisable without a GPU.
  * **real** — a compact LoRA/QLoRA SFT loop (transformers + PEFT + TRL when
    available). Set ``training.real=True`` in the config and install
    ``requirements-train.txt`` to use it.

Every stage records: adapter metadata (id, parent, base model, hyperparams),
data hashes, a loss curve and final metrics.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from common import hash_object, json_dump, now_iso, rng, sha256_text
from experiment.store import ExperimentStore
from .hyperparams import apply_hyperparams

_REAL_DEPS = ("torch", "transformers", "peft")


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


REAL_AVAILABLE = all(_importable(m) for m in _REAL_DEPS)


class TrainingError(RuntimeError):
    pass


class Trainer:
    def __init__(self, store: ExperimentStore, project_dir: Path, config: dict):
        self.store = store
        self.project_dir = Path(project_dir)
        self.adapters_dir = self.project_dir / "adapters"
        self.checkpoints_dir = self.project_dir / "checkpoints"
        self.adapters_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or {}
        self._status: dict[str, str] = {}

    # ----------------------------------------------------- hyperparameters ----
    def set_hyperparams(self, updates: dict) -> dict:
        """Update training hyperparameters between stages (LR, rank, epochs…)."""
        tcfg = apply_hyperparams(self.config.get("training", {}), updates)
        self.config["training"] = tcfg
        self.store.update_config({"training": tcfg})
        return tcfg

    # ------------------------------------------------------ stage / status ----
    def get_status(self, stage_id: str) -> dict:
        stage = self.store.get_stage(stage_id)
        adapter = None
        if stage.get("adapter_id"):
            adapter = self.store.get_adapter(stage["adapter_id"])
        return {
            "stage": stage["id"],
            "status": stage["status"],
            "adapter": stage.get("adapter_id"),
            "progress": self._status.get(stage_id, {}),
            "message": self._status.get(stage_id, {}).get("message", ""),
            "adapter_info": adapter,
        }

    def get_metrics(self, stage_id: str) -> dict:
        stage = self.store.get_stage(stage_id)
        return {
            "stage": stage["id"],
            "metrics": stage.get("metrics", {}),
        }

    # ------------------------------------------------------- incremental -----
    async def start_stage(self, stage_id: str, from_adapter: str | None = None,
                          new_data: str | None = None, epochs: float | None = None,
                          lr: float | None = None, lora_rank: int | None = None,
                          method: str | None = None) -> dict:
        stage = self.store.get_stage(stage_id)
        tcfg = dict(self.config.get("training", {}))
        if epochs is not None:
            tcfg["epochs"] = epochs
        if lr is not None:
            tcfg["learning_rate"] = lr
        if lora_rank is not None:
            tcfg["lora_rank"] = lora_rank
        if method is not None:
            tcfg["method"] = method

        # Load the parent adapter (incremental contract) or the base model.
        parent_adapter = None
        if from_adapter:
            parent_adapter = self.store.get_adapter(from_adapter)
        elif stage.get("parent"):
            parent_stage = self.store.get_stage(stage["parent"])
            if parent_stage.get("adapter_id"):
                parent_adapter = self.store.get_adapter(parent_stage["adapter_id"])

        data_hashes = list(stage.get("data_hashes") or [])
        # Inherit the parent stage's data so incremental stages accumulate data
        # (this is what makes the "incremental" contract visible in metrics).
        if parent_adapter:
            for h in parent_adapter.get("data_hashes", []):
                if h not in data_hashes:
                    data_hashes.append(h)
        elif stage.get("parent"):
            try:
                pstage = self.store.get_stage(stage["parent"])
                for h in pstage.get("data_hashes", []):
                    if h not in data_hashes:
                        data_hashes.append(h)
            except Exception:  # noqa: BLE001
                pass
        if new_data:
            from data.pipeline import DataPipeline

            meta = DataPipeline(self.project_dir / "data").inspect(new_data)
            data_hashes.append(meta["sha256"])

        adapter_id = f"{stage_id}-adapter"
        adapter = {
            "id": adapter_id,
            "stage": stage_id,
            "base_model": self.config.get("base_model", "base"),
            "from_adapter": parent_adapter["id"] if parent_adapter else None,
            "method": tcfg.get("method", "lora"),
            "hyperparams": tcfg,
            "data_hashes": data_hashes,
            "status": "training",
            "created_at": now_iso(),
        }

        self.store.update_stage(stage_id, status="training", adapter_id=adapter_id,
                                config={"training": tcfg}, data_hashes=data_hashes)

        real = self.config.get("training", {}).get("real", False) and REAL_AVAILABLE
        metrics, curve = await self._train(stage_id, adapter, data_hashes, tcfg, real)

        adapter["status"] = "done"
        adapter["metrics"] = metrics
        adapter["loss_curve"] = curve
        adapter["path"] = str(self.adapters_dir / adapter_id)
        self.store.add_adapter(adapter)
        self.store.update_stage(stage_id, status="done", metrics=metrics,
                                finished_at=now_iso())

        checkpoint = {
            "id": f"ckpt-{stage_id}",
            "stage": stage_id,
            "adapter": adapter_id,
            "from_adapter": adapter["from_adapter"],
            "hyperparams": tcfg,
            "data_hashes": data_hashes,
            "metrics": metrics,
            "path": adapter["path"],
            "created_at": now_iso(),
        }
        self.store.add_checkpoint(checkpoint)
        return {"adapter": adapter, "checkpoint": checkpoint}

    async def resume(self, stage_id: str, steps: int = 20) -> dict:
        stage = self.store.get_stage(stage_id)
        if stage["adapter_id"]:
            adapter = self.store.get_adapter(stage["adapter_id"])
        else:
            raise TrainingError(f"stage {stage_id} has no adapter to resume")
        self._status[stage_id] = {"step": 0, "total": steps, "message": "resuming"}
        curve = list(adapter.get("loss_curve", []))
        r = rng(f"resume:{stage_id}:{steps}")
        loss = adapter.get("metrics", {}).get("final_train_loss", 1.0)
        base_step = len(curve)
        for i in range(steps):
            loss = loss * 0.98 + r.uniform(-0.002, 0.001)
            curve.append({"step": base_step + i, "loss": round(loss, 5)})
            self._status[stage_id] = {"step": i + 1, "total": steps}
            await asyncio.sleep(0)
        metrics = dict(adapter.get("metrics", {}))
        metrics["final_train_loss"] = round(loss, 5)
        metrics["steps_total"] = len(curve)
        adapter["metrics"] = metrics
        adapter["loss_curve"] = curve
        self.store.update_stage(stage_id, status="done", metrics=metrics)
        return {"adapter": adapter, "metrics": metrics}

    async def _train(self, stage_id: str, adapter: dict, data_hashes: list[str],
                     tcfg: dict, real: bool) -> tuple[dict, list[dict]]:
        if real:
            return await asyncio.to_thread(_real_lora_sft, self, stage_id, adapter,
                                           data_hashes, tcfg)
        return _simulate_train(self, stage_id, adapter, data_hashes, tcfg)

    # ------------------------------------------------------- checkpoints -----
    def list_checkpoints(self) -> list[dict]:
        return self.store.list_checkpoints()

    def load_adapter(self, adapter_id: str) -> dict:
        adapter = self.store.get_adapter(adapter_id)
        return {
            "adapter": adapter,
            "path": adapter.get("path"),
            "ready": True,
        }


# ----------------------------------------------------------------- simulate ----
def _simulate_train(trainer: Trainer, stage_id: str, adapter: dict,
                    data_hashes: list[str], tcfg: dict) -> tuple[dict, list[dict]]:
    """Deterministic hash-seeded training so results are reproducible."""
    epochs = float(tcfg.get("epochs", 1))
    lr = float(tcfg.get("learning_rate", 5e-5))
    rank = int(tcfg.get("lora_rank", 8))
    seed = sha256_text(json.dumps([stage_id, data_hashes, tcfg], sort_keys=True))
    r = rng(seed)
    steps = max(3, int(40 * epochs))
    loss = 2.0
    curve = []
    # Deeper stages (more incremental data) land on a lower loss floor.
    depth = max(1, len(data_hashes))
    floor = max(0.045, 0.095 - 0.02 * (depth - 1))
    for i in range(steps):
        progress = i / steps
        target = max(floor, 0.5 * (1 - progress) + floor)
        loss = loss + (target - loss) * 0.14 + r.uniform(-0.015, 0.015)
        curve.append({"step": i, "loss": round(max(loss, floor * 0.9), 5)})
        trainer._status[stage_id] = {"step": i + 1, "total": steps,
                                     "message": f"simulated stage (epoch {progress * epochs:.1f}/{epochs})"}
    final = max(curve[-1]["loss"], floor)
    # Simulated model "quality" drives eval ASR: more data + more steps = better.
    quality = min(0.96, 0.72 + 0.06 * (depth - 1) + 0.03 * min(1.0, steps / 60))
    metrics = {
        "mode": "simulate",
        "final_train_loss": round(final, 5),
        "train_loss": round(final, 5),
        "quality": round(quality, 4),
        "lora_rank": rank,
        "learning_rate": lr,
        "epochs": epochs,
        "steps": steps,
        "adapter_bytes": rank * 2 * 1024 * 1024,
        "data_hashes": data_hashes,
    }
    return metrics, curve


# -------------------------------------------------------------------- real ----
def _real_lora_sft(trainer: Trainer, stage_id: str, adapter: dict,
                   data_hashes: list[str], tcfg: dict) -> tuple[dict, list[dict]]:
    """Compact LoRA/QLoRA supervised fine-tune on the JSONL dataset texts.

    Runs in a worker thread (blocking). Requires ``requirements-train.txt``.
    """
    try:
        import torch  # noqa: F401
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import (AutoModelForCausalLM, AutoTokenizer,
                                  TrainingArguments, Trainer as HFTrainer)
    except Exception as exc:  # noqa: BLE001
        raise TrainingError(
            "real training requires torch/transformers/peft (pip install -r "
            f"requirements-train.txt); got {exc!r}") from exc

    base = trainer.config.get("base_model", "gpt2")
    tokenizer = AutoTokenizer.from_pretrained(base)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(base)
    rank = int(tcfg.get("lora_rank", 8))
    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=rank,
        lora_alpha=int(tcfg.get("lora_alpha", 16)),
        target_modules=["c_attn", "c_proj"] if "gpt2" in base.lower() else ["q_proj", "v_proj"],
        lora_dropout=0.1,
    )
    model = get_peft_model(model, lora_cfg)

    # Load concatenated message texts from the stage's data files as tiny SFT texts.
    texts: list[str] = []
    from data.pipeline import DataPipeline

    dp = DataPipeline(trainer.project_dir / "data")
    for h in data_hashes:
        for meta in dp.list():
            if meta["sha256"] == h:
                for rec in dp._iter(dp._path(meta["name"])):
                    msgs = rec.get("messages") or []
                    texts.append("\n".join(
                        f"{m.get('role')}: {m.get('content', '')}" for m in msgs))
    if not texts:
        texts = ["user: help\nassistant: sure"]

    class TinyDS(torch.utils.data.Dataset):  # type: ignore[name-defined]
        def __init__(self, t):
            enc = tokenizer(t, truncation=True, max_length=int(tcfg.get("max_seq_len", 256)))
            self.ids = enc["input_ids"]

        def __len__(self):
            return len(self.ids)

        def __getitem__(self, i):
            return {"input_ids": torch.tensor(self.ids[i]), "labels": torch.tensor(self.ids[i])}

    train_args = TrainingArguments(
        output_dir=str(trainer.checkpoints_dir / stage_id),
        num_train_epochs=float(tcfg.get("epochs", 1)),
        per_device_train_batch_size=int(tcfg.get("batch_size", 4)),
        learning_rate=float(tcfg.get("learning_rate", 5e-5)),
        logging_steps=1,
        save_strategy="no",
        report_to=[],
        disable_tqdm=True,
    )
    hf = HFTrainer(model=model, args=train_args,
                   train_dataset=TinyDS(texts), tokenizer=tokenizer)
    hf.train()
    out_dir = trainer.adapters_dir / adapter["id"]
    model.save_pretrained(str(out_dir))
    hf_history = hf.state.log_history
    curve = [{"step": i, "loss": float(h["loss"])}
             for i, h in enumerate(hf_history) if "loss" in h]
    final = float(curve[-1]["loss"]) if curve else float("nan")
    metrics = {
        "mode": "real",
        "final_train_loss": final,
        "train_loss": final,
        "lora_rank": rank,
        "learning_rate": float(tcfg.get("learning_rate", 5e-5)),
        "epochs": float(tcfg.get("epochs", 1)),
        "steps": len(curve),
        "data_hashes": data_hashes,
    }
    return metrics, curve
