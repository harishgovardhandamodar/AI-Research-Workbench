"""Run/experiment routes: agent-run traceability, structured experiments, goal
tracking, lab-notebook reports, run comparison, and figure regeneration."""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..artifacts.store import Artifact
from ..experiments import build_graph, compare_runs, rank_runs, unify_record
from ..llm import LLMError
from ..state import get_runtime

router = APIRouter()


@router.get("/api/projects/{name}/runs")
async def project_runs(name: str, limit: int = 50):
    """Every agent turn recorded as a run (traceability)."""
    return {"runs": get_runtime(name).store.list_runs(limit)}


@router.get("/api/projects/{name}/runs/{rid}")
async def project_run(name: str, rid: int):
    run = get_runtime(name).store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@router.get("/api/projects/{name}/experiments")
async def project_experiments(name: str):
    """Structured experiments (families of runs) for this project."""
    return {"experiments": get_runtime(name).store.list_experiments()}


@router.get("/api/projects/{name}/experiments/history")
async def project_experiments_history(name: str, limit: int = 50):
    """Every run in this project as a unified traceability record (metrics,
    findings, artifacts) for the Experiments timeline/graph UI."""
    rt = get_runtime(name)
    records = rt.store.list_runs(limit)
    return {"experiments": [unify_record(r, rt.artifacts) for r in records]}


@router.get("/api/projects/{name}/experiments/graph")
async def project_experiments_graph(name: str):
    """Graph view: one node per run + similarity/overlap edges between runs."""
    rt = get_runtime(name)
    return build_graph(rt.store.list_runs(), rt.artifacts)


@router.post("/api/projects/{name}/experiments")
async def create_project_experiment(name: str, body: dict):
    """Create an experiment (name + optional hypothesis/goal/plan) and return it."""
    store = get_runtime(name).store
    name_str = (body.get("name") or "").strip()
    if not name_str:
        raise HTTPException(status_code=400, detail="name required")
    try:
        target = float(body["goal_target"]) if body.get("goal_target") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="goal_target must be a number")
    eid = store.create_experiment(
        name_str, body.get("hypothesis") or "",
        body.get("goal_metric") or "", target,
        bool(body.get("higher_better", True)),
        plan=body.get("plan") or "")
    return {"experiment": store.get_experiment(eid)}


@router.get("/api/projects/{name}/experiments/{eid}")
async def project_experiment(name: str, eid: int):
    """One experiment with its runs (config + metrics per run)."""
    store = get_runtime(name).store
    exp = store.get_experiment(eid)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    exp["runs"] = store.experiment_runs(eid)
    return {"experiment": exp}


EXPERIMENT_STATUSES = ("active", "completed", "cancelled")


@router.patch("/api/projects/{name}/experiments/{eid}")
async def update_project_experiment(name: str, eid: int, body: dict):
    """Update an experiment's lifecycle status (active -> completed/cancelled)."""
    store = get_runtime(name).store
    exp = store.get_experiment(eid)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    status = (body.get("status") or "").strip()
    if status not in EXPERIMENT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {', '.join(EXPERIMENT_STATUSES)}")
    store.update_experiment_status(eid, status)
    return {"experiment": store.get_experiment(eid)}


@router.post("/api/projects/{name}/experiments/run-obfuscation")
async def run_obfuscation_experiments(name: str, body: dict):
    """Run the bank-transaction obfuscation scenario suite and record each
    scenario as a run under a reusable "obfuscation (bank)" experiment.

    Generates synthetic bank-transaction data, runs the 9 obfuscation threat
    scenarios, and records one run per scenario with its metrics, a figure
    artifact (PNG) and a masked-vs-raw transactions table artifact, so the
    Experiments panel can show results and transactions side by side.

    Body: {"dataset": "bank", "n_rows": 2000, "seed": 42}.
    """
    import sys
    import time

    from ..paths import ROOT

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from examples.obfuscation import bank_experiments as bexp
        from examples.obfuscation.bank_transactions_data import (
            generate_bank_transactions,
        )
    except ImportError as exc:  # pragma: no cover - env check
        raise HTTPException(status_code=500,
                            detail=f"bank obfuscation examples unavailable: {exc}")

    rt = get_runtime(name)
    n_rows = int(body.get("n_rows") or 2000)
    seed = int(body.get("seed") or 42)

    df = await asyncio.to_thread(generate_bank_transactions, n_rows=n_rows, seed=seed)
    results = await asyncio.to_thread(bexp.run_all, df)

    store = rt.store
    exps = store.list_experiments()
    eid = next((e["id"] for e in exps
                if e["name"] == "obfuscation (bank)"), None)
    if eid is None:
        eid = store.create_experiment(
            "obfuscation (bank)",
            "Data-obfuscation threat scenarios on synthetic bank transactions",
            "reduction_pct", None, True,
            plan=f"Run the 9 obfuscation scenarios over {n_rows:,} generated "
                 f"bank transactions (seed={seed}) and measure risk reduction "
                 "per technique.")
    else:
        store.update_experiment_status(eid, "active")

    now = time.time()
    run_ids = []
    for r in results:
        art_ids = []
        fig = r.get("fig")
        if fig is not None:
            png = await asyncio.to_thread(bexp.fig_to_png, fig)
            art = Artifact(kind="figure",
                           name=_artifact_name(r.get("title"), "figure"),
                           description=r.get("technique", ""),
                           code=f"examples.obfuscation.bank_experiments::"
                                f"{r.get('title')}",
                           env={"dataset": "bank", "seed": seed, "n_rows": n_rows},
                           message_id="")
            rt.artifacts.add_artifact(art, data=png, data_type="png")
            art_ids.append(art.id)
        table_md = r.get("table_md") or ""
        if table_md:
            art = Artifact(kind="text",
                           name=_artifact_name(r.get("title"), "transactions"),
                           description="masked vs raw bank transactions",
                           code="examples.obfuscation.bank_experiments::"
                                f"{r.get('title')}",
                           env={"dataset": "bank", "seed": seed, "n_rows": n_rows},
                           message_id="")
            rt.artifacts.add_artifact(art, data=table_md.encode(),
                                      data_type="text")
            art_ids.append(art.id)
        rid = store.add_run(
            prompt=(f"obfuscation scenario: {r.get('title')} "
                    f"[{r.get('technique', '')}] on {n_rows:,} synthetic "
                    f"bank transactions"),
            reply=table_md,
            status="error" if r.get("error") else "done",
            started_at=now - 0.5, finished_at=now,
            artifact_ids=art_ids,
            metrics={k: _jsonable(v) for k, v in (r.get("metrics") or {}).items()},
            experiment_id=eid,
            config={"dataset": "bank", "seed": seed, "n_rows": n_rows,
                    "technique": r.get("technique", "")},
            label=r.get("title"),
            kind="obfuscation")
        run_ids.append(rid)

    return {"experiment": store.get_experiment(eid),
            "runs": [store.get_run(rid) for rid in run_ids],
            "count": len(run_ids)}


def _artifact_name(title: str, suffix: str) -> str:
    """Slugify a scenario title into an artifact name."""
    base = "".join(c for c in (title or "").lower() if c.isalnum() or c == " ")
    base = "_".join(base.split())
    return f"{base}_{suffix}" if base else suffix


def _jsonable(value):
    """Coerce numpy/pandas scalars to native Python for the JSON store."""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    try:
        import numpy as np  # noqa: PLC0415

        if isinstance(value, (np.bool_, np.integer, np.floating)):
            return value.item()
    except Exception:  # noqa: BLE001
        pass
    return str(value)


@router.post("/api/projects/{name}/experiments/{eid}/link")
async def link_run_to_experiment(name: str, eid: int, body: dict):
    """Attach an existing run to an experiment (optionally with its config)."""
    store = get_runtime(name).store
    rid = body.get("run_id")
    run = store.get_run(int(rid)) if str(rid).isdigit() else None
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    store.set_run_experiment(int(rid), eid, body.get("config"))
    return {"run": store.get_run(int(rid))}


@router.get("/api/projects/{name}/experiments/{eid}/ranking")
async def project_experiment_ranking(name: str, eid: int,
                                     metric: str = "", limit: int = 50):
    """Leaderboard: rank an experiment's runs by a metric (default: its goal
    metric), higher_better-aware, with delta-vs-best per run."""
    rt = get_runtime(name)
    store = rt.store
    exp = store.get_experiment(eid)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    m = metric or (exp.get("goal_metric") or "")
    if not m:
        return {"ranking": rank_runs([], ""), "experiment": exp}
    higher = bool(exp.get("higher_better", True))
    return {"ranking": rank_runs(store.experiment_runs(eid), m, higher, limit),
            "experiment": exp}


async def build_run_report(rt, run: dict) -> str:
    """Assemble a lab-notebook markdown report for an agent run.

    Deterministic sections (prompt, metrics, tool trace, artifacts, review) are
    always present; an LLM executive summary is prepended when available.
    """
    lines = [
        f"# Run #{run['id']} — report",
        "",
        f"- **Prompt**: {run.get('prompt') or '—'}",
        f"- **Status**: {run.get('status')}",
        f"- **Started**: {_fmt_ts(run.get('started_at'))}",
        f"- **Finished**: {_fmt_ts(run.get('finished_at'))}",
    ]
    metrics = run.get("metrics") or {}
    if metrics:
        lines += ["", "## Metrics", "",
                  "| metric | value |", "|---|---|"]
        for k in sorted(metrics):
            lines.append(f"| {k} | {metrics[k]:.6g} |")
    seq = run.get("tool_sequence") or []
    if seq:
        lines += ["", "## Tool trace", ""]
        for t in seq:
            mark = "ok" if t.get("ok") else "FAILED"
            lines.append(f"- `{t.get('name')}` ({mark}) — args: `{t.get('args') or ''}`")
            lines.append(f"  - result: `{(t.get('result') or '').strip() or '(empty)'}`")
    arts = run.get("artifact_ids") or []
    if arts:
        lines += ["", "## Artifacts", ""]
        for aid in arts:
            art = rt.artifacts.get(aid)
            if art:
                lines.append(f"- [{aid}]({art.url or f'/artifacts/{aid}'}) — {art.name} ({art.kind})")
            else:
                lines.append(f"- `{aid}` (not found)")
    review = run.get("review") or {}
    findings = review.get("findings") or []
    suggestions = review.get("suggestions") or []
    if findings or suggestions:
        lines += ["", "## Review", ""]
        for f in findings:
            lines.append(f"- **{f.get('severity')}**: {f.get('message')}")
        if suggestions:
            lines += ["", "### Suggested next steps", ""]
            for s in suggestions:
                if isinstance(s, dict):
                    title = s.get("title") or s.get("action") or "suggestion"
                    lines.append(f"- **{title}**" +
                                 (f": {s['action']}" if s.get("action") and s["action"] != title else ""))
                else:
                    lines.append(f"- {s}")
    base = "\n".join(lines)

    # LLM-assisted executive summary (best effort).
    try:
        summary = await _summarize_run(rt, run, base)
    except Exception:  # noqa: BLE001
        summary = ""
    if summary:
        base = f"## Executive summary\n\n{summary}\n\n" + base
    return base


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return str(ts)


async def _summarize_run(rt, run: dict, report: str) -> str:
    prompt = (
        "You are writing the executive summary of a lab-notebook report. Given the "
        "run facts below, write 3-5 concise sentences: what was tried, the key "
        "metrics, and whether the result is good or needs improvement. No markdown "
        "headings, just plain sentences.\n\n"
        f"Prompt: {run.get('prompt', '')}\n\nReport:\n{report[:4000]}")
    resp = await rt.llm.complete([{"role": "user", "content": prompt}],
                                 temperature=0.2, tools=None)
    text = (resp.get("content") or "").strip()
    return text[:2000] if text else ""


@router.post("/api/projects/{name}/runs/{rid}/report")
async def project_run_report(name: str, rid: int):
    """Generate a lab-notebook markdown report for a run and save it as an artifact."""
    rt = get_runtime(name)
    run = rt.store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    report = await build_run_report(rt, run)
    env = {}
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        pass
    mid = rt.store.add_message("assistant", report,
                               {"tags": ["report", f"run #{rid}"]})
    art = Artifact(kind="text", name=f"run-{rid}-report",
                   description=f"Auto-generated lab-notebook report for run #{rid}",
                   code="# auto-generated report", env=env,
                   run_id=str(rid), message_id=str(mid))
    rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
    return {"report": report, "artifact_id": art.id, "message_id": mid}


@router.get("/api/projects/{name}/goals")
async def project_goals(name: str):
    return {"goals": get_runtime(name).store.list_goals()}


@router.post("/api/projects/{name}/goals")
async def project_goals_add(name: str, body: dict):
    metric = str(body.get("metric", "")).strip()
    if not metric:
        raise HTTPException(status_code=400, detail="metric is required")
    try:
        target = float(body.get("target", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target must be numeric")
    rt = get_runtime(name)
    eid = body.get("experiment_id")
    eid = int(eid) if str(eid).isdigit() else None
    if eid is not None and rt.store.get_experiment(eid) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    rt.store.add_goal(metric, target, bool(body.get("higher_better", True)),
                      str(body.get("label", "")), experiment_id=eid)
    return {"goals": rt.store.list_goals()}


@router.delete("/api/projects/{name}/goals/{metric}")
async def project_goals_delete(name: str, metric: str, experiment_id: str = ""):
    rt = get_runtime(name)
    eid = int(experiment_id) if str(experiment_id).isdigit() else None
    deleted = rt.store.delete_goal(metric, eid)
    if not deleted:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"goals": rt.store.list_goals()}


@router.get("/api/projects/{name}/compare")
async def project_compare(name: str, run_a: str = "", run_b: str = ""):
    """Metric delta between two runs (any two records from this project)."""
    if not run_a or not run_b:
        raise HTTPException(status_code=400, detail="run_a and run_b are required")
    rt = get_runtime(name)

    def resolve(ref: str):
        # Run ids from this project's runs table are integers.
        return rt.store.get_run(int(ref)) if str(ref).isdigit() else None

    ra, rb = resolve(run_a), resolve(run_b)
    if ra is None or rb is None:
        raise HTTPException(status_code=404,
                            detail=f"could not resolve run ids: {run_a!r}, {run_b!r}")
    return {"comparison": compare_runs(ra, rb)}


REGEN_PROMPT = """\
You are modifying Python code in a scientific workbench. Here is the original code
that produced a figure:

```python
{code}
```

The user wants this change: "{instruction}"

Respond with ONLY the complete, modified Python code in a single fenced code block.
Do not explain. Preserve any existing variable names so kernel state stays consistent.
"""


@router.post("/api/projects/{name}/regenerate")
async def regenerate(name: str, body: dict):
    rt = get_runtime(name)
    artifact_id = body.get("artifact_id", "")
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return JSONResponse({"error": "instruction required"}, status_code=400)
    art = rt.artifacts.get(artifact_id)
    if not art:
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    code = art.code
    try:
        resp = await rt.llm.complete(
            [{"role": "system",
              "content": REGEN_PROMPT.format(code=code, instruction=instruction)},
             {"role": "user", "content": "Output the complete modified code now."}],
            temperature=0.1,
        )
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    text = resp.get("content", "")
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    new_code = m.group(1).strip() if m else text.strip()
    if not new_code:
        return JSONResponse({"error": "model returned no code"}, status_code=502)
    env = await rt.kernels.get_env()
    kernel_resp = await rt.kernels.python.run_code(new_code)
    new_art = None
    figs = kernel_resp.get("figures") or []
    if figs:
        data = base64.b64decode(figs[0])
        new_art = Artifact(kind="figure", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=data, data_type="png")
    else:
        new_art = Artifact(kind="text", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=(kernel_resp.get("output") or "").encode(),
                                  data_type="text")
    return {"artifact": new_art.to_dict(),
            "output": kernel_resp.get("output", ""),
            "error": kernel_resp.get("error", "")}
