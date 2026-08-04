"""Experiment traceability: normalization + similarity/graph logic for the UI.

Every project keeps its runs in per-project SQLite (`store.runs`). This module
normalizes those records (and legacy privacy-workflow records) into a unified
shape and computes the metrics / findings / similarity graph the Experiments tab
renders. The legacy <workbench>/privacy_runs.json file is still written by old
callers but is no longer the source of truth for the UI.

A unified run record looks like:
    {"id": "...", "kind": "agent_run|notebook|privacy_workflow|...",
     "label": "...", "seed": ..., "fresh": false,
     "timestamp": "2026-...", "metrics": {numeric}, "findings": [...],
     "artifacts": [{"id": "...", "name": "..."}], "prompt": "..."}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import WORKBENCH_DIR

RUNS_FILE = WORKBENCH_DIR / "privacy_runs.json"

# Serialize read-modify-write cycles so concurrent legacy callers can't corrupt
# the experiments history file.
_RUNS_LOCK = threading.Lock()


def runs_path() -> Path:
    return RUNS_FILE


# ------------------------------------------------------- legacy file shims ----
# Kept so older callers (privacy workflow script glue, notebooks) keep working.
# New code records into the per-project SQLite `runs` table instead.


def load_experiments() -> list[dict]:
    if not RUNS_FILE.exists():
        return []
    try:
        data = json.loads(RUNS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def record_experiment(record: dict) -> None:
    """Legacy: append any experiment run to the global history file."""
    with _RUNS_LOCK:
        runs = load_experiments()
        runs.append(record)
        RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNS_FILE.write_text(json.dumps(runs, indent=2))


# ---------------------------------------------------------------- normalize ----

def metrics_from_run(run: dict) -> dict:
    """Flatten numeric metrics from any run record.

    Handles plain metric dicts (agent runs / notebooks) and legacy
    privacy-workflow records with stage1/stage2/stage3 structure.
    """
    metrics: dict = {}
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


def findings_from_run(run: dict) -> list[str]:
    """Categorical findings (verdicts, risk labels) used for overlap and the
    graph's sub-node tags. Pulled from the record's findings/config or from a
    legacy privacy-workflow stage structure."""
    out = list(run.get("findings") or [])
    cfg = run.get("config")
    if isinstance(cfg, dict):
        out.extend(cfg.get("findings") or [])
    s1 = run.get("stage1") or []
    s2 = run.get("stage2") or {}
    if s1:
        v = s1[-1].get("plausibility_verdict")
        if v:
            out.append(f"plausibility: {str(v).lower()}")
    if s2.get("reid_risk"):
        out.append(f"re-id: {str(s2['reid_risk']).lower()}")
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _timestamp(run: dict) -> str | None:
    ts = run.get("timestamp")
    if not ts:
        started = run.get("started_at")
        if started:
            try:
                ts = datetime.fromtimestamp(float(started), timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                ts = None
    return ts


def unify_record(run: dict, artifact_store=None) -> dict:
    """Normalize any run record (SQLite row, workflow record, notebook record)
    into the unified shape the Experiments UI renders. When `artifact_store` is
    given, artifact ids are resolved to {id, name} pairs for nicer display."""
    kind = run.get("kind") or "privacy_workflow"
    artifacts = run.get("artifacts")
    if artifacts is None:
        artifacts = []
        for aid in run.get("artifact_ids") or []:
            name = aid
            if artifact_store is not None:
                try:
                    art = artifact_store.get(aid)
                    if art is not None:
                        name = art.name
                except Exception:  # noqa: BLE001
                    pass
            artifacts.append({"id": aid, "name": name})
    cfg = run.get("config") or {}
    seed = run.get("seed")
    if seed is None and isinstance(cfg, dict):
        seed = cfg.get("seed")
    fresh = bool(run.get("fresh"))
    if isinstance(cfg, dict) and cfg.get("fresh") is not None:
        fresh = bool(cfg.get("fresh"))
    label = run.get("label") or (
        "notebook" if kind == "notebook" else f"run {run.get('id')}")
    return {
        "id": run.get("id"),
        "kind": kind,
        "label": label,
        "seed": seed,
        "fresh": fresh,
        "timestamp": _timestamp(run),
        "metrics": metrics_from_run(run),
        "findings": findings_from_run(run),
        "artifacts": artifacts,
        "prompt": run.get("prompt") or "",
    }


# ---------------------------------------------------------------- metrics ----

def _norm(run: dict) -> dict:
    """Accept either raw or already-unified records."""
    return run if "metrics" in run and "artifacts" in run else unify_record(run)


def similarity(a: dict, b: dict) -> float:
    """1 - normalized mean-absolute-difference across shared numeric metrics.

    The difference is scaled against the magnitude of the values involved so a
    single shared metric (two-value comparison) still yields a meaningful,
    non-degenerate score.
    """
    ma, mb = metrics_from_run(_norm(a)), metrics_from_run(_norm(b))
    keys = sorted(set(ma) & set(mb))
    if not keys:
        return 0.0
    pairs = [(ma[k], mb[k]) for k in keys]
    lo = min(min(x, y) for x, y in pairs)
    hi = max(max(x, y) for x, y in pairs)
    mad = sum(abs(x - y) for x, y in pairs) / len(pairs)
    scale = max(hi - lo, abs(hi), abs(lo), 1e-9)
    return max(0.0, min(1.0, 1.0 - mad / scale))


def overlap(a: dict, b: dict) -> float:
    """Fraction of shared categorical findings (verdicts, risk labels)."""
    fa = findings_from_run(_norm(a))
    fb = findings_from_run(_norm(b))
    if not fa or not fb:
        return 0.0
    sa, sb = set(fa), set(fb)
    return len(sa & sb) / max(len(sa), len(sb))


def compare_runs(a: dict, b: dict) -> dict:
    """Metric delta table between two run records.

    Returns
        {"a": label_a, "b": label_b, "rows": [{metric, a, b, delta, pct}...],
         "summary": {shared, improved, worsened, unchanged}}
    """
    ma, mb = metrics_from_run(_norm(a)), metrics_from_run(_norm(b))
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

    def label(run: dict) -> str:
        lbl = run.get("label")
        if lbl:
            return str(lbl)
        if run.get("kind") == "notebook":
            return "notebook"
        return f"run {run.get('id')}"

    return {
        "a": label(_norm(a)),
        "b": label(_norm(b)),
        "rows": rows,
        "summary": {
            "shared": len(keys),
            "increased": increased,
            "decreased": decreased,
            "unchanged": unchanged,
        },
    }


# ------------------------------------------------------------------- graph ----

def build_graph(records: list[dict], artifact_store=None) -> dict:
    """Nodes (one per run/record, with headline metrics + findings) and
    similarity/overlap edges, for the Experiments timeline/graph view."""
    nodes = []
    for i, r in enumerate(records):
        u = unify_record(r, artifact_store)
        nodes.append({
            "id": u["id"],
            "index": i,
            "kind": u["kind"],
            "label": u["label"],
            "seed": u["seed"],
            "fresh": u["fresh"],
            "timestamp": u["timestamp"],
            "metrics": u["metrics"],
            "findings": u["findings"],
            "artifacts": u["artifacts"],
            "prompt": u["prompt"],
        })
    edges = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            sim = similarity(records[i], records[j])
            ov = overlap(records[i], records[j])
            if sim > 0.05 or ov > 0:
                edges.append({
                    "source": records[i].get("id"),
                    "target": records[j].get("id"),
                    "similarity": round(sim, 3),
                    "overlap": round(ov, 3),
                })
    return {"nodes": nodes, "edges": edges}
