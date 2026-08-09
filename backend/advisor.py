"""Experiment advisor (round 28).

A deterministic, always-available analysis of one experiment that turns the raw
traceability records (runs, configs, metrics, typed suggestions, learnings,
goals, datasets) into researcher-facing guidance:

    - goal proposal + alignment: whether the goal is defined, how close the best
      run is to the target, and what a sensible goal/target would be if missing
    - missing elements: the fields/artifacts that would make the experiment
      complete (hypothesis, target, plan, dataset tags, metrics, learnings)
    - areas of improvement: pending suggestions grouped by their typed category
      (hyperparameter / data / model / method / finetune / eval / repro)
    - suggested hyperparameters: the best run's config plus concrete next-step
      hyperparameter suggestions
    - data pipeline: the datasets + data tools actually used across runs
    - model selection: which models ran, what's pinned, and whether a pin is
      worth setting
    - finetuning setup: a readiness checklist for a finetune/pre-train run

No LLM call is made — everything is computed from stored records, so the panel
is instant and reproducible.
"""

from __future__ import annotations

from typing import Any


def _goals_for(store, eid: int) -> list[dict]:
    try:
        return store.goals_for_experiment(eid)
    except Exception:  # noqa: BLE001
        return []


def _best_for(store, eid: int, metric: str, higher: bool) -> tuple[float | None, int | None]:
    """Best value + run id for a metric across an experiment's runs."""
    try:
        runs = store.experiment_runs(eid)
    except Exception:  # noqa: BLE001
        runs = []
    best: float | None = None
    best_id: int | None = None
    for r in runs:
        try:
            m = float((r.get("metrics") or {}).get(metric))
        except (TypeError, ValueError):
            continue
        if best is None or (m > best if higher else m < best):
            best, best_id = m, r.get("id")
    return best, best_id


def _count_metrics(runs: list[dict]) -> dict[str, int]:
    """How many runs recorded each metric key (for proposing a goal metric)."""
    counts: dict[str, int] = {}
    for r in runs:
        for k in (r.get("metrics") or {}):
            counts[k] = counts.get(k, 0) + 1
    return counts


def _data_tools(runs: list[dict]) -> list[str]:
    names: set[str] = set()
    for r in runs:
        for s in r.get("tool_sequence") or []:
            n = (s.get("name") or "").lower()
            if any(k in n for k in ("read_csv", "read_excel", "read_parquet",
                                    "read_sql", "load", "kaggle", "dataset",
                                    "open(")):
                names.add(s.get("name") or "")
    return sorted(names)


def _datasets(runs: list[dict]) -> list[str]:
    seen: set[str] = set()
    for r in runs:
        ds = (r.get("dataset") or "").strip()
        if ds:
            seen.add(ds)
    return sorted(seen)


def _models_used(runs: list[dict]) -> list[str]:
    seen: set[str] = set()
    for r in runs:
        m = (r.get("model") or "").strip()
        if m:
            seen.add(m)
    return sorted(seen)


def _suggestion_plan(store, eid: int) -> list[dict]:
    try:
        sugs = store.list_suggestions(experiment_id=eid)
    except Exception:  # noqa: BLE001
        sugs = []
    # Newest first, only still-relevant ones.
    return sorted(sugs, key=lambda s: s.get("id") or 0, reverse=True)


def _goal_proposal(store, exp: dict, runs: list[dict]) -> dict:
    """Propose a goal_metric/target when the experiment lacks one, grounded in
    what was actually measured."""
    metric = exp.get("goal_metric") or ""
    target = exp.get("goal_target")
    higher = bool(exp.get("higher_better", True))
    counts = _count_metrics(runs)
    if not metric and counts:
        # Most-measured metric is the safest proposed goal.
        metric = max(counts, key=counts.get)
    best, best_id = _best_for(store, exp["id"], metric, higher) if metric else (None, None)
    if target is None and best is not None:
        target = round(best * 1.05, 6) if higher else round(best * 0.95, 6)
    pct = None
    if target and best is not None:
        pct = round((best / target) * 100 if higher else (target / best) * 100, 1)
    reached = target is not None and best is not None and (
        best >= target if higher else best <= target)
    return {
        "metric": metric or "",
        "target": target,
        "higher_better": higher,
        "best": best,
        "best_run_id": best_id,
        "pct_target": pct,
        "reached": bool(reached),
        "proposed": (metric or None) if not exp.get("goal_metric") else None,
    }


def _missing_elements(store, exp: dict, runs: list[dict], goal: dict) -> list[dict]:
    """Checklist of what an experiment is missing to be well-formed + trackable."""
    eid = exp["id"]
    missing: list[dict] = []
    if not (exp.get("hypothesis") or "").strip():
        missing.append({"key": "hypothesis", "ok": False,
                        "label": "Hypothesis", "hint": "State the research question you expect this experiment to answer."})
    if not (exp.get("goal_metric") or ""):
        missing.append({"key": "goal_metric", "ok": False,
                        "label": "Goal metric", "hint": goal.get("proposed") and
                        f"No goal metric set — the data suggests '{goal['proposed']}'." or
                        "Pick the metric your goal is measured by (e.g. accuracy)."})
    if exp.get("goal_metric") and exp.get("goal_target") is None:
        missing.append({"key": "goal_target", "ok": False,
                        "label": "Goal target", "hint": "Set a numeric target to measure progress toward the goal."})
    if not (exp.get("plan") or "").strip():
        missing.append({"key": "plan", "ok": False,
                        "label": "Plan", "hint": "Describe configs/variables to try and stopping criteria."})
    if not (exp.get("model") or "").strip():
        missing.append({"key": "model", "ok": False,
                        "label": "Pinned model", "hint": "Pin the model that should run this experiment's turns for consistency."})
    if not runs:
        missing.append({"key": "runs", "ok": False,
                        "label": "Runs", "hint": "No runs yet — ask the agent to run a baseline for this experiment."})
    else:
        if not _datasets(runs):
            missing.append({"key": "dataset", "ok": False,
                            "label": "Dataset tags", "hint": "None of the runs report a dataset — tag one to compare across data."})
        if not _count_metrics(runs):
            missing.append({"key": "metrics", "ok": False,
                            "label": "Metrics", "hint": "No numeric metrics recorded — report metrics with report_metric(...)."})
    try:
        if not store.list_learnings(experiment_id=eid, limit=1):
            missing.append({"key": "learnings", "ok": False,
                            "label": "Learnings", "hint": "No outcomes recorded yet — applied suggestions become learnings."})
    except Exception:  # noqa: BLE001
        pass
    return missing


def _improvements(sugs: list[dict], learnings: list[dict]) -> dict:
    """Pending suggestions grouped by typed category + a short trend summary."""
    pending = [s for s in sugs if s.get("status") in ("pending", "applied")]
    by_category: dict[str, list[dict]] = {}
    for s in pending:
        by_category.setdefault(s.get("category") or "other", []).append(s)
    no_gain = [l for l in learnings if l.get("improved") == 0]
    improved = [l for l in learnings if l.get("improved") == 1]
    return {
        "pending": pending,
        "by_category": by_category,
        "no_gain_count": len(no_gain),
        "improved_count": len(improved),
    }


def _hyperparameters(runs: list[dict], sugs: list[dict]) -> dict:
    """The best run's config plus concrete hyperparameter next-steps."""
    hp = [s for s in sugs if s.get("category") == "hyperparameter" and
          s.get("status") in ("pending", "applied")]
    return {
        "best_config": (runs[0].get("config") or {}) if runs else {},
        "suggestions": hp,
    }


def _finetune_checklist(store, exp: dict, runs: list[dict]) -> dict:
    """Readiness for a finetune/pre-train run on the project's data."""
    items = [
        {"ok": bool((exp.get("model") or "").strip()),
         "label": "Pinned base model (the model you will finetune from)"},
        {"ok": bool(_datasets(runs)) or bool(_project_data(store)),
         "label": "Training data available (dataset tags or project files)"},
        {"ok": bool((exp.get("goal_metric") or "").strip()),
         "label": "Evaluation metric defined (to measure finetune gains)"},
        {"ok": bool(runs),
         "label": "Baseline run exists (to compare finetune against)"},
    ]
    ready = all(i["ok"] for i in items)
    return {"ready": ready, "checklist": items}


def _project_data(store) -> bool:
    try:
        return store.count_project_files() > 0 if hasattr(store, "count_project_files") else False
    except Exception:  # noqa: BLE001
        return False


def experiment_advisor(store, eid: int) -> dict[str, Any]:
    """Compute the full advisor payload for one experiment (deterministic)."""
    exp = store.get_experiment(eid)
    if exp is None:
        raise KeyError(eid)
    runs = store.experiment_runs(eid)
    goal = _goal_proposal(store, exp, runs)
    sugs = _suggestion_plan(store, eid)
    try:
        learnings = store.list_learnings(experiment_id=eid, limit=30)
    except Exception:  # noqa: BLE001
        learnings = []
    return {
        "experiment": {"id": exp["id"], "name": exp["name"]},
        "goal": goal,
        "missing": _missing_elements(store, exp, runs, goal),
        "improvements": _improvements(sugs, learnings),
        "hyperparameters": _hyperparameters(runs, sugs),
        "data": {
            "datasets": _datasets(runs),
            "tools": _data_tools(runs),
            "run_count": len(runs),
        },
        "model": {
            "pinned": exp.get("model") or "",
            "used": _models_used(runs),
        },
        "finetune": _finetune_checklist(store, exp, runs),
        "goals": _goals_for(store, eid),
    }
