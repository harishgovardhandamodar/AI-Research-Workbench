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


def record_experiment(record: dict) -> None:
    """Append any experiment run (notebook rerun, workflow run, ...) to history."""
    runs = load_experiments()
    runs.append(record)
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(json.dumps(runs, indent=2))


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
        return _metric_similarity(a, b)
    span = max(max(va), max(vb), 1e-9)
    mad = sum(abs(x - y) for x, y in zip(va, vb)) / len(va)
    return max(0.0, 1.0 - mad / span)


def _metric_similarity(a: dict, b: dict) -> float:
    """Similarity from shared numeric metrics (works for any run record)."""
    ma, mb = _node_metrics(a), _node_metrics(b)
    keys = sorted(set(ma) & set(mb))
    if not keys:
        return 0.0
    pairs = [(ma[k], mb[k]) for k in keys]
    lo = min(min(x, y) for x, y in pairs)
    hi = max(max(x, y) for x, y in pairs)
    span = (hi - lo) or 1.0
    mad = sum(abs(x - y) for x, y in pairs) / len(pairs)
    return max(0.0, 1.0 - mad / span)


def compare_runs(a: dict, b: dict) -> dict:
    """Metric delta table between two run records.

    Returns
        {"a": label_a, "b": label_b, "rows": [{metric, a, b, delta, pct}...],
         "summary": {shared, improved, worsened, unchanged, direction}}
    """
    ma, mb = _node_metrics(a), _node_metrics(b)
    keys = sorted(set(ma) & set(mb))
    rows = []
    increased = decreased = unchanged = 0
    for k in keys:
        va, vb = ma[k], mb[k]
        delta = vb - va
        pct = (delta / va * 100) if va else 0.0
        rows.append({
            "metric": k,
            "a": va, "b": vb,
            "delta": round(delta, 4),
            "pct": round(pct, 1),
        })
        if abs(delta) < 1e-12:
            unchanged += 1
        elif delta > 0:
            increased += 1
        else:
            decreased += 1
    label = lambda r: (r.get("label") or
                       (f"notebook" if r.get("kind") == "notebook" else f"run {r.get('id')}"))
    return {
        "a": label(a),
        "b": label(b),
        "rows": rows,
        "summary": {
            "shared": len(keys),
            "increased": increased,
            "decreased": decreased,
            "unchanged": unchanged,
        },
    }


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

def _node_metrics(run: dict) -> dict:
    """Uniform numeric metrics dict for any run record (privacy or notebook)."""
    metrics = {}
    raw = run.get("metrics")
    if isinstance(raw, dict):
        metrics.update({k: v for k, v in raw.items() if isinstance(v, (int, float))})
    s1 = run.get("stage1") or []
    s2 = run.get("stage2") or {}
    s3 = run.get("stage3") or []
    if s1:
        last1 = s1[-1]
        if last1.get("linkage_success") is not None:
            metrics["linkage50"] = last1.get("linkage_success")
        if last1.get("attack_plausibility") is not None:
            metrics["plausibility"] = last1.get("attack_plausibility")
    if s2.get("unique_pct") is not None:
        metrics["unique_pct"] = s2["unique_pct"]
    if s3 and s3[0].get("attacker_pred_rmse") is not None:
        metrics["rmse_eps0_1"] = s3[0]["attacker_pred_rmse"]
    return metrics


def build_graph() -> dict:
    """Nodes (one per run/experiment, with headline metrics) + similarity edges."""
    runs = load_experiments()
    nodes = []
    for i, r in enumerate(runs):
        kind = r.get("kind", "privacy_workflow")
        label = r.get("label") or ("notebook" if kind == "notebook" else f"seed {r.get('seed')}")
        nodes.append({
            "id": r.get("id"),
            "index": i,
            "kind": kind,
            "label": label,
            "seed": r.get("seed"),
            "fresh": bool(r.get("fresh")),
            "timestamp": r.get("timestamp"),
            "metrics": _node_metrics(r),
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
