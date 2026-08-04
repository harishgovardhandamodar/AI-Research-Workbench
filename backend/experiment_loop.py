"""B2: reviewer-driven improve loop.

A bounded, fully traceable experiment loop: run a variant of an experiment, have
the background reviewer suggest the next change, apply the best suggestion as a
fresh prompt and rerun, until the experiment's goal metric reaches its target or
the iteration budget is spent. Every iteration lands in the project store as a
normal run + review + messages, and a closing summary message reports per-iteration
progress toward the goal.

The function is wired to the chat WebSocket (it reuses the live Coordinator and
reviewer), but takes its dependencies explicitly so the loop is unit-testable.
"""

from __future__ import annotations

from typing import Awaitable, Callable

LOOP_MAX_ITERATIONS = 5

_NoopEmit = Callable[[str, dict], Awaitable[None]]


async def _noop_emit(event: str, payload: dict):
    return None


def best_metric(runs: list[dict], metric: str, higher_better: bool = True):
    """Best (best_value, run_id) over runs with a numeric value for `metric`.

    Returns (None, None) when no run carries the metric.
    """
    best = None
    best_id = None
    for r in runs:
        m = (r.get("metrics") or {}).get(metric)
        if m is None:
            continue
        try:
            m = float(m)
        except (TypeError, ValueError):
            continue
        if best is None or (m > best if higher_better else m < best):
            best, best_id = m, r.get("id")
    return best, best_id


async def run_improve_loop(store, coordinator, build_llm_messages, reviewer,
                           experiment_id: int, prompt: str,
                           emit: _NoopEmit | None = None,
                           iterations: int | None = None,
                           max_iterations: int = LOOP_MAX_ITERATIONS,
                           workflow=None) -> dict:
    """Run a bounded improve loop for an experiment.

    Returns {"summary", "iterations": [...], "goal_reached": bool, "best": value}.
    Each iteration entry carries {iteration, prompt, run_id, metrics,
    suggestion, goal_metric_value}. When `workflow` (a WorkflowTracker) is given,
    per-iteration progress is pushed to the chat's workflow panel.
    """
    emit = emit or _noop_emit
    exp = store.get_experiment(experiment_id)
    if exp is None:
        return {"summary": f"experiment #{experiment_id} not found",
                "iterations": [], "goal_reached": False, "best": None}
    if exp.get("status", "active") != "active":
        return {"summary": (f"experiment {exp['name']!r} is {exp.get('status')} "
                            "— reopen it before running the improve loop"),
                "iterations": [], "goal_reached": False, "best": None,
                "stopped_reason": f"experiment {exp.get('status')}"}

    iterations = max(1, min(int(iterations or 3), max_iterations))
    goal_metric = exp.get("goal_metric") or ""
    goal_target = exp.get("goal_target")
    higher = bool(exp.get("higher_better", True))

    if workflow is not None:
        from .workflows import improve_stages

        await workflow.start(title=f"Improve {exp['name']}",
                             stages=improve_stages(iterations))

    runs_all = store.experiment_runs(experiment_id)
    best_val, best_id = best_metric(runs_all, goal_metric, higher) if goal_metric else (None, None)
    current_prompt = (prompt or "").strip() or f"Improve the experiment {exp['name']!r}."
    last_suggestion = None
    goal_reached = False
    stopped_reason = ""
    history: list[dict] = []

    # Attach loop-produced runs to the experiment.
    coordinator.ctx.experiment_id = str(experiment_id)

    for i in range(1, iterations + 1):
        if workflow is not None:
            await workflow.update_stage(f"iter{i}", "running",
                                        message=f"Improve loop — iteration {i}/{iterations}")
        mid = store.add_message("user", current_prompt,
                                {"tags": ["improve loop", f"iteration {i}"],
                                 "experiment_id": experiment_id})
        coordinator.ctx.message_id = str(mid)
        await emit("user_message", {"id": mid, "content": current_prompt,
                                    "tags": ["improve loop"],
                                    "experiment_id": experiment_id,
                                    "created_at": _created(store, mid)})
        await emit("status", {"message": f"Improve loop — iteration {i}/{iterations}…"})

        try:
            result = await coordinator.run_turn(build_llm_messages())
        except Exception as e:  # noqa: BLE001
            stopped_reason = f"iteration {i} failed: {type(e).__name__}: {e}"
            await emit("notice", {"message": f"Improve loop {stopped_reason}"})
            if workflow is not None:
                await workflow.update_stage(f"iter{i}", "failed",
                                            message=stopped_reason)
            break
        text = result.get("text", "")
        if workflow is not None:
            await workflow.update_stage(f"iter{i}", "done",
                                        message=f"Iteration {i} complete")
        amid = store.add_message("assistant", text,
                                 {"tags": ["improve loop"],
                                  "experiment_id": experiment_id})
        await emit("assistant_message", {"id": amid, "content": text,
                                         "tags": ["improve loop"],
                                         "experiment_id": experiment_id,
                                         "created_at": _created(store, amid)})

        runs = store.list_runs()
        run = runs[-1] if runs else None
        review = {"findings": [], "suggestions": []}
        try:
            review = await reviewer() if reviewer else review
        except Exception:  # noqa: BLE001
            review = {"findings": [], "suggestions": []}
        if run is not None:
            store.update_run_review(run["id"], review)
        await emit("review", review)

        iter_metrics = dict(run.get("metrics") or {}) if run else {}
        mval = iter_metrics.get(goal_metric) if goal_metric else None
        if mval is not None:
            try:
                mval = float(mval)
            except (TypeError, ValueError):
                mval = None
        history.append({
            "iteration": i,
            "prompt": current_prompt,
            "run_id": run["id"] if run else None,
            "metrics": iter_metrics,
            "suggestion": last_suggestion,
            "goal_metric_value": mval,
        })

        if mval is not None and goal_metric:
            if best_val is None or (mval > best_val if higher else mval < best_val):
                best_val = mval
                best_id = run["id"] if run else None

        if mval is not None and goal_target is not None:
            reached = mval >= goal_target if higher else mval <= goal_target
            if reached:
                goal_reached = True
                stopped_reason = "goal reached"
                await emit("notice", {"message": (
                    f"Goal {goal_metric} reached: {mval:.4g} vs target {goal_target:.4g} "
                    f"in run #{run['id']}")})
                break

        suggestion = (review.get("suggestions") or [None])[0]
        if not suggestion or not (suggestion.get("prompt") or suggestion.get("action")):
            stopped_reason = "no further suggestions"
            await emit("notice", {"message": (
                f"Improve loop stopped at iteration {i} — the reviewer offered no "
                "further actionable suggestions.")})
            break
        last_suggestion = suggestion
        current_prompt = suggestion.get("prompt") or suggestion.get("action")
    else:
        stopped_reason = f"iteration budget ({iterations}) spent"

    summary = _loop_summary(exp, history, best_val, best_id, goal_reached,
                            goal_metric, goal_target, higher, stopped_reason)
    store.add_message("assistant", summary,
                      {"tags": ["improve loop", "summary"],
                       "experiment_id": experiment_id})
    await emit("assistant_message", {"id": -1, "content": summary,
                                     "tags": ["improve loop summary"],
                                     "experiment_id": experiment_id})
    if workflow is not None:
        await workflow.finish()
    return {"summary": summary, "iterations": history,
            "goal_reached": goal_reached, "best": best_val,
            "stopped_reason": stopped_reason}


def _loop_summary(exp: dict, history: list[dict], best_val, best_id,
                  goal_reached: bool, goal_metric: str, goal_target, higher,
                  stopped_reason: str = "") -> str:
    lines = [f"## Improve loop — {exp.get('name')}",
             "",
             f"- **Goal**: {goal_metric or '(none)'}"
             + (f" {higher and '↑' or '↓'} {goal_target}" if goal_target is not None else ""),
             f"- **Iterations run**: {len(history)}",
             f"- **Goal reached**: {'yes ✓' if goal_reached else 'no'}",
             f"- **Stopped because**: {stopped_reason or '—'}"]
    if goal_metric and best_val is not None:
        lines.append(f"- **Best {goal_metric}**: {best_val:.4g}"
                     + (f" (run #{best_id})" if best_id is not None else ""))
    if history:
        lines += ["", "| it | run | " + (goal_metric or "metric") + " | applied suggestion |",
                  "|---|---|---|---|"]
        for h in history:
            mval = h.get("goal_metric_value")
            mstr = f"{mval:.4g}" if mval is not None else "—"
            sug = (h.get("suggestion") or {}).get("title") or "initial prompt"
            lines.append(f"| {h['iteration']} | #{h.get('run_id') or '—'} | "
                         f"{mstr} | {sug} |")
    return "\n".join(lines)


def _created(store, mid: int) -> float | None:
    """Timestamp for a WS message event emitted from the loop."""
    row = store.get_message(mid)
    return (row or {}).get("created_at")
