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

import difflib
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
            data_type = None
            if artifact_store is not None:
                try:
                    art = artifact_store.get(aid)
                    if art is not None:
                        name = art.name
                        data_type = art.data_type
                except Exception:  # noqa: BLE001
                    pass
            artifacts.append({"id": aid, "name": name, "data_type": data_type})
    cfg = run.get("config") or {}
    seed = run.get("seed")
    if seed is None and isinstance(cfg, dict):
        seed = cfg.get("seed")
    fresh = bool(run.get("fresh"))
    if isinstance(cfg, dict) and cfg.get("fresh") is not None:
        fresh = bool(cfg.get("fresh"))
    label = run.get("label") or (
        "notebook" if kind == "notebook" else f"run {run.get('id')}")
    tools = []
    mcp = action = ""
    for t in run.get("tool_sequence") or []:
        name = (t or {}).get("name") or ""
        if name:
            if "__" in name:
                m, _, a = name.partition("__")
            else:
                m, a = "core", name
            mcp, action = m, a  # the last executed tool labels the run
            tools.append({"name": name, "mcp": m, "action": a,
                          "ok": bool((t or {}).get("ok"))})
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
        "experiment_id": run.get("experiment_id"),
        "model": run.get("model") or "",
        "mcp": mcp,
        "action": action,
        "tools": tools,
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


def run_diff(a: dict, b: dict) -> dict:
    """What changed between two runs: config, metrics, tool usage and code.

    Operates on raw run records (store.get_run / add_run output) which retain
    full `config` / `tool_sequence` and (round 4) full `code` per tool. Returns
        {"a": label_a, "b": label_b,
         "config": {"added": [...], "removed": [...], "changed": [[key, va, vb]...]},
         "metrics": compare_runs(...) result,
         "tools": {"added": [...], "removed": [...], "failed": [...], "used": [...]},
         "code": {"available": bool, "diffs": [{tool, added, removed, patch}]},
         "prompt": {"a": prompt_a, "b": prompt_b}}
    """
    def label(run: dict) -> str:
        lbl = run.get("label")
        if lbl:
            return str(lbl)
        return f"run {run.get('id')}"

    def cfg(run: dict) -> dict:
        c = run.get("config")
        return dict(c) if isinstance(c, dict) else {}

    ca, cb = cfg(a), cfg(b)
    added = sorted(k for k in cb if k not in ca)
    removed = sorted(k for k in ca if k not in cb)
    changed = [[k, ca[k], cb[k]] for k in sorted(set(ca) & set(cb))
               if ca[k] != cb[k]]

    def tools(run: dict) -> list[dict]:
        seq = run.get("tool_sequence")
        return list(seq) if isinstance(seq, list) else []

    ta, tb = {t.get("name") for t in tools(a)}, {t.get("name") for t in tools(b)}
    tool_added = sorted(tb - ta)
    tool_removed = sorted(ta - tb)
    failed = sorted({t.get("name") for t in tools(a) + tools(b) if not t.get("ok")})

    # Full-code diff (round 4): unified diff per tool name via difflib.
    code_a = {c.get("name"): c.get("code") or "" for c in (a.get("code") or [])}
    code_b = {c.get("name"): c.get("code") or "" for c in (b.get("code") or [])}
    code_diffs = []
    for tool in sorted(set(code_a) | set(code_b)):
        src_a = (code_a.get(tool) or "").splitlines()
        src_b = (code_b.get(tool) or "").splitlines()
        if src_a == src_b:
            continue
        if tool in code_a and tool not in code_b:
            patch = "\n".join([f"- {l}" for l in src_a]) or "- (empty)"
            code_diffs.append({"tool": tool, "added": 0, "removed": len(src_a),
                               "patch": patch})
            continue
        if tool not in code_a and tool in code_b:
            patch = "\n".join([f"+ {l}" for l in src_b]) or "+ (empty)"
            code_diffs.append({"tool": tool, "added": len(src_b), "removed": 0,
                               "patch": patch})
            continue
        diff_lines = list(difflib.unified_diff(
            src_a, src_b, fromfile=f"{tool}@{label(a)}", tofile=f"{tool}@{label(b)}",
            lineterm=""))
        added_n = removed_n = 0
        for dl in diff_lines[2:]:
            if dl.startswith("+"):
                added_n += 1
            elif dl.startswith("-"):
                removed_n += 1
        code_diffs.append({"tool": tool, "added": added_n, "removed": removed_n,
                           "patch": "\n".join(diff_lines)})

    return {
        "a": label(a),
        "b": label(b),
        "config": {"added": added, "removed": removed, "changed": changed},
        "metrics": compare_runs(a, b),
        "tools": {"added": tool_added, "removed": tool_removed,
                  "failed": failed, "used": sorted(tb)},
        "code": {"available": bool(code_a) or bool(code_b),
                 "diffs": code_diffs},
        "prompt": {"a": a.get("prompt") or "", "b": b.get("prompt") or ""},
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
            "experiment_id": u["experiment_id"],
            "model": u["model"],
            "mcp": u["mcp"],
            "action": u["action"],
            "tools": u["tools"],
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


# ------------------------------------------------------ git-flow branches ----

def build_branch_graph(runs: list[dict], experiments: list[dict]) -> dict:
    """Git-flow branching history: runs as commit-like nodes, parent→child
    edges, experiments as branches.

    Parentage comes from each run's explicit `parent_run_id` (improve loops,
    reruns, fresh reruns). When missing it is inferred: runs within the same
    experiment chain chronologically (children branch off the experiment's
    current tip); standalone runs of the same `kind` (fresh reruns, notebook
    reruns) chain too. Returns {nodes, edges, experiments, tips}.
    """
    exp_by_id = {e.get("id"): e for e in experiments}
    # Deterministic experiment ordering for stable branch colors/lanes.
    exp_order = {eid: i for i, eid in enumerate(e for e in exp_by_id)}

    nodes = []
    by_id: dict[int, dict] = {}
    for r in sorted(runs, key=lambda x: (x.get("started_at") or 0)):
        eid = r.get("experiment_id")
        exp = exp_by_id.get(eid) if eid is not None else None
        metrics = metrics_from_run(r)
        goal_metric = (exp or {}).get("goal_metric")
        goal_value = metrics.get(goal_metric) if goal_metric else None
        review = r.get("review") or {}
        findings = findings_from_run(r)
        for f in review.get("findings") or []:
            msg = f.get("message") or (f if isinstance(f, str) else "")
            if msg and msg not in findings:
                findings.append(msg)
        nodes.append({
            "id": r.get("id"),
            "label": r.get("label") or r.get("kind") or f"run {r.get('id')}",
            "kind": r.get("kind") or "agent_run",
            "status": r.get("status"),
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "experiment_id": eid,
            "experiment_name": (exp or {}).get("name"),
            "experiment_branch": exp_order.get(eid) if eid is not None else None,
            "config": r.get("config") or {},
            "metrics": metrics,
            "goal_metric": goal_metric,
            "goal_value": goal_value,
            "artifacts": len(r.get("artifact_ids") or []),
            "tools": len(r.get("tool_sequence") or []),
            "parent_run_id": r.get("parent_run_id"),
            "timestamp": _timestamp(r),
            # narrative fields for the branch-history detail panel
            "objective": (r.get("prompt") or "")[:600],
            "summary": (r.get("reply") or "")[:800],
            "findings": findings[:20],
            "notes": [(s.get("title") or s.get("action") or "")
                      for s in (review.get("suggestions") or [])][:10],
            # round-4 provenance: snapshot commit + run-time environment
            "git_commit": r.get("git_commit") or "",
            "env": r.get("env") or {},
        })
        by_id[r.get("id")] = r

    # ---- edges -----------------------------------------------------------
    edges = []
    chain_tip: dict[object, int] = {}   # experiment_id / kind -> last run id
    for n in nodes:
        rid = n["id"]
        parent = n.get("parent_run_id")
        if parent is not None and parent in by_id:
            edges.append({"parent": parent, "child": rid})
            # track per-experiment tip for inference on other roots
            key = ("exp", n.get("experiment_id")) if n.get("experiment_id") is not None \
                else ("kind", n.get("kind"))
            chain_tip[key] = rid
            continue
        # Infer a parent when none is explicit.
        inferred = None
        if n.get("experiment_id") is not None:
            key = ("exp", n.get("experiment_id"))
            inferred = chain_tip.get(key)
        if inferred is None and n.get("experiment_id") is None:
            key = ("kind", n.get("kind"))
            inferred = chain_tip.get(key)
        if inferred is not None:
            edges.append({"parent": inferred, "child": rid})
            n["parent_run_id"] = inferred
        # advance the chain tip for this experiment/kind
        key = ("exp", n.get("experiment_id")) if n.get("experiment_id") is not None \
            else ("kind", n.get("kind"))
        chain_tip[key] = rid

    child_count = {}
    for e in edges:
        child_count[e["parent"]] = child_count.get(e["parent"], 0) + 1
    tips = [n["id"] for n in nodes if child_count.get(n["id"], 0) == 0]

    return {
        "nodes": nodes,
        "edges": edges,
        "experiments": [
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "goal_metric": e.get("goal_metric"),
                "goal_target": e.get("goal_target"),
                "higher_better": e.get("higher_better"),
                "status": e.get("status"),
                "hypothesis": (e.get("hypothesis") or "")[:600],
                "plan": (e.get("plan") or "")[:800],
                "run_count": len([n for n in nodes
                                  if n.get("experiment_id") == e.get("id")]),
            }
            for e in experiments
        ],
        "tips": tips,
    }


# -------------------------------------------------------------- leaderboard ----

def rank_runs(runs: list[dict], metric: str, higher_better: bool = True,
              limit: int = 50, goal_target: float | None = None) -> dict:
    """Rank runs by a numeric metric (multi-run leaderboard) for the UI.

    Returns {"metric", "higher_better", "goal_target", "best", "rows": [...]}
    where each row is {rank, run_id, label, config, metric, delta_best,
    pct_best} plus, when a goal_target is given, distance-to-target
    (to_target, pct_target). Runs that do not carry the metric are skipped.
    """
    rows = []
    for r in runs:
        m = (r.get("metrics") or {}).get(metric)
        if m is None:
            continue
        try:
            m = float(m)
        except (TypeError, ValueError):
            continue
        rows.append({
            "run_id": r.get("id"),
            "label": r.get("label") or "",
            "config": r.get("config") or {},
            "metric": m,
            "prompt": r.get("prompt") or "",
        })
    if not rows:
        return {"metric": metric, "higher_better": higher_better,
                "goal_target": goal_target, "best": None, "rows": []}
    rows.sort(key=lambda x: x["metric"], reverse=higher_better)
    best = rows[0]["metric"]
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
        row["delta_best"] = row["metric"] - best
        row["pct_best"] = ((row["metric"] - best) / best * 100) if best else 0.0
        if goal_target is not None:
            row["to_target"] = goal_target - row["metric"]
            row["pct_target"] = ((row["metric"] / goal_target) * 100) if goal_target else 0.0
    return {"metric": metric, "higher_better": higher_better,
            "goal_target": goal_target, "best": best, "rows": rows[:limit]}
