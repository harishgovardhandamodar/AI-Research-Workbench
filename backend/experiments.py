"""Experiment history for the privacy workflow.

Every workflow run appends a record to <workbench>/privacy_runs.json (the
persistent volume). This module loads that history, exposes it over the API, and
computes per-run similarity / overlap so the UI can render a graph + timeline
that shows how experiments evolved.

A run record looks like:
    {"id": "...", "seed": 42, "fresh": false, "timestamp": "...",
     "settings": {...}, "stage1": [ {coverage, linkage_success, ...}, ...],
     "stage2": {...}, "stage3": [ {epsilon, attacker_pred_rmse, ...}, ...],
     "artifacts": ["audit_trail", "fig_peer_coverage", ...]}
"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import WORKBENCH_DIR

RUNS_FILE = WORKBENCH_DIR / "privacy_runs.json"


def runs_path() -> Path:
    return RUNS_FILE


def load_experiments() -> list[dict]:
    if not RUNS_FILE.exists():
        return []
    try:
        data = json.loads(RUNS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- metrics ----

def _linkage_vec(run: dict) -> list[float]:
    return [float(x.get("linkage_success", 0.0)) for x in run.get("stage1", [])]


def _plausibility(run: dict):
    s1 = run.get("stage1") or []
    if not s1:
        return None
    return s1[-1].get("attack_plausibility")


def _verdict(run: dict):
    s1 = run.get("stage1") or []
    return s1[-1].get("plausibility_verdict") if s1 else None


def similarity(a: dict, b: dict) -> float:
    """1 - normalized mean-absolute-difference of the linkage-vs-coverage curves."""
    va, vb = _linkage_vec(a), _linkage_vec(b)
    if not va or not vb or len(va) != len(vb):
        return 0.0
    span = max(max(va), max(vb), 1e-9)
    mad = sum(abs(x - y) for x, y in zip(va, vb)) / len(va)
    return max(0.0, 1.0 - mad / span)


def overlap(a: dict, b: dict) -> float:
    """Fraction of shared categorical findings (plausibility verdict, re-id risk)."""
    agree, total = 0, 0
    for x, y in [(_verdict(a), _verdict(b)),
                 (a.get("stage2", {}).get("reid_risk"),
                  b.get("stage2", {}).get("reid_risk"))]:
        if x is not None and y is not None:
            total += 1
            agree += int(x == y)
    return (agree / total) if total else 0.0


# ------------------------------------------------------------------- graph ----

def build_graph() -> dict:
    """Nodes (one per run, with headline metrics) + similarity/overlap edges."""
    runs = load_experiments()
    nodes = []
    for i, r in enumerate(runs):
        linkage = _linkage_vec(r)
        s3 = r.get("stage3") or []
        nodes.append({
            "id": r.get("id"),
            "index": i,
            "seed": r.get("seed"),
            "fresh": bool(r.get("fresh")),
            "timestamp": r.get("timestamp"),
            "linkage50": linkage[-1] if linkage else None,
            "plausibility": _plausibility(r),
            "unique_pct": r.get("stage2", {}).get("unique_pct"),
            "rmse_eps0_1": s3[0].get("attacker_pred_rmse") if s3 else None,
            "artifacts": r.get("artifacts", []),
        })
    edges = []
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            sim = similarity(runs[i], runs[j])
            ov = overlap(runs[i], runs[j])
            if sim > 0.05 or ov > 0:
                edges.append({
                    "source": runs[i].get("id"),
                    "target": runs[j].get("id"),
                    "similarity": round(sim, 3),
                    "overlap": round(ov, 3),
                })
    return {"nodes": nodes, "edges": edges}
