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
                           workflow=None,
                           start_at: int = 1) -> dict:
    """Run a bounded improve loop for an experiment.

    Returns {"summary", "iterations": [...], "goal_reached": bool, "best": value}.
    Each iteration entry carries {iteration, prompt, run_id, metrics,
    suggestion, goal_metric_value, suggestion_id, delta, improved}. When
    `workflow` (a WorkflowTracker) is given, per-iteration progress is pushed to
    the chat's workflow panel. `start_at > 1` resumes a previously failed loop
    from iteration N (stage ids offset, lineage from the best prior run).
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
    start_at = max(1, min(int(start_at or 1), iterations))
    goal_metric = exp.get("goal_metric") or ""
    goal_target = exp.get("goal_target")
    higher = bool(exp.get("higher_better", True))
    # Combined goal checks: the experiment's own goal plus any Goals-panel goals
    # (scoped or project-wide) that apply to this experiment — so the objective
    # UI and the improve loop agree on when a target is reached.
    goal_checks = []
    if goal_metric and goal_target is not None:
        goal_checks.append((goal_metric, float(goal_target), higher))
    try:
        for g in store.goals_for_experiment(experiment_id):
            m, t = g.get("metric"), g.get("target")
            if m and t is not None and m != goal_metric:
                goal_checks.append((m, float(t), bool(g.get("higher_better", True))))
    except Exception:  # noqa: BLE001
        pass

    if workflow is not None:
        from .workflows import improve_stages

        # A resumed loop only shows the remaining iterations (iterN..iterM).
        stages = improve_stages(iterations) if start_at <= 1 else [
            {"id": f"iter{i}", "label": f"Iteration {i}"}
            for i in range(start_at, iterations + 1)]
        await workflow.start(title=f"Improve {exp['name']}",
                             stages=stages)
        workflow.set_invoke(kind="improve", experiment_id=experiment_id,
                            prompt=(prompt or "").strip(),
                            iterations=iterations)

    runs_all = store.experiment_runs(experiment_id)
    best_val, best_id = best_metric(runs_all, goal_metric, higher) if goal_metric else (None, None)
    current_prompt = (prompt or "").strip() or f"Improve the experiment {exp['name']!r}."
    if start_at > 1:
        # Resume from the failed iteration's own prompt (recorded as the user
        # message tagged "iteration N" on the original run).
        try:
            for m in reversed(store.list_messages()):
                tags = (m.get("meta") or {}).get("tags") or []
                if m["role"] == "user" and f"iteration {start_at}" in tags:
                    current_prompt = m["content"]
                    break
        except Exception:  # noqa: BLE001
            pass
    last_suggestion = None
    last_applied_sid = None
    goal_reached = False
    stopped_reason = ""
    history: list[dict] = []
    regress_streak = 0

    # Attach loop-produced runs to the experiment.
    coordinator.ctx.experiment_id = str(experiment_id)
    # Branching lineage: iteration 1 derives from the experiment's best prior
    # run; each later iteration derives from the run the previous iteration
    # produced, so the branch-history graph shows the improvement chain.
    parent_run_id = best_id or (runs_all[-1]["id"] if runs_all else None)
    coordinator.ctx.parent_run_id = parent_run_id

    for i in range(start_at, iterations + 1):
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
        from .agents.coordinator import tool_mcp_action
        tools = (result or {}).get("tools") or []
        model = (result or {}).get("model") or ""
        mcp = action = ""
        extra: list[str] = []
        seen: set[str] = set()
        for t in tools:
            m, _ = tool_mcp_action(t.get("name", ""))
            if m and m != "core" and m not in seen:
                seen.add(m)
                extra.append(m)
        for t in reversed(tools):
            m, a = tool_mcp_action(t.get("name", ""))
            if mcp == "":
                mcp, action = m, a
            if a and a not in extra:
                extra.append(a)
                break
        itags = list(dict.fromkeys(["improve loop"] + extra))
        amid = store.add_message("assistant", text,
                                 {"tags": itags,
                                  "experiment_id": experiment_id,
                                  "mcp_name": mcp, "action": action,
                                  "tools": tools, "model": model})
        await emit("assistant_message", {"id": amid, "content": text,
                                         "tags": itags,
                                         "mcp_name": mcp, "action": action,
                                         "tools": tools, "model": model,
                                         "experiment_id": experiment_id,
                                         "created_at": _created(store, amid)})

        runs = store.list_runs()
        run = runs[-1] if runs else None
        if run is not None:
            # The next improvement iteration branches off this run.
            parent_run_id = run["id"]
            coordinator.ctx.parent_run_id = parent_run_id
        review = {"findings": [], "suggestions": []}
        try:
            from .agents.reviewer import build_review_context
            if reviewer:
                try:
                    review = await reviewer(build_review_context(store, run))
                except TypeError:
                    review = await reviewer()
        except Exception:  # noqa: BLE001
            review = {"findings": [], "suggestions": []}
        if run is not None:
            store.update_run_review(run["id"], review)
            # First-class suggestion records: persist + attach ids so the loop
            # can de-dup and measure each applied suggestion's outcome.
            sids = store.add_suggestions(experiment_id, run["id"], review)
            for s, sid in zip(review.get("suggestions") or [], sids):
                if isinstance(s, dict):
                    s["id"] = sid
        await emit("review", review)

        iter_metrics = dict(run.get("metrics") or {}) if run else {}
        mval = iter_metrics.get(goal_metric) if goal_metric else None
        if mval is not None:
            try:
                mval = float(mval)
            except (TypeError, ValueError):
                mval = None
        # Regression check for the previous iteration's applied suggestion:
        # did it actually improve the goal metric vs. the run it derived from?
        suggestion_delta = suggestion_improved = None
        if last_applied_sid is not None and run is not None:
            try:
                # Bind the applied suggestion to the run it produced before
                # resolving, so the regression check + learning are recorded.
                store.mark_suggestion_applied(last_applied_sid, run["id"])
                out = store.resolve_suggestion_outcome(last_applied_sid)
                if out is not None:
                    suggestion_delta = out.get("delta")
                    suggestion_improved = out.get("improved")
                    if suggestion_improved:
                        regress_streak = 0
                    elif suggestion_improved is not None:
                        regress_streak += 1
                    # Round-7: remember the measured outcome (knowledge memory).
                    try:
                        store.record_suggestion_learning(out)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        history.append({
            "iteration": i,
            "prompt": current_prompt,
            "run_id": run["id"] if run else None,
            "metrics": iter_metrics,
            "suggestion": last_suggestion,
            "goal_metric_value": mval,
            "suggestion_id": last_applied_sid,
            "delta": suggestion_delta,
            "improved": suggestion_improved,
        })

        if mval is not None and goal_metric:
            if best_val is None or (mval > best_val if higher else mval < best_val):
                best_val = mval
                best_id = run["id"] if run else None

        # Stop when any applicable target (experiment or Goals-panel) is reached.
        reached_any = None
        for cmetric, ctarget, chigher in goal_checks:
            cm = iter_metrics.get(cmetric)
            if cm is None:
                continue
            try:
                cm = float(cm)
            except (TypeError, ValueError):
                continue
            if cm >= ctarget if chigher else cm <= ctarget:
                reached_any = (cmetric, cm, ctarget)
                break
        if reached_any is not None:
            goal_reached = True
            stopped_reason = "goal reached"
            cmetric, cm, ctarget = reached_any
            await emit("notice", {"message": (
                f"Goal {cmetric} reached: {cm:.4g} vs target {ctarget:.4g} "
                f"in run #{run['id']}")})
            break

        # Regression stop: two consecutive applied suggestions that failed to
        # improve the goal mean the current direction is exhausted.
        if regress_streak >= 2:
            stopped_reason = ("no improvement in 2 consecutive applied suggestions")
            await emit("notice", {"message": (
                f"Improve loop stopped at iteration {i} — the last two applied "
                "suggestions did not improve the goal.")})
            break

        suggestion = _next_pending_suggestion(store, experiment_id, review)
        if suggestion is None:
            stopped_reason = "no further suggestions"
            await emit("notice", {"message": (
                f"Improve loop stopped at iteration {i} — the reviewer offered no "
                "further actionable suggestions.")})
            break
        last_suggestion = suggestion
        last_applied_sid = suggestion.get("id")
        current_prompt = suggestion.get("prompt") or suggestion.get("action")
    else:
        stopped_reason = f"iteration budget ({iterations}) spent"

    summary = _loop_summary(exp, history, best_val, best_id, goal_reached,
                            goal_metric, goal_target, higher, stopped_reason)
    if goal_reached:
        try:
            if store.get_experiment(experiment_id).get("status") == "active":
                store.update_experiment_status(experiment_id, "completed")
                summary += "\n\nExperiment marked **completed** — the target was reached."
                await emit("notice", {"message":
                                      "Goal reached — experiment marked completed."})
        except Exception:  # noqa: BLE001
            pass
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
        lines += ["", "| it | run | " + (goal_metric or "metric") + " | applied suggestion | delta |",
                  "|---|---|---|---|---|"]
        for h in history:
            mval = h.get("goal_metric_value")
            mstr = f"{mval:.4g}" if mval is not None else "—"
            sug = (h.get("suggestion") or {}).get("title") or "initial prompt"
            delta = h.get("delta")
            if delta is not None:
                dstr = (f"{delta:+.4g} {'✓' if h.get('improved') else '✗'}")
            else:
                dstr = "—"
            lines.append(f"| {h['iteration']} | #{h.get('run_id') or '—'} | "
                         f"{mstr} | {sug} | {dstr} |")
    return "\n".join(lines)


def _created(store, mid: int) -> float | None:
    """Timestamp for a WS message event emitted from the loop."""
    row = store.get_message(mid)
    return (row or {}).get("created_at")


def _next_pending_suggestion(store, experiment_id: int, review: dict) -> dict | None:
    """Pick the next not-yet-applied suggestion for an experiment.

    Prefers the freshly suggested ones (in order); excludes suggestions already
    applied in previous loop runs (their status is no longer 'pending'), so the
    loop never re-tries the same change blindly.
    """
    fresh = [s for s in (review or {}).get("suggestions") or []
             if isinstance(s, dict) and (s.get("prompt") or s.get("action"))]
    used = {s["id"] for s in store.list_suggestions(experiment_id)
            if s["status"] != "pending"}
    for s in fresh:
        if s.get("id") is None or s["id"] not in used:
            return s
    return None
