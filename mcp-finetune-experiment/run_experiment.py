"""Drive the incremental fine-tuning experiment for Paper 2305.15587 via MCP.

Agent workflow:
  1. connect to the combined MCP server
  2. create the experiment (paper.json has the authors' realistic ASR)
  3. generate + split a harder attack-perturbation dataset
  4. Stage 0: base SFT -> eval -> compare ASR vs paper
  5. Stage N: incremental stages (new data + hyperparam changes) -> eval -> compare
  6. export the lab-notebook report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "notebooks"))
sys.path.insert(0, str(ROOT / "src"))

from mcp_client import Client  # noqa: E402

# The paper's reported (realistic) model ASR for the attack methods.
PAPER_ASR = {
    "attack_success_rate": 0.947,
    "success_rate": 0.947,
    "accuracy": 0.856,
}

# A synthetic "tool environment" matching the paper's attack methods, so the
# agent learns to pick the right attack tool + arguments per dataset.
ATTACK_TEMPLATE = {
    "source": "2305.15587",
    "tools": [
        {"name": "mcp.attack.charsugar", "args": {"dataset": "imdb", "target": "bert"}},
        {"name": "mcp.attack.hotflip", "args": {"dataset": "imdb", "target": "bert"}},
        {"name": "mcp.attack.textfooler", "args": {"dataset": "imdb", "target": "bert"}},
        {"name": "mcp.attack.charsugar", "args": {"dataset": "sst2", "target": "bert"}},
        {"name": "mcp.attack.hotflip", "args": {"dataset": "sst2", "target": "bert"}},
        {"name": "mcp.attack.textfooler", "args": {"dataset": "sst2", "target": "bert"}},
        {"name": "mcp.attack.evaluate", "args": {"metric": "asr", "humans": True}},
    ],
}


def line(rows: list[dict]) -> str:
    if not rows:
        return "(no comparable metrics)"
    head = " | ".join(rows[0].keys())
    body = "\n".join(" | ".join(str(r[k]) for k in rows[0]) for r in rows)
    return head + "\n" + body


async def run(project: str) -> None:
    mcp = Client(project_dir=project)
    await mcp.connect()
    try:
        paper = json.loads((Path(project) / "paper.json").read_text())

        print("== mcp.experiment.create ==")
        await mcp.call("mcp.experiment.create", {
            "name": "2305.15587-asr-improve",
            "base_model": "Qwen/Qwen2.5-0.5B-Instruct",
            "paper": paper,
            "seed": 0,
            "config": {"training": {"method": "lora", "lora_rank": 8,
                                    "epochs": 1, "learning_rate": 5e-5},
                       "eval": {"subset": 200}},
        })

        print("== mcp.dataset.generate / split / validate ==")
        await mcp.call("mcp.dataset.generate", {
            "name": "attack_perturbations", "n_trajectories": 240,
            "n_teacher": 240, "n_rubric": 120, "seed": 0,
            "template": ATTACK_TEMPLATE})
        splits = await mcp.call("mcp.dataset.split", {"name": "attack_perturbations", "seed": 0})
        train_hash = splits["attack_perturbations_train"]["sha256"]
        val = await mcp.call("mcp.dataset.validate", {"name": "attack_perturbations_train"})
        assert val["valid"], val
        print(f"train={splits['attack_perturbations_train']['records']} "
              f"val={splits['attack_perturbations_val']['records']} "
              f"test={splits['attack_perturbations_test']['records']}")

        # ---------- Stage 0 ----------
        print("\n== Stage 0: base SFT (authors' realistic ASR as target) ==")
        # stage_0 is auto-created by mcp.experiment.create; start training on it.
        await mcp.call("mcp.train.start_stage", {
            "stage_id": "stage_0", "new_data": "attack_perturbations_train",
            "epochs": 1, "lr": 5e-5, "lora_rank": 8})
        m0 = (await mcp.call("mcp.train.get_metrics", {"stage_id": "stage_0"}))["metrics"]
        print(f"  train_loss={m0['final_train_loss']} quality={m0.get('quality')}")
        ev0 = await mcp.call("mcp.eval.run", {"stage_id": "stage_0"})
        print(f"  ASR (simulated) = {ev0['metrics']['success_rate']}")
        c0 = await mcp.call("mcp.eval.compare_to_paper", {"stage_id": "stage_0"})
        print("  vs paper:\n" + line(c0["table"]))

        # ---------- Stage 1: harder data + higher rank ----------
        print("\n== Stage 1: incremental (curriculum: harder ASR targets, rank 16) ==")
        harder = [{"kind": "rubric",
                   "messages": [{"role": "user", "content": "Judge if perturbation flips BERT on a realistic IMDB sentence."},
                                {"role": "assistant", "content": "yes, ASR achieved"}],
                   "expected": "correct", "label": 1.0,
                   "meta": {"difficulty": "hard", "dataset": "imdb"}}] * 30
        await mcp.call("mcp.dataset.add_incremental", {"name": "attack_curriculum",
                                                       "records": harder})
        await mcp.call("mcp.train.set_hyperparams", {
            "updates": {"learning_rate": 2e-5, "lora_rank": 16, "method": "lora"}})
        await mcp.call("mcp.experiment.create_stage", {
            "stage_id": "stage_1", "name": "Curriculum + rank 16", "parent": "stage_0"})
        await mcp.call("mcp.train.start_stage", {
            "stage_id": "stage_1", "from_adapter": "stage_0-adapter",
            "new_data": "attack_curriculum", "lora_rank": 16})
        m1 = (await mcp.call("mcp.train.get_metrics", {"stage_id": "stage_1"}))["metrics"]
        print(f"  train_loss={m1['final_train_loss']} quality={m1.get('quality')}")
        ev1 = await mcp.call("mcp.eval.run", {"stage_id": "stage_1"})
        print(f"  ASR (simulated) = {ev1['metrics']['success_rate']}")
        c1 = await mcp.call("mcp.eval.compare_to_paper", {"stage_id": "stage_1"})
        print("  vs paper:\n" + line(c1["table"]))

        # ---------- Stage 2: another incremental pass ----------
        print("\n== Stage 2: incremental (hardest rubric data, low LR) ==")
        hardest = [{"kind": "rubric",
                    "messages": [{"role": "user", "content": "Judge if perturbation flips BERT on realistic SST-2."},
                                 {"role": "assistant", "content": "yes, ASR achieved"}],
                    "expected": "correct", "label": 1.0,
                    "meta": {"difficulty": "hard", "dataset": "sst2"}}] * 40
        await mcp.call("mcp.dataset.add_incremental", {"name": "attack_hardest",
                                                       "records": hardest})
        await mcp.call("mcp.train.set_hyperparams", {
            "updates": {"learning_rate": 8e-6, "epochs": 1.5}})
        await mcp.call("mcp.experiment.create_stage", {
            "stage_id": "stage_2", "name": "Hardest rubric data, fine LR", "parent": "stage_1"})
        await mcp.call("mcp.train.start_stage", {
            "stage_id": "stage_2", "from_adapter": "stage_1-adapter",
            "new_data": "attack_hardest", "epochs": 1.5, "lr": 8e-6})
        m2 = (await mcp.call("mcp.train.get_metrics", {"stage_id": "stage_2"}))["metrics"]
        print(f"  train_loss={m2['final_train_loss']} quality={m2.get('quality')}")
        ev2 = await mcp.call("mcp.eval.run", {"stage_id": "stage_2"})
        print(f"  ASR (simulated) = {ev2['metrics']['success_rate']}")
        c2 = await mcp.call("mcp.eval.compare_to_paper", {"stage_id": "stage_2"})
        print("  vs paper:\n" + line(c2["table"]))

        # ---------- failures + report ----------
        fc = await mcp.call("mcp.eval.failure_cases", {"stage_id": "stage_2", "top_k": 3})
        print(f"\n== failure cases (stage_2): {fc['count']} ==")
        rep = await mcp.call("mcp.experiment.export_report", {"stage_id": "stage_2"})
        print(f"\nreport: {rep['report_path']}")
        stages = (await mcp.call("mcp.experiment.list_stages", {}))["stages"]
        print("\n== stage summary ==")
        for s in stages:
            ev = (s.get("metrics") or {}).get("eval") or {}
            print(f"  {s['id']}: {s['status']}  eval_accuracy={ev.get('accuracy')} "
                  f"success_rate={ev.get('success_rate')}")
    finally:
        await mcp.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the 2305.15587 ASR-improvement experiment.")
    parser.add_argument("--project", default="experiments/2305.15587")
    args = parser.parse_args()
    asyncio.run(run(args.project))


if __name__ == "__main__":
    main()
