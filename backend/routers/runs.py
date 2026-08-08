"""Run/experiment routes: agent-run traceability, structured experiments, goal
tracking, lab-notebook reports, run comparison, and figure regeneration."""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from ..artifacts.store import Artifact
from ..experiments import (build_graph, compare_campaigns, compare_experiments,
                           compare_runs, compare_runs_many, rank_runs, run_diff,
                           unify_record)
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


@router.delete("/api/projects/{name}/messages/{mid}")
async def project_message_delete(name: str, mid: int):
    """Delete a single chat message so the user can curate the conversation."""
    rt = get_runtime(name)
    if not rt.store.delete_message(mid):
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True, "messages": rt.store.list_messages()}


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


@router.get("/api/projects/{name}/experiments/branches")
async def project_experiments_branches(name: str):
    """Git-flow branching history: runs as commit-like nodes, parent→child
    edges, experiments as branches, run parameters (config/metrics) per node."""
    from ..experiments import build_branch_graph

    rt = get_runtime(name)
    return build_branch_graph(rt.store.list_runs(),
                              rt.store.list_experiments())


@router.get("/api/projects/{name}/experiments/compare")
async def project_experiments_compare(name: str):
    """Leaderboard of experiments by their goal metric's best run."""
    rt = get_runtime(name)
    return compare_experiments(rt.store, rt.store.list_experiments())


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
        plan=body.get("plan") or "",
        model=body.get("model") or "")
    return {"experiment": store.get_experiment(eid)}


@router.get("/api/projects/{name}/experiments/focus")
async def project_experiment_focus(name: str):
    """The currently focused experiment (drives context steering + run tagging)."""
    store = get_runtime(name).store
    fid = store.get_setting("focus_experiment_id", "")
    if str(fid).isdigit() and store.get_experiment(int(fid)) is not None:
        return {"focus_id": int(fid)}
    return {"focus_id": None}


@router.post("/api/projects/{name}/experiments/focus")
async def set_project_experiment_focus(name: str, body: dict):
    """Set (id) or clear (null) the focused experiment."""
    store = get_runtime(name).store
    fid = body.get("id")
    if fid is not None:
        fid = int(fid)
        if store.get_experiment(fid) is None:
            raise HTTPException(status_code=404, detail="experiment not found")
        store.set_setting("focus_experiment_id", str(fid))
        return {"focus_id": fid}
    store.set_setting("focus_experiment_id", "")
    return {"focus_id": None}


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
    """Update an experiment: its lifecycle status, or its objective fields
    (name/hypothesis/goal_metric/goal_target/higher_better/plan) so the user can
    refine the goal without recreating the experiment."""
    from ..store import _UNSET

    store = get_runtime(name).store
    exp = store.get_experiment(eid)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    status = (body.get("status") or "").strip()
    if status:
        if status not in EXPERIMENT_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {', '.join(EXPERIMENT_STATUSES)}")
        store.update_experiment_status(eid, status)
        return {"experiment": store.get_experiment(eid)}

    target = body.get("goal_target")
    target = _UNSET if target is None and "goal_target" in body else target
    if target is not _UNSET and target is not None:
        try:
            target = float(target)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="goal_target must be a number")
    goal_metric = body.get("goal_metric")
    if "goal_metric" not in body:
        goal_metric = None
    else:
        goal_metric = (goal_metric or "").strip()
        if goal_metric and target is None and "goal_target" not in body:
            target = exp["goal_target"]  # keep existing target on metric edit
    # Editing only the target must keep the experiment's current metric.
    effective_metric = goal_metric if "goal_metric" in body else exp["goal_metric"]
    if target is not _UNSET and target is not None and not (effective_metric or "").strip():
        raise HTTPException(status_code=400,
                            detail="goal_target requires a goal_metric")
    store.update_experiment(
        eid,
        name=body.get("name"),
        hypothesis=body.get("hypothesis"),
        goal_metric=goal_metric,
        goal_target=target,
        higher_better=body.get("higher_better"),
        plan=body.get("plan"),
        model=body.get("model"),
    )
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
    fig_by_title = {}
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
            fig_by_title[r.get("title")] = art.id
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

    _record_obfuscation_chat(rt, eid, df, results, fig_by_title, seed, n_rows)

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


def _record_obfuscation_chat(rt, eid: int, df, results: list[dict],
                             fig_by_title: dict, seed: int, n_rows: int) -> None:
    """Write the bank-obfuscation run as chat messages so the results and
    transactions appear in the chat window (not just the Experiments panel).

    One user turn summarises the run; one assistant message per scenario
    carries the figure, its metrics and the masked-vs-raw transactions table.
    """
    store = rt.store
    store.add_message(
        "user",
        f"Run the bank-transaction obfuscation scenario suite on "
        f"{n_rows:,} synthetic transactions (seed={seed}).",
        {"tags": ["obfuscation"], "experiment_id": eid})

    for r in results:
        title = r.get("title") or "?"
        technique = r.get("technique", "")
        metrics = r.get("metrics") or {}
        lines = [f"### {title}", ""]
        if technique:
            lines.append(f"**Technique:** {technique}")
        if metrics:
            lines += ["", "| metric | value |", "|---|---|"]
            for k, v in metrics.items():
                lines.append(f"| {k} | {v} |")
        fig_id = fig_by_title.get(title)
        if fig_id:
            lines += ["", f"![{title}](/artifacts/{fig_id})"]
        table_md = r.get("table_md") or ""
        if table_md:
            lines += ["", table_md]
        if r.get("error"):
            lines += ["", f"> error: {r['error']}"]
        store.add_message("assistant", "\n".join(lines),
                          {"tags": ["obfuscation"], "experiment_id": eid})


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
    return {"ranking": rank_runs(store.experiment_runs(eid), m, higher, limit,
                                 goal_target=exp.get("goal_target")),
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
    env = run.get("env") or {}
    if env:
        lines += ["", "## Environment", ""]
        for k in sorted(env):
            lines.append(f"- **{k}**: {env[k]}")
    if run.get("git_commit"):
        lines += ["", "## Provenance", "",
                  f"- **Snapshot commit**: `{run['git_commit']}`"]
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
    # Round-4: prefer the run's stored env (captured at run time); fall back to
    # the live kernel env only for runs recorded before the feature existed.
    env = run.get("env") or {}
    if not env:
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


@router.get("/api/projects/{name}/suggestions")
async def project_suggestions(name: str, experiment_id: str = "",
                              status: str = ""):
    """First-class reviewer suggestions with status/outcome, so the UI can show
    which have been applied and whether they improved the goal."""
    rt = get_runtime(name)
    eid = int(experiment_id) if str(experiment_id).isdigit() else None
    return {"suggestions": rt.store.list_suggestions(eid, status or None)}


@router.get("/api/projects/{name}/learnings")
async def project_learnings(name: str, experiment_id: str = "", metric: str = ""):
    """Round-7 knowledge memory: measured outcomes worth remembering."""
    rt = get_runtime(name)
    eid = int(experiment_id) if str(experiment_id).isdigit() else None
    return {"learnings": rt.store.list_learnings(eid, metric or "")}


@router.delete("/api/projects/{name}/learnings/{lid}")
async def project_learning_delete(name: str, lid: int):
    rt = get_runtime(name)
    if not rt.store.delete_learning(lid):
        raise HTTPException(status_code=404, detail="learning not found")
    return {"ok": True}


@router.get("/api/projects/{name}/campaigns")
async def project_campaigns(name: str):
    """Research campaigns for the project (id/name/question/status/steps)."""
    rt = get_runtime(name)
    return {"campaigns": rt.store.list_campaigns(),
            "running": rt.campaign_running()}


@router.post("/api/projects/{name}/campaigns")
async def project_campaigns_create(name: str, body: dict):
    """Create a campaign (does not start it)."""
    rt = get_runtime(name)
    name_str = str(body.get("name") or "Campaign").strip() or "Campaign"
    cid = rt.store.create_campaign(
        name_str, str(body.get("research_question") or ""),
        str(body.get("goal_metric") or ""),
        bool(body.get("higher_better", True)))
    return {"campaign": rt.store.get_campaign(cid)}


@router.post("/api/projects/{name}/campaigns/{cid}/run")
async def project_campaign_run(name: str, cid: int, body: dict | None = None):
    """Start (or resume) a campaign in the background."""
    rt = get_runtime(name)
    if rt.store.get_campaign(cid) is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    ok, msg = rt.start_campaign(cid, plan_steps=(body or {}).get("plan_steps"))
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"campaign": rt.store.get_campaign(cid), "running": True}


@router.post("/api/projects/{name}/campaigns/{cid}/stop")
async def project_campaign_stop(name: str, cid: int):
    """Request a graceful stop of the running background campaign."""
    rt = get_runtime(name)
    if rt.store.get_campaign(cid) is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    stopped = rt.stop_campaign()
    return {"stopped": stopped, "running": rt.campaign_running()}


@router.get("/api/projects/{name}/campaigns/{cid}")
async def project_campaign(name: str, cid: int):
    """One campaign with its steps."""
    rt = get_runtime(name)
    c = rt.store.get_campaign(cid)
    if c is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    c["steps"] = rt.store.list_campaign_steps(cid)
    return {"campaign": c}


@router.post("/api/projects/{name}/suggestions/{sid}/resolve")
async def project_suggestion_resolve(name: str, sid: int):
    """Resolve (regression-check) an applied suggestion on demand."""
    rt = get_runtime(name)
    sug = rt.store.get_suggestion(sid)
    if sug is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return {"suggestion": rt.store.resolve_suggestion_outcome(sid)}


@router.get("/api/projects/{name}/compare")
async def project_compare(name: str, run_a: str = "", run_b: str = "",
                          runs: str = ""):
    """Compare runs. Two ids (run_a/run_b) → the classic pairwise comparison;
    a comma-separated `runs` list → a side-by-side table across N runs."""
    rt = get_runtime(name)

    def resolve(ref: str):
        return rt.store.get_run(int(ref)) if str(ref).isdigit() else None

    if runs:
        ids = [r for r in runs.split(",") if r.strip().isdigit()]
        rs = [resolve(i) for i in ids]
        rs = [r for r in rs if r is not None]
        if len(rs) < 2:
            raise HTTPException(status_code=400, detail="need at least two runs")
        return {"many": compare_runs_many(rs)}
    if not run_a or not run_b:
        raise HTTPException(status_code=400, detail="run_a and run_b are required")
    ra, rb = resolve(run_a), resolve(run_b)
    if ra is None or rb is None:
        raise HTTPException(status_code=404,
                            detail=f"could not resolve run ids: {run_a!r}, {run_b!r}")
    return {"comparison": compare_runs(ra, rb)}


@router.get("/api/projects/{name}/campaigns/compare")
async def project_campaigns_compare(name: str):
    """Leaderboard of campaigns by the best goal value across their steps."""
    rt = get_runtime(name)
    return compare_campaigns(rt.store, rt.store.list_campaigns())


@router.get("/api/projects/{name}/report")
async def project_report(name: str, summary: bool = True):
    """Comprehensive markdown research report for the project."""
    rt = get_runtime(name)
    from ..report import build_project_report
    report = await build_project_report(rt, bool(summary))
    return {"report": report}


@router.post("/api/projects/{name}/report")
async def project_report_save(name: str, body: dict | None = None):
    """Generate the report, save it as an artifact and post it to chat."""
    rt = get_runtime(name)
    from ..report import build_project_report
    report = await build_project_report(rt, bool((body or {}).get("summary", True)))
    mid = rt.store.add_message("assistant", report, {"tags": ["report"]})
    art = Artifact(kind="text", name=f"{name}-report",
                   description=f"Comprehensive research report for project {name}",
                   code="# auto-generated project report", env={},
                   message_id=str(mid))
    rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
    return {"report": report, "artifact_id": art.id, "message_id": mid}


@router.post("/api/projects/{name}/export")
async def project_export(name: str):
    """Portable zip bundle of the project's research record."""
    rt = get_runtime(name)
    from ..export import export_project
    path = export_project(rt)
    return FileResponse(path, media_type="application/zip",
                        filename=f"{name}-export.zip")


@router.get("/api/projects/{name}/evals")
async def project_evals(name: str):
    """Model benchmarks for the project."""
    rt = get_runtime(name)
    return {"evals": rt.store.list_evals(), "running": rt.eval_running()}


@router.post("/api/projects/{name}/evals")
async def project_evals_create(name: str, body: dict):
    """Create a model benchmark (does not run it)."""
    rt = get_runtime(name)
    name_str = str(body.get("name") or "Eval").strip() or "Eval"
    models = [m for m in (body.get("models") or []) if str(m).strip()]
    if not models:
        raise HTTPException(status_code=400, detail="at least one model required")
    eid = rt.store.create_eval(
        name_str, str(body.get("prompt") or ""), models,
        str(body.get("goal_metric") or ""),
        bool(body.get("higher_better", True)))
    return {"eval": rt.store.get_eval(eid)}


@router.post("/api/projects/{name}/evals/{eid}/run")
async def project_eval_run(name: str, eid: int):
    """Run a model benchmark in the background."""
    rt = get_runtime(name)
    if rt.store.get_eval(eid) is None:
        raise HTTPException(status_code=404, detail="eval not found")
    ok, msg = rt.start_eval(eid)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"eval": rt.store.get_eval(eid), "running": True}


@router.post("/api/projects/{name}/evals/{eid}/stop")
async def project_eval_stop(name: str, eid: int):
    rt = get_runtime(name)
    if rt.store.get_eval(eid) is None:
        raise HTTPException(status_code=404, detail="eval not found")
    stopped = rt.stop_eval()
    return {"stopped": stopped, "running": rt.eval_running()}


@router.get("/api/projects/{name}/runs/{rid}/diff")
async def project_run_diff(name: str, rid: int, run_b: int = 0):
    """What changed between run rid and another run (default: its parent)."""
    store = get_runtime(name).store
    a = store.get_run(rid, include_code=True)
    if a is None:
        raise HTTPException(status_code=404, detail="run not found")
    bid = run_b or a.get("parent_run_id")
    b = store.get_run(bid, include_code=True) if bid else None
    if b is None:
        seq = a.get("tool_sequence") or []
        return {"a": a.get("label") or f"run {rid}", "b": None,
                "config": {"added": [], "removed": [], "changed": []},
                "metrics": {"rows": [], "summary": {}},
                "tools": {"added": [], "removed": [], "failed": [],
                          "used": sorted({t.get("name") for t in seq})},
                "code": {"diffs": [], "available": False},
                "prompt": {"a": a.get("prompt") or "", "b": ""}}
    return run_diff(b, a)


@router.get("/api/projects/{name}/runs/{rid}/commits")
async def project_run_commits(name: str, rid: int):
    """The management-repo commit(s) for a run (self-heals legacy runs via
    `git log -- <snapshot path>` when the commit hash was never recorded)."""
    rt = get_runtime(name)
    run = rt.store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        from ..experiment_repo import management_repo_dir, run_commit_info
        repo = management_repo_dir()
        if repo is None:
            return {"run_id": rid, "commit": None, "message": "no management repo configured"}
        info = run_commit_info(repo, name, rid, run.get("git_commit") or "")
        if not info:
            return {"run_id": rid, "commit": None, "message": "no commit found for this run"}
        return {"run_id": rid, **info}
    except Exception as e:  # noqa: BLE001
        return {"run_id": rid, "commit": None, "message": f"{type(e).__name__}: {e}"}


@router.get("/api/projects/{name}/runs/{rid}/audit")
async def project_run_audit(name: str, rid: int):
    """Round-8: the audit trail for a run — its tool events (by trace_id),
    any deviations touching those events, and the audit-chain status."""
    rt = get_runtime(name)
    run = rt.store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    from ..audit import public_event
    mid = run.get("message_id")
    events = []
    deviations = []
    chain = {}
    try:
        if mid is not None:
            events = rt.audit_store.query(trace_id=str(mid), limit=500)
            event_ids = {e.get("event_id") for e in events}
            if event_ids:
                for d in rt.audit_store.list_deviations(limit=1000):
                    dids = set(d.get("event_ids") or [])
                    if dids & event_ids:
                        deviations.append(d)
        chain = rt.audit_store.verify_chain()
    except Exception:  # noqa: BLE001
        pass
    return {"run_id": rid, "trace_id": str(mid) if mid is not None else None,
            "events": [public_event(e) for e in events],
            "deviations": deviations,
            "chain_verified": bool(chain.get("verified"))}


@router.get("/api/projects/{name}/runs/{rid}/verify")
async def project_run_verify(name: str, rid: int):
    """Round-8: recompute the run's content hash and compare to the recorded
    one (tamper-evidence)."""
    rt = get_runtime(name)
    if rt.store.get_run(rid) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": rid, **rt.store.verify_run_integrity(rid)}


@router.post("/api/projects/{name}/runs/{rid}/restore")
async def project_run_restore(name: str, rid: int):
    """Restore a run's artifacts from its management-repo commit and fork a new
    'restore' run (child of rid) so the branch graph shows the restoration."""
    rt = get_runtime(name)
    if rt.store.get_run(rid) is None:
        raise HTTPException(status_code=404, detail="run not found")
    from ..experiment_repo import restore_run
    result = await asyncio.to_thread(restore_run, rt, rid)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message") or "restore failed")
    return result


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
