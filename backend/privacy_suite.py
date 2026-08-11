"""Deterministic privacy exploit suite.

Runs the full set of privacy experiments (PII scan, re-identification risk,
differential privacy, outlier/anomaly, correlation, bank peer identification)
across a project's datasets and aggregates a detailed markdown report. Pure
pandas/numpy/matplotlib — no LLM loop — so a request like "run all privacy
exploits on these datasets and prepare a detailed report" executes
deterministically instead of looping.
"""

from __future__ import annotations

import asyncio
import time

SUITE_EXPERIMENTS = ["pii_scan", "reid_risk", "dp_privacy", "anomaly",
                     "correlation", "peer"]

# goal metric used in the cross-dataset summary table (matches the catalog).
GOAL_METRICS = {
    "pii_scan": "pii_columns",
    "reid_risk": "k_anonymity_1",
    "dp_privacy": "min_mae",
    "anomaly": "outlier_cols",
    "correlation": "max_abs_corr",
    "peer": "identification_accuracy",
}

MAX_ROWS = 50000  # cap each dataset for the suite (speed + memory safety).


def _load_datasets(project_dir, datasets=None, max_rows: int = MAX_ROWS):
    """Resolve datasets: explicit list, else every data file in the project.
    Each is loaded (deterministically sampled when huge). Returns [(name, df)]."""
    from .experiment_planner import is_dataset_file, load_dataset
    paths = []
    if datasets:
        paths = [project_dir / d for d in datasets
                 if (project_dir / d).exists()]
    else:
        paths = sorted(p for p in project_dir.iterdir()
                       if p.is_file() and is_dataset_file(p.name)
                       and not p.name.startswith("."))
    out = []
    for p in paths:
        try:
            df = load_dataset(p)
        except Exception:  # noqa: BLE001
            continue
        if len(df) > max_rows:
            try:
                df = df.sample(n=max_rows, random_state=42)
            except Exception:  # noqa: BLE001
                pass
        out.append((p.name, df))
    return out


def _run_one(experiment_id: str, df, seed: int):
    """Run one registered experiment on a dataset (sync, worker-thread safe)."""
    from . import experiment_planner as ep
    defn = ep.EXPERIMENT_REGISTRY.get(experiment_id)
    if defn is None:
        return {"report": f"_{experiment_id}: not registered._",
                "figures": {}, "metrics": {}, "n": 0}
    try:
        res = defn["run"](df, seed=seed)
        report = defn["render_report"](res)
        figures = (defn["render_figures"](res)
                   if "render_figures" in defn else {})
        metrics = res.get("metrics") or ep._default_metrics(res)
        return {"report": report or "", "figures": figures,
                "metrics": metrics or {}, "n": res.get("n")}
    except Exception as e:  # noqa: BLE001
        return {"report": f"**{experiment_id} failed**: {type(e).__name__}: {e}",
                "figures": {}, "metrics": {}, "n": 0, "error": str(e)}


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✓" if v else "✗"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def build_suite_report(results: dict) -> str:
    """Aggregate markdown report: summary table + per-dataset sections."""
    datasets = list(results)
    lines = ["# Privacy Exploit Suite — Report", "",
             f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M UTC')}",
             f"- **Datasets:** {', '.join(datasets) or '—'}",
             f"- **Experiments:** {', '.join(SUITE_EXPERIMENTS)}",
             "", "## Summary (goal metric per dataset)", "",
             "| Dataset | " + " | ".join(SUITE_EXPERIMENTS) + " |",
             "|---|" + "---|" * len(SUITE_EXPERIMENTS)]
    for ds in datasets:
        row = [f"`{ds}`"]
        for exp in SUITE_EXPERIMENTS:
            m = (results[ds].get(exp) or {}).get("metrics") or {}
            gm = GOAL_METRICS.get(exp)
            row.append(_fmt(m.get(gm)))
        lines.append("| " + " | ".join(row) + " |")
    for ds in datasets:
        lines += ["", f"## {ds}", ""]
        for exp in SUITE_EXPERIMENTS:
            r = results[ds].get(exp) or {}
            lines += [f"### {exp}", "", (r.get("report") or "_no result_").strip(), ""]
    return "\n".join(lines)


async def run_privacy_suite(rt, datasets=None, seed: int = 42,
                            progress=None) -> dict:
    """Run the privacy suite across datasets (worker threads) and persist the
    figures + report as artifacts. Returns {report, datasets, results, artifacts}."""
    from .artifacts.store import Artifact
    from .experiment_planner import EXPERIMENT_REGISTRY
    data = _load_datasets(rt.dir, datasets)
    results = {}
    total = len(data) * len(SUITE_EXPERIMENTS)
    step = 0
    for name, df in data:
        results[name] = {}
        for exp in SUITE_EXPERIMENTS:
            step += 1
            if progress:
                await progress(step, total,
                               f"Running {exp} on {name}")
            results[name][exp] = await asyncio.to_thread(
                _run_one, exp, df, seed)
    report = build_suite_report(results)

    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        env = {}
    figure_ids = []
    for ds, ds_res in results.items():
        for exp, r in ds_res.items():
            for fname, png in (r.get("figures") or {}).items():
                art = Artifact(kind="figure",
                               name=f"{ds}-{exp}-{fname}",
                               description=f"{exp} figure for {ds}",
                               code="privacy_suite", env=env,
                               message_id="", run_id="", data_type="png")
                try:
                    rt.artifacts.add_artifact(art, data=png, data_type="png")
                    figure_ids.append(art.id)
                except Exception:  # noqa: BLE001
                    pass
    art = Artifact(kind="report", name="privacy-suite-report",
                   description="Aggregate privacy exploit suite report",
                   code="privacy_suite", env=env,
                   message_id="", run_id="", data_type="text")
    report_id = ""
    try:
        rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
        report_id = art.id
    except Exception:  # noqa: BLE001
        pass
    return {"report": report, "datasets": [n for n, _ in data],
            "results": results, "report_id": report_id,
            "figure_ids": figure_ids}
