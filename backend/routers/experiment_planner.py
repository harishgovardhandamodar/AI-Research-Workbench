"""Experiment planner routes: plan, propose, approve/reject, execute, present.

Implements the confirm-before-execute lifecycle over the per-project PlanStore.
Used both by the REST API and (via helpers here) by the chat intent handler so
a plan can be proposed in the chat window and executed only after the user
approves.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..artifacts.store import Artifact
from ..state import get_runtime

router = APIRouter()


def _store(rt):
    from ..experiment_planner import PlanStore
    return PlanStore(rt.dir)


# Track in-flight plan runs per project: plan_id -> asyncio.Task. Prevents
# double-execution and enables cancel.
_run_tasks: dict[str, "asyncio.Task"] = {}


def _runs_key(rt, plan_id: str) -> str:
    return f"{rt.name}:{plan_id}"


def plan_proposal_payload(plan: dict, project_dir: str | None = None) -> dict:
    """The chat/UI payload for a proposed plan (no result/figures yet)."""
    out = {
        "plan_id": plan.get("id"),
        "experiment_id": plan.get("experiment_id"),
        "name": plan.get("name"),
        "description": plan.get("description"),
        "request": plan.get("request"),
        "dataset": plan.get("dataset"),
        "seed": plan.get("seed"),
        "steps": plan.get("steps") or [],
        "expected_outputs": plan.get("expected_outputs") or [],
        "status": plan.get("status"),
        "created_at": plan.get("created_at"),
    }
    # Lightweight dataset preview so the user approves with context. The plan
    # stores the dataset relative to its project dir.
    dataset = plan.get("dataset")
    base = Path(project_dir) if project_dir else Path(plan.get("_project_dir") or "")
    if dataset and base:
        cand = base / dataset
        if cand.exists():
            try:
                import pandas as pd
                df = pd.read_csv(cand, nrows=500, low_memory=False)
                out["dataset_info"] = {
                    "shape": [len(df), len(df.columns)],
                    "columns": list(df.columns),
                }
            except Exception:  # noqa: BLE001
                pass
    return out


async def present_result(rt, plan: dict, emit=None, progress=None) -> None:
    """Register the executed plan's artifacts + run, and (if emit) post an
    assistant message with the report + figures to the chat."""
    from ..experiment_planner import execute_plan
    import pandas as pd

    path = rt.dir / plan["dataset"]
    df = pd.read_csv(path, low_memory=False)

    async def _prog(i, message):
        if emit:
            try:
                await emit("workflow", {
                    "status": "running", "title": f"Plan {plan['id']}",
                    "message": message, "pct": round(i / max(len(plan.get("steps") or []), 1) * 100),
                })
            except Exception:  # noqa: BLE001
                pass

    done = execute_plan(plan, df, project_dir=rt.dir, progress=progress or _prog)
    if done["status"] != "DONE":
        raise RuntimeError(done.get("error") or "experiment failed")

    # Persist the executed plan (with result).
    _store(rt).update(
        done["id"], status=done["status"], result=done["result"],
        metrics=done["metrics"], error=done.get("error"),
        result_dir=done.get("result_dir"))

    # Register figures + report as artifacts.
    artifact_ids = []
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        env = {}
    fig_refs = []
    for name_, data in (done.get("_figures_bytes") or {}).items():
        art = Artifact(kind="figure", name=name_,
                       description=f"{plan['name']} figure: {name_}",
                       code=f"experiment_plan({plan['id']})", env=env,
                       message_id="", run_id="", data_type="png")
        rt.artifacts.add_artifact(art, data=data, data_type="png")
        artifact_ids.append(art.id)
        fig_refs.append({"name": name_, "id": art.id})
    report_md = done.get("_report_md") or ""
    report_id = ""
    if report_md:
        art = Artifact(kind="report",
                       name=f"plan-{plan['id']}-report",
                       description=f"{plan['name']} report",
                       code=f"experiment_plan({plan['id']})", env=env,
                       message_id="", run_id="", data_type="text")
        rt.artifacts.add_artifact(art, data=report_md.encode(), data_type="text")
        report_id = art.id
        artifact_ids.append(art.id)

    # Record a run in the project's table (Experiments tab), attached to a
    # plan experiment (created once per plan so it shows in Experiments).
    experiment_id = None
    try:
        exps = rt.store.list_experiments()
        for e in exps:
            if (e.get("name") or "").startswith(f"🧪 {plan['name']} · {plan['id']}"):
                experiment_id = e["id"]
                break
        if experiment_id is None:
            experiment_id = rt.store.create_experiment(
                name=f"🧪 {plan['name']} · {plan['id']}",
                hypothesis=(plan.get("request") or plan.get("description") or "")[:200],
                goal_metric=(next(iter(plan.get("metrics") or {}), "") or ""),
                higher_better=True)
    except Exception:  # noqa: BLE001
        experiment_id = None

    rt.store.add_run(
        prompt=(f"[Plan {plan['id']}] {plan['name']} on {plan['dataset']}"),
        reply=report_md[:3000], status="done",
        started_at=time.time(), finished_at=time.time(),
        artifact_ids=artifact_ids,
        metrics=done.get("metrics") or {},
        kind="experiment_plan", label=f"plan:{plan['id']}",
        model=None, dataset=plan.get("dataset"),
        experiment_id=experiment_id)

    if emit:
        # Post figures inline + report link + summary as an assistant message.
        fig_html = "".join(
            f"![{f['name']}](/artifacts/{f['id']})" for f in fig_refs)
        content = (f"**✅ {plan['name']} — plan `{plan['id']}` executed**\n\n"
                   f"- Dataset: `{plan['dataset']}` · seed `{plan['seed']}`\n"
                   f"- Metrics: {_fmt_metrics(done.get('metrics'))}\n\n"
                   f"{fig_html}\n\n"
                   + report_md)
        amid = rt.store.add_message(
            "assistant", content, {"tags": ["experiment_plan", "result", "report"]})
        if emit:
            try:
                await emit("assistant_message", {
                    "id": amid, "content": content,
                    "tags": ["experiment_plan", "result", "report"],
                    "created_at": time.time()})
                for f in fig_refs:
                    await emit("artifact", {"artifact": rt.artifacts.get(f["id"]).to_dict()})
                await emit("done", {})
            except Exception:  # noqa: BLE001
                pass

    return {"plan_id": plan["id"], "artifact_ids": artifact_ids,
            "report_id": report_id, "figures": fig_refs,
            "metrics": done.get("metrics")}


def _fmt_metrics(m: dict | None) -> str:
    if not m:
        return "—"
    return " · ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                      for k, v in m.items())


# ----------------------------------------------------------------- REST -------
@router.get("/api/projects/{name}/experiment-plans")
async def list_plans(name: str, status: str = ""):
    rt = get_runtime(name)
    return {"plans": _store(rt).list(status=status or None)}


@router.get("/api/experiments/catalog")
async def experiment_catalog():
    """The available deterministic experiments (planner catalog)."""
    from ..experiment_planner import list_experiments, EXPERIMENT_REGISTRY
    out = []
    for e in list_experiments():
        defn = EXPERIMENT_REGISTRY.get(e["id"]) or {}
        steps = defn.get("plan_steps") or []
        expected = defn.get("expected_outputs") or []
        out.append({**e,
                    "requires_columns": defn.get("requires_columns") or [],
                    "steps": list(steps("", "")) if callable(steps) else list(steps),
                    "expected_outputs": (list(expected("", ""))
                                         if callable(expected) else list(expected))})
    return {"experiments": out}


@router.get("/api/projects/{name}/experiment-plans/{plan_id}")
async def get_plan(name: str, plan_id: str):
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan": plan}


@router.get("/api/projects/{name}/experiment-plans/{plan_id}/result")
async def get_plan_result(name: str, plan_id: str):
    """Re-fetch a DONE plan's result (figures + report) from its persisted dir."""
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.get("status") != "DONE":
        return {"plan": plan_proposal_payload(plan), "result": None,
                "message": "Plan not executed yet."}
    result_dir = plan.get("result_dir")
    figures, report_md = [], ""
    if result_dir:
        d = Path(result_dir)
        report_path = d / "report.md"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8", errors="replace")
        figures = [p.name for p in sorted(d.iterdir())
                   if p.suffix.lower() in (".png", ".svg")]
    return {"plan": plan, "result": {"figures": figures,
                                     "report": report_md,
                                     "metrics": plan.get("metrics")},
            "message": "Plan result (persisted)."}


@router.post("/api/projects/{name}/experiment-plans")
async def create_plan(name: str, body: dict):
    """Create + propose a plan (DRAFT -> WAITING_APPROVAL). Nothing runs."""
    rt = get_runtime(name)
    try:
        plan = _store(rt).create(
            experiment_id=body.get("experiment_id") or "",
            request=body.get("request") or "",
            dataset=body.get("dataset") or "",
            seed=body.get("seed"))
        proposed = _store(rt).propose(plan["id"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"plan": plan_proposal_payload(proposed),
            "message": "Plan proposed — confirm to execute."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/decide")
async def decide_plan(name: str, plan_id: str, body: dict):
    """Approve or reject a WAITING_APPROVAL plan."""
    rt = get_runtime(name)
    approve = bool(body.get("approve"))
    try:
        plan = _store(rt).decide(plan_id, approve, by=body.get("by") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"plan": plan_proposal_payload(plan),
            "approved": approve,
            "message": "Plan approved — run it to execute." if approve
                       else "Plan rejected."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/run")
async def run_plan(name: str, plan_id: str):
    """Launch execution of an APPROVED plan in the background (non-blocking).
    Idempotent: a plan already running won't be re-launched."""
    import asyncio
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.get("status") != "APPROVED":
        return JSONResponse({"error": "plan must be APPROVED before running"},
                            status_code=400)
    key = _runs_key(rt, plan_id)
    if key in _run_tasks and not _run_tasks[key].done():
        return {"ok": False, "running": True,
                "message": "Plan is already running."}

    async def _task():
        try:
            await present_result(rt, plan, emit=None)
        finally:
            _run_tasks.pop(key, None)

    _run_tasks[key] = asyncio.create_task(_task())
    return {"ok": True, "running": True,
            "message": "Plan execution started in the background."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/cancel")
async def cancel_plan(name: str, plan_id: str):
    """Cancel an in-flight plan run (best-effort)."""
    import asyncio
    rt = get_runtime(name)
    key = _runs_key(rt, plan_id)
    task = _run_tasks.get(key)
    if task and not task.done():
        task.cancel()
        _run_tasks.pop(key, None)
        _store(rt).update(plan_id, status="REJECTED",
                          error="cancelled by user")
        return {"ok": True, "message": "Plan run cancelled."}
    return {"ok": False, "message": "Plan is not running."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/repropose")
async def repropose_plan(name: str, plan_id: str, body: dict):
    """Edit + re-propose a rejected/failed plan (new seed/dataset/request)."""
    rt = get_runtime(name)
    try:
        plan = _store(rt).repropose(
            plan_id,
            dataset=(body.get("dataset") or "").strip() or None,
            seed=body.get("seed"),
            request=(body.get("request") or "").strip() or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"plan": plan_proposal_payload(plan),
            "message": "Plan re-proposed — confirm to execute."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/clone")
async def clone_plan(name: str, plan_id: str, body: dict):
    """Clone a plan into a fresh DRAFT (re-run variant with a new seed)."""
    rt = get_runtime(name)
    try:
        plan = _store(rt).clone(
            plan_id, seed=body.get("seed"),
            dataset=(body.get("dataset") or "").strip() or None,
            request=(body.get("request") or "").strip() or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"plan": plan_proposal_payload(_store(rt).get(plan["id"])),
            "message": "Plan cloned — confirm to execute the variant."}


@router.delete("/api/projects/{name}/experiment-plans/{plan_id}")
async def delete_plan(name: str, plan_id: str):
    rt = get_runtime(name)
    _store(rt).delete(plan_id)
    return {"ok": True}
