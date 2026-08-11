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


def _sync_plan(rt, plan: dict) -> None:
    """Mirror a planner plan into the project's SQLite plan record (unified
    plan lineage). Best-effort: the JSON PlanStore stays the source of truth."""
    try:
        rt.store.upsert_plan(plan)
    except Exception:  # noqa: BLE001
        pass


async def _audit_plan(rt, kind: str, plan: dict, run_id: int | None = None,
                      metrics: dict | None = None, error: str | None = None,
                      cancelled: bool = False) -> None:
    """Emit a plan-lifecycle audit event (plan_started / plan_completed /
    plan_failed / plan_cancelled) linked to the plan's run_id, so deterministic
    plan executions are visible in the audit trail."""
    try:
        if rt.audit_emitter is None:
            return
        from ..audit import emit_session_event
        rid = str(run_id) if run_id else None
        payload = {
            "event": kind,
            "plan_id": plan.get("id"),
            "experiment_id": plan.get("experiment_id"),
            "dataset": plan.get("dataset"),
            "seed": plan.get("seed"),
            "steps": len(plan.get("steps") or []),
        }
        if metrics:
            payload["metrics"] = metrics
        if error:
            payload["error"] = str(error)[:2000]
        await emit_session_event(
            rt.audit_emitter, agent_id="Fox", session_id=rt.name,
            trace_id=None, run_id=rid, kind=kind, tool_name=None,
            payload=payload,
            severity="critical" if kind == "plan_failed" else "info")
    except Exception:  # noqa: BLE001
        pass


def create_or_get_experiment(rt, plan: dict, goal_metric: str = "") -> int | None:
    """Create (or find) the Experiments-tab experiment for a plan. Uses a
    meaningful goal metric when available (falls back to the registry hint) and
    the experiment's declared direction (higher/lower better) instead of blindly
    ranking higher-is-better."""
    try:
        from ..experiment_planner import EXPERIMENT_REGISTRY
        defn = EXPERIMENT_REGISTRY.get(plan.get("experiment_id")) or {}
        hint = (defn.get("goal_metric") or "").strip() or goal_metric
        higher_better = bool(defn.get("higher_better", True))
        exps = rt.store.list_experiments()
        for e in exps:
            if (e.get("name") or "").startswith(f"🧪 {plan['name']} · {plan['id']}"):
                if hint and not (e.get("goal_metric") or ""):
                    rt.store.update_experiment(e["id"], goal_metric=hint)
                if e.get("higher_better") is None:
                    rt.store.update_experiment(e["id"], higher_better=higher_better)
                return e["id"]
        return rt.store.create_experiment(
            name=f"🧪 {plan['name']} · {plan['id']}",
            hypothesis=(plan.get("request") or plan.get("description") or "")[:200],
            goal_metric=hint,
            higher_better=higher_better)
    except Exception:  # noqa: BLE001
        return None


# In-flight plan runs are tracked per project on ProjectRuntime (``_plan_tasks``)
# so the chat and REST executors share one dedup + cancel registry.


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
        "parent_id": plan.get("parent_id") or "",
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
                from ..experiment_planner import peek_dataset
                df = peek_dataset(cand, n=500)
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
    from ..experiment_planner import execute_plan, load_dataset

    # Run the (potentially heavy) compute in a worker thread so the event loop
    # stays responsive during long experiments.
    import asyncio
    loop = asyncio.get_running_loop()

    async def _load_df():
        return await asyncio.to_thread(load_dataset, rt.dir / plan["dataset"])

    df = await _load_df()

    async def _emit_progress(i, message):
        if emit:
            try:
                await emit("workflow", {
                    "status": "running", "title": f"Plan {plan['id']}",
                    "message": message,
                    "pct": round(i / max(len(plan.get("steps") or []), 1) * 100),
                })
            except Exception:  # noqa: BLE001
                pass

    # Sync progress: execute_plan calls it from the to_thread worker, so it must
    # hop back onto this event loop thread-safely (asyncio.get_event_loop() from
    # a worker thread would drop the update silently).
    def _prog(i, message):
        try:
            asyncio.run_coroutine_threadsafe(
                _emit_progress(i, message), loop)
        except Exception:  # noqa: BLE001
            pass

    # Persist RUNNING + started_at up front so a restart can recover the run,
    # and so a concurrent cancel can flip it to REJECTED (checked below).
    _store(rt).update(plan["id"], status="RUNNING", started_at=time.time())
    _sync_plan(rt, _store(rt).get(plan["id"]))

    # Record a RUNNING run immediately so the Recent-runs list shows the plan
    # as in progress (previously the run only appeared after execution, so it
    # looked "done" while the plan was still working).
    experiment_id = create_or_get_experiment(rt, plan)
    started_at = time.time()
    run_id = rt.store.add_run(
        prompt=f"[Plan {plan['id']}] {plan['name']} on {plan['dataset']}",
        reply="", status="running",
        started_at=started_at, finished_at=None,
        kind="experiment_plan", label=f"plan:{plan['id']}",
        model=None, dataset=plan.get("dataset"),
        experiment_id=experiment_id,
        plan_id=plan["id"])
    await _audit_plan(rt, "plan_started", plan, run_id=run_id)

    done = await asyncio.to_thread(
        execute_plan, plan, df, rt.dir, progress or _prog)

    # A cancel that raced the compute flips the persisted status to REJECTED;
    # respect it and don't register artifacts for a cancelled run.
    current = _store(rt).get(done["id"]) or {}
    if current.get("status") == "REJECTED":
        rt.store.update_run(run_id, status="cancelled",
                            reply="cancelled by user",
                            finished_at=time.time())
        await _audit_plan(rt, "plan_cancelled", plan, run_id=run_id,
                          cancelled=True)
        if emit:
            try:
                await emit("workflow", {
                    "status": "cancelled", "title": f"Plan {plan['id']}",
                    "message": "Cancelled by user", "pct": 0})
            except Exception:  # noqa: BLE001
                pass
        return {"plan_id": plan["id"], "cancelled": True,
                "artifact_ids": [], "report_id": "", "figures": [],
                "metrics": None}

    if done["status"] != "DONE":
        rt.store.update_run(run_id, status="error",
                            reply=(done.get("error") or "experiment failed")[:3000],
                            finished_at=time.time())
        await _audit_plan(rt, "plan_failed", plan, run_id=run_id,
                          error=done.get("error") or "experiment failed")
        if emit:
            try:
                await emit("workflow", {
                    "status": "failed", "title": f"Plan {plan['id']}",
                    "message": done.get("error") or "experiment failed",
                    "pct": 100})
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(done.get("error") or "experiment failed")

    # Persist the executed plan (with result).
    _store(rt).update(
        done["id"], status=done["status"], result=done["result"],
        metrics=done["metrics"], error=done.get("error"),
        result_dir=done.get("result_dir"))
    _sync_plan(rt, _store(rt).get(done["id"]))

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

    # Mark the running run done with the result + artifacts.
    rt.store.update_run(
        run_id, status="done", reply=report_md[:3000],
        metrics=done.get("metrics") or {},
        artifact_ids=artifact_ids,
        finished_at=time.time())
    await _audit_plan(rt, "plan_completed", plan, run_id=run_id,
                      metrics=done.get("metrics") or {})

    if emit:
        # Final workflow status so the chat run-log bubble closes out.
        try:
            await emit("workflow", {
                "status": "done", "title": f"Plan {plan['id']}",
                "message": f"Completed — {plan['name']}", "pct": 100})
        except Exception:  # noqa: BLE001
            pass
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
    plans = _store(rt).list(status=status or None)
    # Backfill the unified plan mirror so pre-existing plans get run lineage.
    for p in plans:
        _sync_plan(rt, p)
    return {"plans": plans}


@router.get("/api/projects/{name}/experiment-plans/{plan_id}/runs")
async def plan_runs(name: str, plan_id: str):
    """The runs a plan produced (unified plan lineage: runs.plan_id)."""
    rt = get_runtime(name)
    st = _store(rt)
    if st.get(plan_id) is None:
        raise HTTPException(status_code=404, detail="plan not found")
    _sync_plan(rt, st.get(plan_id))
    runs = rt.store.plan_runs(plan_id)
    return {"plan_id": plan_id,
            "experiment_id": (rt.store.get_plan_record(plan_id) or {}).get("experiment_id"),
            "runs": runs, "count": len(runs)}


@router.get("/api/projects/{name}/experiment-plans/suggestions")
async def plan_suggestions(name: str):
    """Incremental next-step suggestions derived from prior plan runs.

    Also scans the project dir for dataset files that have never been planned so
    a freshly uploaded CSV isn't invisible to the planner (cold-start EDA).
    User-dismissed suggestion ids are filtered out."""
    rt = get_runtime(name)
    from ..experiment_planner import build_suggestions, is_dataset_file
    st = _store(rt)
    plans = st.list()
    planned = {p.get("dataset") for p in plans if p.get("dataset")}
    available = set()
    try:
        for f in rt.dir.iterdir():
            if (f.is_file() and is_dataset_file(f.name)
                    and not f.name.lower().startswith("synthetic_")
                    and f.name not in planned):
                available.add(f.name)
    except Exception:  # noqa: BLE001
        pass
    return {"suggestions": build_suggestions(
        plans, datasets=sorted(available),
        dismissed=st.dismissed_suggestions())}


@router.post("/api/projects/{name}/experiment-plans/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(name: str, suggestion_id: str):
    """Dismiss a suggestion so it no longer appears (per-project, persistent)."""
    rt = get_runtime(name)
    st = _store(rt)
    if not suggestion_id or len(suggestion_id) < 8:
        raise HTTPException(status_code=400, detail="invalid suggestion id")
    st.dismiss_suggestion(suggestion_id)
    return {"ok": True, "dismissed": suggestion_id}


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
    artifact_links = []
    if result_dir:
        d = Path(result_dir)
        report_path = d / "report.md"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8", errors="replace")
        figures = [p.name for p in sorted(d.iterdir())
                   if p.suffix.lower() in (".png", ".svg")]
        # Link figures to registered artifacts so the UI can show them inline.
        for name in figures:
            for a in rt.artifacts.list(limit=500):
                if a.get("name") == name and a.get("data_type") == "png":
                    artifact_links.append({"name": name, "id": a.get("id")})
                    break
    return {"plan": plan, "result": {"figures": figures,
                                     "report": report_md,
                                     "artifact_links": artifact_links,
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
        # Create the Experiments-tab experiment up front so the plan is visible
        # and trackable before it runs.
        create_or_get_experiment(rt, proposed)
        _sync_plan(rt, proposed)
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
        _sync_plan(rt, plan)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"plan": plan_proposal_payload(plan),
            "approved": approve,
            "message": "Plan approved — run it to execute." if approve
                       else "Plan rejected."}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/run")
async def run_plan(name: str, plan_id: str):
    """Launch execution of an APPROVED plan in the background (non-blocking).
    Idempotent: a plan already running (chat or REST) won't be re-launched."""
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.get("status") != "APPROVED":
        return JSONResponse({"error": "plan must be APPROVED before running"},
                            status_code=400)
    if rt.plan_running(plan_id):
        return {"ok": False, "running": True,
                "message": "Plan is already running."}
    # Persist the launch so a restart can recover an orphaned RUNNING plan and
    # a cancel mid-run is visible to present_result.
    _store(rt).update(plan_id, status="RUNNING", started_at=time.time())
    _sync_plan(rt, _store(rt).get(plan_id))

    async def _task():
        try:
            await present_result(rt, plan, emit=None)
        except Exception as e:  # noqa: BLE001
            try:
                cur = _store(rt).get(plan_id) or {}
                if cur.get("status") != "REJECTED":
                    _store(rt).update(plan_id, status="FAILED",
                                      error=f"{type(e).__name__}: {e}")
                    _sync_plan(rt, _store(rt).get(plan_id))
            except Exception:  # noqa: BLE001
                pass

    ok, msg = rt.launch_plan(plan_id, _task())
    return {"ok": ok, "running": ok,
            "message": "Plan execution started in the background." if ok
                       else msg}


@router.post("/api/projects/{name}/experiment-plans/{plan_id}/cancel")
async def cancel_plan(name: str, plan_id: str):
    """Cancel an in-flight plan run (best-effort). The worker thread can't be
    killed, but the persisted status flips to REJECTED so a racing
    present_result won't register artifacts or a DONE result."""
    rt = get_runtime(name)
    plan = _store(rt).get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    running = rt.plan_running(plan_id) or plan.get("status") == "RUNNING"
    if not running:
        return {"ok": False, "message": "Plan is not running."}
    rt.cancel_plan_task(plan_id)
    _store(rt).update(plan_id, status="REJECTED",
                      error="cancelled by user")
    _sync_plan(rt, _store(rt).get(plan_id))
    return {"ok": True, "message": "Plan run cancelled."}


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
    _sync_plan(rt, plan)
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
    _sync_plan(rt, _store(rt).get(plan["id"]))
    return {"plan": plan_proposal_payload(_store(rt).get(plan["id"])),
            "message": "Plan cloned — confirm to execute the variant."}


@router.delete("/api/projects/{name}/experiment-plans/{plan_id}")
async def delete_plan(name: str, plan_id: str):
    rt = get_runtime(name)
    _store(rt).delete(plan_id)
    return {"ok": True}
