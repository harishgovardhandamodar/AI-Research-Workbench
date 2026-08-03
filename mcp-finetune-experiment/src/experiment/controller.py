"""Experiment control: create the project, manage stages, export reports.

Ties the store, data pipeline, trainer and eval harness together and exposes the
high-level operations the ``mcp.experiment.*`` tools call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common import json_dump, now_iso
from data.pipeline import DataPipeline
from eval.harness import EvalHarness
from training.trainer import Trainer
from .store import ExperimentStore


class ExperimentController:
    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.store = ExperimentStore(self.project_dir)
        config = self.store.get_config()
        self.data = DataPipeline(self.project_dir / "data")
        self.trainer = Trainer(self.store, self.project_dir, config)
        self.harness = EvalHarness(self.store, self.project_dir, config)

    # -------------------------------------------------------------- create ----
    def create(self, name: str, base_model: str, paper: dict | None = None,
               seed: int = 0, config: dict | None = None) -> dict:
        for sub in ("data", "adapters", "checkpoints", "reports", "configs"):
            (self.project_dir / sub).mkdir(parents=True, exist_ok=True)
        exp = self.store.init_experiment(name, base_model, paper, seed, config)
        # Refresh trainer/eval with the finalized config.
        self.trainer.config = self.store.get_config()
        self.harness.config = self.store.get_config()
        return exp

    def get_config(self) -> dict:
        return self.store.get_config()

    def update_config(self, updates: dict) -> dict:
        cfg = self.store.update_config(updates)
        self.trainer.config = cfg
        self.harness.config = cfg
        return cfg

    # -------------------------------------------------------------- stages ----
    def create_stage(self, stage_id: str, name: str = "", parent: str | None = None,
                     config: dict | None = None, data_hashes: list[str] | None = None) -> dict:
        return self.store.create_stage(stage_id, name, parent, config, data_hashes)

    def list_stages(self) -> dict:
        return {"stages": self.store.list_stages(),
                "current": (self.store.current_stage() or {}).get("id")}

    def rollback_to_stage(self, stage_id: str) -> dict:
        removed = self.store.rollback_to_stage(stage_id)
        return {"rolled_back_to": stage_id,
                "removed_stages": [s["id"] for s in removed]}

    # ------------------------------------------------------------- report ----
    def export_report(self, stage_id: str | None = None) -> dict:
        stage = None
        if stage_id:
            stage = self.store.get_stage(stage_id)
        else:
            stage = self.store.current_stage()
        if stage is None:
            raise ValueError("no stage to report (create one first)")
        paper = (self.store.get_experiment() or {}).get("paper") or {}
        cmp = self.harness.compare_to_paper(stage["id"])
        adapter = None
        if stage.get("adapter_id"):
            try:
                adapter = self.store.get_adapter(stage["adapter_id"])
            except Exception:  # noqa: BLE001
                adapter = None
        md = _render_markdown(stage, adapter, cmp, paper)
        path = self.project_dir / "reports" / f"report_{stage['id']}.md"
        json_dump(path, None)  # ensure dir exists
        path.write_text(md, encoding="utf-8")
        return {"report_path": str(path.relative_to(self.project_dir)),
                "stage": stage["id"], "markdown": md}


def _render_markdown(stage: dict, adapter: dict | None, cmp: dict,
                     paper: dict) -> str:
    lines = [
        f"# Report: stage `{stage['id']}` — {stage.get('name')}",
        f"- status: {stage.get('status')}",
        f"- adapter: {stage.get('adapter_id')}",
        f"- data hashes: {', '.join(stage.get('data_hashes') or []) or 'none'}",
        f"- created: {stage.get('created_at')}",
        "",
        "## Training",
    ]
    if adapter:
        h = adapter.get("hyperparams", {})
        lines.append(f"- method: {adapter.get('method')} (rank {h.get('lora_rank')}, "
                     f"lr {h.get('learning_rate')}, epochs {h.get('epochs')})")
        lines.append(f"- final train loss: {adapter.get('metrics', {}).get('final_train_loss')}")
    else:
        lines.append("- (no adapter)")
    lines += ["", "## Evaluation vs. paper"]
    if cmp.get("warning"):
        lines.append(f"> {cmp['warning']}")
    if cmp.get("table"):
        lines.append("| metric | paper | ours | delta | status |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in cmp["table"]:
            lines.append(f"| {r['metric']} | {r['paper']} | {r['ours']} | {r['delta']} | {r['status']} |")
    else:
        lines.append("_No reported metrics to compare yet._")
    lines += ["", f"_Generated {now_iso()}_"]
    return "\n".join(lines)
