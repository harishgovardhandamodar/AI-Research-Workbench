"""Round-9: model benchmark (eval).

Benchmark the workbench's LLMs on a task: for each model in the eval, create a
per-model experiment (round-3 model pinning makes the coordinator use that
model), run one agent turn with the eval prompt, and collect the goal metric.
Produce a ranked leaderboard report. Runs inside a chat turn or the background
runner (mirrors ``campaign.run_campaign``).
"""

from __future__ import annotations

import time

from .experiment_loop import best_metric


def _noop_emit(event: str, payload: dict):
    return None


async def _audit_eval(rt, kind: str, ev: dict, models: list | None = None,
                      error: str | None = None) -> None:
    """Emit an eval-level audit event (eval_started / eval_completed /
    eval_failed) so model benchmarks are visible in the audit trail."""
    try:
        if rt.audit_emitter is None:
            return
        from .audit import emit_session_event
        payload = {
            "event": kind,
            "eval_id": ev.get("id"),
            "name": ev.get("name"),
            "goal_metric": ev.get("goal_metric") or "",
            "models": models or [],
        }
        if error:
            payload["error"] = str(error)[:2000]
        await emit_session_event(
            rt.audit_emitter, agent_id="Fox", session_id=rt.name,
            trace_id=None, run_id=None, kind=kind, tool_name=None,
            payload=payload,
            severity="critical" if kind == "eval_failed" else "info")
    except Exception:  # noqa: BLE001
        pass


def _eval_report(store, ev: dict, results: list[dict]) -> str:
    goal = ev.get("goal_metric") or ""
    higher = bool(ev.get("higher_better", True))
    lines = [f"# Model benchmark: {ev['name']}", "",
             f"**Goal metric**: {goal or '(none)'} "
             f"({'higher' if higher else 'lower'} is better)",
             f"**Models**: {len(results)}", ""]
    if results:
        lines.append("| model | best " + (goal or "metric") + " | experiment | run |")
        lines.append("|---|---|---|---|")
        for r in sorted(results, key=lambda x: (x["best"] is not None, x["best"]),
                        reverse=higher):
            b = r.get("best")
            bstr = f"{b:.4g}" if b is not None else "—"
            lines.append(f"| {r['model']} | {bstr} | #{r.get('experiment_id') or '—'} "
                         f"| #{r.get('best_run_id') or '—'} |")
        lines.append("")
        ranked = [r for r in results if r.get("best") is not None]
        if ranked:
            best = max(ranked, key=lambda r: r["best"] if higher else -r["best"])
            lines.append(f"**Best model**: {best['model']} "
                         f"({best['best']:.4g} on {goal or 'metric'}).")
    else:
        lines.append("No models were evaluated.")
    return "\n".join(lines)


async def run_eval(rt, coordinator, build_llm_messages, eval_id: int,
                   emit=None, workflow=None) -> dict:
    """Run a model benchmark: one experiment (pinned to that model) + one agent
    turn per model; collect each run's goal metric; write the leaderboard."""
    emit = emit or _noop_emit
    store = rt.store
    ev = store.get_eval(eval_id)
    if ev is None:
        return {"eval": None, "report": "", "stopped_reason": "eval not found"}
    models = [m for m in (ev.get("models") or []) if m]
    if not models:
        store.update_eval(eval_id, status="failed", report="no models to evaluate")
        return {"eval": ev, "report": "", "stopped_reason": "no models"}
    goal = ev.get("goal_metric") or ""
    higher = bool(ev.get("higher_better", True))
    prompt = ev.get("prompt") or "Run the experiment and report the goal metric."

    store.update_eval(eval_id, status="running")
    if workflow is not None:
        from .workflows import campaign_stages
        await workflow.start(title=f"Eval: {ev['name']}",
                             stages=campaign_stages(len(models)))
        workflow.set_invoke(kind="eval", eval_id=eval_id)
    await _audit_eval(rt, "eval_started", ev, models=models)

    results: list[dict] = []
    prev_best_run_id = None
    stopped_reason = ""
    for i, model in enumerate(models, 1):
        if workflow is not None:
            await workflow.update_stage(f"step{i}", "running",
                                        message=f"{model}")
        await emit("status", {"message": f"Eval {i}/{len(models)}: {model}…"})

        # Retry-safe: if this model already has a benchmarked experiment (a
        # previous run of the eval), reuse its best result instead of re-running
        # the model from scratch and duplicating experiments.
        exp_name = f"[Eval] {ev['name']} · {model}"
        existing = None
        try:
            for e in store.list_experiments():
                if (e.get("name") or "").startswith(exp_name):
                    if store.experiment_runs(e["id"]):
                        existing = e
                        break
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            runs = store.experiment_runs(existing["id"])
            best_val, best_id = best_metric(runs, goal, higher) if goal else (None, None)
            if best_id is None and runs:
                best_id = runs[-1]["id"]
            results.append({"model": model, "best": best_val,
                            "best_run_id": best_id, "experiment_id": existing["id"],
                            "skipped": True})
            prev_best_run_id = best_id
            if workflow is not None:
                await workflow.update_stage(
                    f"step{i}", "done",
                    message=f"already benchmarked ({best_val:.4g})" if best_val is not None else "done")
            await emit("notice", {"message": (
                f"Eval model {model}: reusing previous result "
                f"(best {goal or 'metric'} = {best_val:.4g})." if best_val is not None
                else f"Eval model {model}: reusing previous result.")})
            continue

        eid = store.create_experiment(
            exp_name, prompt, goal, None, higher,
            model=model)
        store.update_experiment(eid, plan=f"Model benchmark run for {model}")
        coordinator.ctx.experiment_id = str(eid)
        coordinator.ctx.parent_run_id = prev_best_run_id
        mid = store.add_message("user", prompt,
                                {"tags": ["eval", f"model {i}"], "experiment_id": eid})
        coordinator.ctx.message_id = str(mid)
        await emit("user_message", {"id": mid, "content": prompt,
                                    "tags": ["eval"], "experiment_id": eid,
                                    "created_at": _created(store, mid)})
        try:
            result = await coordinator.run_turn(build_llm_messages())
        except Exception as e:  # noqa: BLE001
            stopped_reason = f"model {model} failed: {type(e).__name__}: {e}"
            store.update_eval(eval_id, status="failed", report=stopped_reason)
            if workflow is not None:
                await workflow.update_stage(f"step{i}", "failed", message=stopped_reason)
            await emit("notice", {"message": f"Eval {stopped_reason}"})
            break
        text = result.get("text", "")
        tags = ["eval", f"model {i}"]
        amid = store.add_message("assistant", text, {"tags": tags, "experiment_id": eid})
        await emit("assistant_message", {"id": amid, "content": text,
                                         "tags": tags, "experiment_id": eid,
                                         "created_at": _created(store, amid)})
        runs = store.experiment_runs(eid)
        best_val, best_id = best_metric(runs, goal, higher) if goal else (None, None)
        if best_id is None and runs:
            best_id = runs[-1]["id"]
        results.append({"model": model, "best": best_val,
                        "best_run_id": best_id, "experiment_id": eid})
        prev_best_run_id = best_id
        if workflow is not None:
            await workflow.update_stage(
                f"step{i}", "done",
                message=f"best {goal or 'metric'}: {best_val:.4g}" if best_val is not None else "done")
        await emit("notice", {"message": (
            f"Eval model {model}: best {goal or 'metric'} = "
            f"{best_val:.4g}" if best_val is not None else f"Eval {model} done.")})
        try:
            await rt.maybe_compact()
        except Exception:  # noqa: BLE001
            pass
        if coordinator.check_abort is not None and coordinator.check_abort():
            stopped_reason = "stopped by user"
            break

    report = _eval_report(store, ev, results)
    status = "done" if not stopped_reason else "failed"
    store.update_eval(eval_id, status=status, report=report)
    await _audit_eval(rt, "eval_completed" if status == "done" else "eval_failed",
                      ev, models=models, error=stopped_reason or None)
    report_mid = store.add_message("assistant", report, {"tags": ["eval", "report"]})
    await emit("assistant_message", {"id": report_mid, "content": report,
                                     "tags": ["eval report"],
                                     "created_at": _created(store, report_mid)})
    try:
        from .artifacts.store import Artifact
        art = Artifact(kind="text", name=f"eval-{eval_id}-report",
                       description=f"Model benchmark report for eval #{eval_id}",
                       code="# eval report", env={},
                       run_id=prev_best_run_id, message_id=str(report_mid))
        rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
        await emit("artifact", {"artifact": art.to_dict()})
    except Exception:  # noqa: BLE001
        pass
    if workflow is not None:
        await workflow.finish()
    await emit("notice", {"message": f"Model benchmark '{ev['name']}' complete."})
    return {"eval": store.get_eval(eval_id), "report": report,
            "stopped_reason": stopped_reason, "results": results}


def _created(store, mid: int) -> float | None:
    row = store.get_message(mid)
    return (row or {}).get("created_at")
