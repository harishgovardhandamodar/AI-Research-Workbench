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


def plan_proposal_payload(plan: dict) -> dict:
    """The chat/UI payload for a proposed plan (no result/figures yet)."""
    return {
        "plan_id": plan.get("id"),
        "experiment_id": plan.get("experiment_id"),
        "name": plan.get("name"),
        "request": plan.get("request"),
        "dataset": plan.get("dataset"),
        "seed": plan.get("seed"),
        "steps": plan.get("steps") or [],
        "status": plan.get("status"),
        "created_at": plan.get("created_at"),
    }


async def present_result(rt, plan: dict, emit=None) -> None:
    """Register the executed plan's artifacts + run, and (if emit) post an
    assistant message with the report + figures to the chat."""
    from ..experiment_planner import execute_plan
    import pandas as pd

    path = rt.dir / plan["dataset"]
    df = pd.read_csv(path, low_memory=False)
    done = execute_plan(plan, df)
    if done["status"] != "DONE":
        raise RuntimeError(done.get("error") or "experiment failed")

    # Persist the executed plan (with result).
    _store(rt).update(
        done["id"], status=done["status"], result=done["result"],
        metrics=done["metrics"], error=done.get("error"))

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

    # Record a run in the project's table (Experiments tab).
    rt.store.add_run(
        prompt=(f"[Plan {plan['id']}] {plan['name']} on {plan['dataset']}"),
        reply=report_md[:3000], status="done",
        started_at=time.time(), finished_at=time.time(),
        artifact_ids=artifact_ids,
        metrics=done.get("metrics") or {},
        kind="experiment_plan", label=f"plan:{plan['id']}",
        model=None, dataset=plan.get("dataset"))

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


@router.get("/api/projects/{name}/experiment-plans/{plan_id}")
async def get_plan(name: str, plan_id: str):
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan": plan}


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
    """Execute an APPROVED plan and present the result."""
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.get("status") != "APPROVED":
        return JSONResponse({"error": "plan must be APPROVED before running"},
                            status_code=400)
    try:
        res = await present_result(rt, plan, emit=None)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=422)
    return {"ok": True, **res}


@router.delete("/api/projects/{name}/experiment-plans/{plan_id}")
async def delete_plan(name: str, plan_id: str):
    rt = get_runtime(name)
    _store(rt).delete(plan_id)
    return {"ok": True}
