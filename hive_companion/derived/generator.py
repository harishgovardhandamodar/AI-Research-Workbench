"""Generate derived experiments — more scenarios to help narrow AGI specialize.

Each derived experiment is a child of a parent experiment from
personal-experiments/fox/*/experiments.json, with added scenario perturbations
that exercise narrow AGI robustness, distribution shift, imbalance, privacy, efficiency.

Scenarios:
- robustness: noise, missing values, adversarial perturbation
- shift: temporal concept drift, covariate shift
- imbalance: class rarity variations
- privacy: k-anonymity, epsilon, linkage strength
- efficiency: sample budget, quantization, rank

Derived tasks are stored in ~/.hive/derived_experiments/<workbench>/*.json
and also emitted as ledger-eligible experiment runs.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from hive.config import CONFIG_DIR, EXPERIMENTS_DIR

DERIVED_DIR = CONFIG_DIR / "derived_experiments"

# Scenario templates per domain
SCENARIOS = {
    "EDA-creditcard-demo": [
        {"tag": "robustness", "suffix": "Missing 10% + median impute", "hypothesis": "Median imputation recovers f1 under MCAR 10%", "param": "missing_rate=0.10"},
        {"tag": "imbalance", "suffix": "Class 1:500 vs 1:100", "hypothesis": "Focal loss beats undersampling at extreme imbalance", "param": "imbalance=1:500"},
        {"tag": "shift", "suffix": "Temporal drift 2020→2024", "hypothesis": "Calibration drift hurts plausibility over time", "param": "drift=temporal"},
        {"tag": "robustness", "suffix": "Adversarial 5% feature flip", "hypothesis": "Adversarial training improves plausibility under flip", "param": "adv_flip=0.05"},
    ],
    "Godmode-test": [
        {"tag": "robustness", "suffix": "Noisy labels 20% flip", "hypothesis": "L2 regularization helps noisy labels 20%", "param": "noise=0.20"},
        {"tag": "efficiency", "suffix": "Few-shot 50 samples", "hypothesis": "Few-shot degrades SVM more than GB", "param": "n_samples=50"},
    ],
    "UPI-Peer-identification": [
        {"tag": "privacy", "suffix": "k=10 anonymity", "hypothesis": "k=10 reduces reid risk below 0.02 vs k=5 0.05", "param": "k=10"},
        {"tag": "privacy", "suffix": "DP ε=0.5", "hypothesis": "DP ε=0.5 halves risk vs ε=1.0 with utility trade", "param": "epsilon=0.5"},
        {"tag": "shift", "suffix": "Aux linkage 3 quasi-ids", "hypothesis": "Linkage with 3 QIs triples reid vs 1 QI", "param": "aux_qi=3"},
    ],
    "UPI-transaction-Experiments": [
        {"tag": "imbalance", "suffix": "Rare peer 1:1000", "hypothesis": "Rare peer detection needs threshold tuning at 1:1000", "param": "rare=1:1000"},
        {"tag": "robustness", "suffix": "Amount noise σ=20%", "hypothesis": "Amount noise collapses clustering", "param": "amount_noise=0.20"},
    ],
    "fraud-demo": [
        {"tag": "shift", "suffix": "Concept drift fraud pattern v2", "hypothesis": "Drift v2 drops f1 10 points without retrain", "param": "drift=v2"},
        {"tag": "imbalance", "suffix": "Fraud 0.3% vs 1%", "hypothesis": "Threshold recalibration needed at 0.3%", "param": "fraud_rate=0.003"},
    ],
    "kaggle-demo": [
        {"tag": "efficiency", "suffix": "Budget 5min vs 30min", "hypothesis": "5min budget still reaches 95% of 30min f1 with tuning", "param": "budget=5min"},
        {"tag": "robustness", "suffix": "Missing categorical 15%", "hypothesis": "Target encoding robust to 15% missing cats", "param": "cat_missing=0.15"},
    ],
    "privacy-analysis-2024": [
        {"tag": "privacy", "suffix": "Membership inference 100 shadow", "hypothesis": "100 shadow models improve MI AUC 0.05", "param": "shadow=100"},
        {"tag": "privacy", "suffix": "Synthetic vs real 10k", "hypothesis": "Synthetic 10k preserves utility but cuts risk 50%", "param": "synthetic=10k"},
    ],
    "quai-lora": [
        {"tag": "efficiency", "suffix": "Rank 4 vs 16", "hypothesis": "Rank 4 retains 98% of rank 16 with 4x fewer params", "param": "rank=4"},
        {"tag": "efficiency", "suffix": "Quant 4-bit vs 8-bit", "hypothesis": "4-bit quant drops loss 0.02 vs 8-bit", "param": "quant=4bit"},
        {"tag": "robustness", "suffix": "LR 1e-4 vs 2e-4", "hypothesis": "LR 1e-4 stabilizes LoRA vs 2e-4 diverging", "param": "lr=1e-4"},
    ],
    "fox-audit-trail": [
        {"tag": "robustness", "suffix": "Tamper 1% log drop", "hypothesis": "Hash chain detects 1% drop with 100% recall", "param": "tamper_drop=0.01"},
        {"tag": "shift", "suffix": "High volume 10k eps", "hypothesis": "Audit throughput 10k eps keeps impact <40", "param": "eps=10k"},
    ],
    "audit-demo": [
        {"tag": "privacy", "suffix": "Redact PII 99% recall", "hypothesis": "Sensitive classifier redacts PII 99% at severity 3", "param": "redact_recall=0.99"},
    ],
    "default": [
        {"tag": "robustness", "suffix": "Noise σ=15% generic", "hypothesis": "Generic narrow AGI degrades gracefully under σ=15%", "param": "noise=0.15"},
        {"tag": "shift", "suffix": "Few-shot transfer", "hypothesis": "Few-shot transfer within narrow lane retains 80%", "param": "fewshot=transfer"},
    ],
}

GENERIC = SCENARIOS["default"]


def _parent_experiments(workbench: str) -> list[dict]:
    """Load parent experiments for a workbench from personal-experiments."""
    if not EXPERIMENTS_DIR or not EXPERIMENTS_DIR.exists():
        return []
    # try fox/<wb> then top <wb>
    for cand in [EXPERIMENTS_DIR / "fox" / workbench / "experiments.json", EXPERIMENTS_DIR / workbench / "experiments.json"]:
        if cand.exists():
            try:
                j = json.loads(cand.read_text())
                return j.get("experiments", [])[:3]  # at most 3 parents per wb
            except Exception:
                continue
    return []


def generate_derived(workbench: str | None = None, per_wb: int = 2) -> list[Path]:
    """Generate derived JSON files for workbench(es). Returns list of created paths."""
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    targets = [workbench] if workbench else sorted({p["workbench"] for p in _all_plan()})
    out: list[Path] = []
    for wb in targets:
        parents = _parent_experiments(wb)
        if not parents:
            # fallback: use generic parent
            parents = [{"id": 1, "name": f"{wb} base", "hypothesis": f"Base narrow AGI for {wb}", "goal_metric": "score", "goal_target": 0.9}]
        scenarios = SCENARIOS.get(wb, GENERIC)
        # pick per_wb scenarios round-robin
        for i in range(min(per_wb, len(scenarios))):
            sc = scenarios[i % len(scenarios)]
            parent = parents[i % len(parents)]
            did = uuid.uuid4().hex[:8]
            derived = {
                "id": did,
                "parent_id": parent.get("id"),
                "parent_name": parent.get("name"),
                "workbench": wb,
                "name": f"{parent.get('name','')} — Derived [{sc['tag']}] {sc['suffix']}",
                "hypothesis": sc["hypothesis"],
                "scenario": sc["tag"],
                "param": sc["param"],
                "goal_metric": parent.get("goal_metric", "score"),
                "goal_target": parent.get("goal_target"),
                "status": "derived",
                "created_at": time.time(),
                "derived_from": str(EXPERIMENTS_DIR / "fox" / wb / "experiments.json") if wb else "",
                "task": f"{parent.get('name','')} — Derived [{sc['tag']}] {sc['suffix']} — {sc['hypothesis']} ({sc['param']})",
            }
            wb_dir = DERIVED_DIR / wb
            wb_dir.mkdir(parents=True, exist_ok=True)
            path = wb_dir / f"{did}.json"
            path.write_text(json.dumps(derived, indent=2))
            out.append(path)
    return out


def _all_plan() -> list[dict]:
    """Helper to list all workbenches from personal-experiments."""
    if not EXPERIMENTS_DIR or not EXPERIMENTS_DIR.exists():
        return [{"workbench": "default"}]
    # reuse workbench list
    from hive.workbench import list_workbenches
    wbs = list_workbenches()
    # keep only those that had parents
    return [{"workbench": wb["name"]} for wb in wbs if "derived" not in wb["name"]][:20]


def list_derived(workbench: str | None = None) -> list[dict]:
    if not DERIVED_DIR.exists():
        return []
    pats = [DERIVED_DIR / workbench / "*.json"] if workbench else [DERIVED_DIR.glob("*.json"), DERIVED_DIR.glob("*/*.json")]
    # simpler: rglob
    files = list(DERIVED_DIR.rglob("*.json")) if not workbench else list((DERIVED_DIR / workbench).glob("*.json"))
    out = []
    for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            j = json.loads(f.read_text())
            j["path"] = str(f)
            out.append(j)
        except Exception:
            continue
    if workbench:
        out = [x for x in out if x.get("workbench") == workbench]
    return out


def load_derived(did: str) -> dict | None:
    for f in DERIVED_DIR.rglob(f"{did}.json"):
        try:
            return json.loads(f.read_text())
        except Exception:
            continue
    return None
