"""Round-5: research campaigns.

A campaign plans a multi-step research investigation, executes each step as its
own experiment through the live agent (reusing the goal/reviewer/sweep/git
machinery), and writes a synthesis report. It generalizes the single-file
autoresearch loop into whole studies.

The function runs inside a chat turn (under ``rt.lock``) and takes its
dependencies explicitly so it is unit-testable, mirroring
``experiment_loop.run_improve_loop``.
"""

from __future__ import annotations

import json
import re
import time

from .experiment_loop import best_metric
from .workflows import campaign_stages

DEFAULT_MAX_STEPS = 5


def _noop_emit(event: str, payload: dict):
    return None


def _parse_steps(text: str) -> list[dict]:
    """Extract a JSON array of step dicts from an LLM reply (robust)."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    steps = []
    for s in data[:DEFAULT_MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or s.get("hypothesis") or "step").strip()[:120]
        if not title:
            continue
        steps.append({
            "title": title,
            "kind": str(s.get("kind") or "experiment").strip() or "experiment",
            "hypothesis": str(s.get("hypothesis") or "").strip()[:400],
            "plan": str(s.get("plan") or "").strip()[:800],
        })
    return steps


def _step_prompt(c: dict, step: dict, idx: int, n: int) -> str:
    lines = [f"Campaign step {idx}/{n}: {step['title']}"]
    if step.get("hypothesis"):
        lines.append(f"Hypothesis: {step['hypothesis']}")
    if step.get("plan"):
        lines.append(f"Plan: {step['plan']}")
    metric = c.get("goal_metric") or ""
    lines.append(
        f"Goal metric: {metric or '(none)'} "
        f"({'higher' if c.get('higher_better', True) else 'lower'} is better). "
        "You are attached to this step's experiment — run its variants with "
        "start_run/finish_run or run_sweep, report metrics with report_metric, "
        "and summarize what you found and how it compares to the baseline.")
    return "\n".join(lines)


async def _plan_campaign(rt, c: dict, emit, plan_steps: list[dict] | None = None) -> list[dict]:
    """Produce the campaign's step list: from an explicit plan, or by asking the
    LLM, falling back to a sensible default. Persists the steps."""
    steps = list(plan_steps or [])
    if not steps:
        try:
            prior = ""
            try:
                learnings = rt.store.list_learnings(
                    metric=c.get("goal_metric") or "", limit=5)
                if learnings:
                    prior = "\nPrior learnings: " + "; ".join(
                        f"\"{l['summary']}\"" for l in learnings)
            except Exception:  # noqa: BLE001
                pass
            prompt = (
                "You are planning a research campaign for an autonomous "
                "experimentation workbench.\n"
                f"Campaign: {c['name']}\n"
                f"Research question: {c['research_question'] or '(none)'}\n"
                f"Goal metric: {c['goal_metric'] or '(none)'} "
                f"({'higher' if c.get('higher_better', True) else 'lower'} is better).\n"
                f"{prior}\n"
                "Design 3-5 concrete research steps, each an experiment the agent "
                "can run with the workbench tools (run_python, run_sweep, "
                "start_run/finish_run, report_metric). Steps should build on each "
                "other (baseline → ablations → best variant) and on any prior "
                "learnings above. "
                'Reply with JSON only, an array of objects: [{"title", "kind": '
                '"experiment|sweep|comparison", "hypothesis", "plan"}]')
            resp = await rt.llm.complete([{"role": "user", "content": prompt}],
                                         temperature=0.2, tools=None)
            steps = _parse_steps(resp.get("content") or "")
        except Exception:  # noqa: BLE001
            steps = []
    if not steps:
        steps = [
            {"title": "Baseline",
             "kind": "experiment",
             "hypothesis": c.get("research_question") or "Establish a baseline",
             "plan": "Run a baseline experiment and report the goal metric."},
            {"title": "Variation",
             "kind": "experiment",
             "hypothesis": "",
             "plan": "Try the most promising variation and compare it to the baseline."},
            {"title": "Best variant",
             "kind": "experiment",
             "hypothesis": "",
             "plan": "Confirm the best configuration found so far."},
        ]
    for i, s in enumerate(steps, 1):
        rt.store.add_campaign_step(c["id"], i, s.get("title") or f"Step {i}",
                                   s.get("kind") or "experiment",
                                   s.get("hypothesis") or "", s.get("plan") or "")
    return rt.store.list_campaign_steps(c["id"])


def _campaign_report(store, c: dict, steps: list[dict]) -> str:
    higher = bool(c.get("higher_better", True))
    goal = c.get("goal_metric") or ""
    lines = [f"# Research campaign: {c['name']}", "",
             f"**Research question**: {c.get('research_question') or '—'}",
             f"**Goal metric**: {goal or '(none)'} "
             f"({'higher' if higher else 'lower'} is better)",
             f"**Steps**: {len(steps)}", ""]
    for step in steps:
        lines.append(f"## Step {step['step_order']}: {step['title']} "
                     f"({step['kind']})")
        if step.get("hypothesis"):
            lines.append(f"- Hypothesis: {step['hypothesis']}")
        if step.get("status") != "done":
            lines.append(f"- Status: {step['status']}"
                         + (f" — {step['note']}" if step.get("note") else ""))
            continue
        exp = store.get_experiment(step["experiment_id"]) if step["experiment_id"] else None
        if exp is not None:
            runs = store.experiment_runs(exp["id"])
            lines.append(f"- Experiment #{exp['id']} · {len(runs)} run(s)")
            sg = exp.get("goal_metric") or goal
            if sg:
                best_val, best_id = best_metric(runs, sg, higher)
                lines.append(f"- Best {sg}: {best_val:.4g} (run #{best_id})"
                             if best_val is not None else f"- Best {sg}: —")
        if step.get("note"):
            lines.append(f"- Note: {step['note']}")
        lines.append("")
    return "\n".join(lines)


async def run_campaign(rt, coordinator, build_llm_messages, campaign_id: int,
                       emit=None, workflow=None, resume_step: int = 1,
                       plan_steps: list[dict] | None = None) -> dict:
    """Run a research campaign: plan → execute each step → synthesize.

    Returns {"campaign", "steps", "report", "stopped_reason"}. Steps run as their
    own experiments (lineage chained via parent_run_id); the final synthesis is a
    markdown report stored on the campaign, posted to chat, and saved as an
    artifact.
    """
    emit = emit or _noop_emit
    store = rt.store
    c = store.get_campaign(campaign_id)
    if c is None:
        return {"campaign": None, "steps": [], "report": "",
                "stopped_reason": f"campaign #{campaign_id} not found"}

    steps = store.list_campaign_steps(campaign_id)
    if not steps:
        steps = await _plan_campaign(rt, c, emit, plan_steps)
    if not steps:
        return {"campaign": c, "steps": [], "report": "",
                "stopped_reason": "campaign produced no steps"}

    store.update_campaign(campaign_id, status="running")
    if workflow is not None:
        await workflow.start(title=f"Campaign: {c['name']}",
                             stages=campaign_stages(len(steps)))
        workflow.set_invoke(kind="campaign", campaign_id=campaign_id,
                            step=max(1, int(resume_step or 1)))

    resume_step = max(1, min(int(resume_step or 1), len(steps)))
    higher = bool(c.get("higher_better", True))
    goal = c.get("goal_metric") or ""
    prev_best_run_id = None
    stopped_reason = ""

    for i, step in enumerate(steps):
        idx = i + 1
        if idx < resume_step:
            if step.get("experiment_id") and step.get("best_run_id"):
                prev_best_run_id = step["best_run_id"]
            continue
        if workflow is not None:
            await workflow.update_stage(
                f"step{idx}", "running",
                message=f"Step {idx}/{len(steps)}: {step['title']}")
        await emit("status", {"message": f"Campaign step {idx}/{len(steps)}: {step['title']}…"})

        eid = step.get("experiment_id")
        if eid is None:
            eid = store.create_experiment(
                f"[{c['name']}] {step['title']}", step.get("hypothesis") or "",
                goal, None, higher, plan=step.get("plan") or "")
            store.update_campaign_step(step["id"], experiment_id=eid)
        store.update_campaign_step(step["id"], status="running")

        coordinator.ctx.experiment_id = str(eid)
        coordinator.ctx.parent_run_id = prev_best_run_id
        step_prompt = _step_prompt(c, step, idx, len(steps))
        mid = store.add_message("user", step_prompt,
                                {"tags": ["campaign", f"step {idx}"],
                                 "experiment_id": eid})
        coordinator.ctx.message_id = str(mid)
        await emit("user_message", {"id": mid, "content": step_prompt,
                                    "tags": ["campaign"], "experiment_id": eid,
                                    "created_at": _created(store, mid)})

        try:
            result = await coordinator.run_turn(build_llm_messages())
        except Exception as e:  # noqa: BLE001
            stopped_reason = f"step {idx} failed: {type(e).__name__}: {e}"
            store.update_campaign_step(step["id"], status="failed",
                                       note=stopped_reason)
            if workflow is not None:
                await workflow.update_stage(f"step{idx}", "failed",
                                            message=stopped_reason)
            await emit("notice", {"message": f"Campaign {stopped_reason}"})
            break

        text = result.get("text", "")
        tags = ["campaign", f"step {idx}"]
        amid = store.add_message("assistant", text, {"tags": tags,
                                                     "experiment_id": eid})
        await emit("assistant_message", {"id": amid, "content": text,
                                         "tags": tags, "experiment_id": eid,
                                         "created_at": _created(store, amid)})

        runs = store.experiment_runs(eid)
        best_val, best_id = best_metric(runs, goal, higher) if goal else (None, None)
        if best_id is None and runs:
            best_id = runs[-1]["id"]
        store.update_campaign_step(step["id"], status="done", best_run_id=best_id)
        if workflow is not None:
            await workflow.update_stage(
                f"step{idx}", "done",
                message=f"Best {goal or 'metric'}: {best_val:.4g}" if best_val is not None else "done")
        prev_best_run_id = best_id

        # Persist the durable resume point (survives restart for background runs).
        if workflow is not None:
            workflow.set_invoke(kind="campaign", campaign_id=campaign_id,
                                step=idx + 1)

        # Reviewer pass per step (goal-grounded suggestions; round-2/3 machinery).
        try:
            if getattr(rt, "reviewer_enabled", True) and best_id is not None:
                from .agents.reviewer import Reviewer, build_review_context
                run = store.get_run(best_id)
                review = await Reviewer(rt.llm, store).review(
                    build_review_context(store, run))
                store.update_run_review(best_id, review)
                sids = store.add_suggestions(eid, best_id, review)
                for s, sid in zip(review.get("suggestions") or [], sids):
                    if isinstance(s, dict):
                        s["id"] = sid
                await emit("review", review)
        except Exception:  # noqa: BLE001
            pass

        await emit("notice", {"message": (
            f"Campaign step {idx} done — best {goal or 'metric'}: "
            f"{best_val:.4g}" if best_val is not None
            else f"Campaign step {idx} done.")})
        try:
            await rt.maybe_compact()
        except Exception:  # noqa: BLE001
            pass
        if coordinator.check_abort is not None and coordinator.check_abort():
            stopped_reason = "stopped by user"
            break

    if not stopped_reason:
        stopped_reason = f"{len(steps)} steps completed"
    report = _campaign_report(store, c, steps)
    status = "done" if not stopped_reason.startswith(("step", "stopped")) else "failed"
    store.update_campaign(campaign_id, status=status, report=report)
    report_mid = store.add_message(
        "assistant", report, {"tags": ["campaign", "report"]})
    await emit("assistant_message", {"id": report_mid, "content": report,
                                     "tags": ["campaign report"],
                                     "created_at": _created(store, report_mid)})
    # Persist the synthesis as a text artifact (provenance / reproducibility).
    try:
        from .artifacts.store import Artifact
        art = Artifact(kind="text", name=f"campaign-{campaign_id}-report",
                       description=f"Synthesis report for campaign #{campaign_id}",
                       code="# campaign report", env={},
                       run_id=prev_best_run_id, message_id=str(report_mid))
        rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
        await emit("artifact", {"artifact": art.to_dict()})
    except Exception:  # noqa: BLE001
        pass
    if workflow is not None:
        workflow.set_invoke(kind="campaign", campaign_id=campaign_id,
                            step=len(steps) + 1)
        await workflow.finish()
    await emit("notice", {"message": (
        f"Campaign '{c['name']}' {'complete' if status == 'done' else 'stopped'} "
        "— synthesis report generated.")})
    return {"campaign": store.get_campaign(campaign_id), "steps": steps,
            "report": report, "stopped_reason": stopped_reason}


def _created(store, mid: int) -> float | None:
    row = store.get_message(mid)
    return (row or {}).get("created_at")
