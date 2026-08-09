"""Experiment planning (round 30).

Turns an experiment's free-text `plan` (or a research idea) into an ordered,
runnable plan of steps — the same plan→steps idea campaigns already use, but for
a single experiment. Steps are first-class rows (`experiment_steps`) so the UI
can show progress, and the chat can run one step at a time.

Two ways to get a plan:
  - deterministic: split the plan text / hypothesis into numbered steps
  - LLM: propose a goal + plan grounded in prior learnings and the literature,
    falling back to a sensible default when the LLM is unavailable.
"""

from __future__ import annotations

import re
from typing import Any

MAX_STEPS = 6

_KIND_HINTS = [
    ("sweep", ("sweep", "grid", "grid-search", "hyperparameter", "tune ")),
    ("finetune", ("finetun", "fine-tun", "lora", "adapter")),
    ("eval", ("evaluate", "evaluation", "benchmark", "cross-valid", "compare",
              "ablation")),
    ("data", ("data", "dataset", "clean", "preprocess", "augment", "features")),
    ("model", ("model", "train", "fit", "svm", "nn", "regress", "forest")),
]


def _classify(text: str) -> str:
    low = (text or "").lower()
    for kind, hints in _KIND_HINTS:
        if any(h in low for h in hints):
            return kind
    return "experiment"


def plan_to_steps(plan_text: str, hypothesis: str = "") -> list[dict]:
    """Deterministically split a plan (or hypothesis) into ordered steps.

    Understands bulleted / numbered lines ("1. ...", "- ...", "* ...", "Step N:")
    and plain paragraphs (one step per sentence-ish block). Each step carries a
    title, kind, hypothesis and the raw plan snippet.
    """
    text = (plan_text or "").strip()
    if not text:
        if hypothesis.strip():
            return [{"title": "Baseline", "kind": "experiment",
                     "hypothesis": hypothesis.strip(),
                     "plan": "Run a baseline for this hypothesis and report the "
                             "goal metric."}]
        return []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Group contiguous non-bullet lines into paragraphs (a step each).
    blocks: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if re.match(r"^(\d+[.)]|[-*•]|Step\s+\d+[:.)])\s+", ln):
            if cur:
                blocks.append(" ".join(cur)); cur = []
            blocks.append(ln)
        else:
            cur.append(ln)
    if cur:
        blocks.append(" ".join(cur))

    steps: list[dict] = []
    for b in blocks[:MAX_STEPS]:
        clean = re.sub(r"^(?:Step\s+\d+[:.)]\s*|\d+[.)]\s*|[-*•]\s*)", "", b).strip()
        if not clean:
            continue
        title = clean[:80]
        steps.append({
            "title": title,
            "kind": _classify(clean),
            "hypothesis": "",
            "plan": clean,
        })
    if not steps:
        steps = [{"title": "Baseline", "kind": "experiment",
                  "hypothesis": hypothesis.strip() or plan_text.strip(),
                  "plan": plan_text.strip()}]
    return steps


def default_plan(exp: dict) -> list[dict]:
    """A sensible 3-step default plan for an experiment."""
    goal = (exp.get("goal_metric") or "").strip()
    return [
        {"title": "Baseline", "kind": "experiment",
         "hypothesis": (exp.get("hypothesis") or "").strip(),
         "plan": "Run a baseline experiment and report the goal metric"
                 + (f" ({goal})" if goal else " you care about.")},
        {"title": "Variation", "kind": "experiment",
         "hypothesis": "", "plan": "Try the most promising variation of the "
                                   "baseline and compare it."},
        {"title": "Best variant", "kind": "experiment",
         "hypothesis": "", "plan": "Confirm the best configuration found so far "
                                   "and summarize the outcome."},
    ]


async def propose_plan(store, llm, exp: dict) -> dict:
    """Propose a goal + plan for an experiment (LLM-grounded, with fallbacks).

    Returns {"goal_metric", "goal_target", "higher_better", "plan_text",
             "steps": [...]} where steps are the concrete plan.
    """
    goal = (exp.get("goal_metric") or "").strip()
    hypothesis = (exp.get("hypothesis") or "").strip()
    steps: list[dict] = []
    proposal: dict[str, Any] = {
        "goal_metric": goal, "goal_target": exp.get("goal_target"),
        "higher_better": bool(exp.get("higher_better", True)),
        "plan_text": (exp.get("plan") or "").strip(),
        "steps": [],
    }
    try:
        prior = ""
        try:
            learnings = store.list_learnings(metric=goal, limit=5) if goal else []
            if learnings:
                prior = "\nPrior learnings: " + "; ".join(
                    f"\"{l['summary']}\"" for l in learnings)
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            "You are planning an experiment for an autonomous experimentation "
            "workbench.\n"
            f"Experiment: {exp.get('name') or ''}\n"
            f"Hypothesis: {hypothesis or '(none)'}\n"
            f"Goal metric: {goal or '(none)'}"
            f"({'higher' if exp.get('higher_better', True) else 'lower'} is better).\n"
            f"{prior}\n"
            "Design 3-5 concrete steps, each something the agent can run with the "
            "workbench tools (run_python, run_sweep, start_run/finish_run, "
            "report_metric). Steps should build on each other (baseline → "
            "variations → best variant) and chase the goal. "
            "Reply with JSON only:\n"
            '{"goal_metric": "...", "goal_target": <number|null>, "higher_better": <bool>, '
            '"plan_text": "short free-text plan", "steps": [{"title", "kind": '
            '"experiment|sweep|finetune|eval|data", "hypothesis", "plan"}]}')
        resp = await llm.complete([{"role": "user", "content": prompt}],
                                  temperature=0.2, tools=None)
        proposal.update(_parse_proposal(resp.get("content") or ""))
    except Exception:  # noqa: BLE001
        proposal["steps"] = []
    if not proposal["steps"]:
        proposal["steps"] = plan_to_steps(proposal.get("plan_text")
                                          or (exp.get("plan") or ""), hypothesis)
    if not proposal["steps"]:
        proposal["steps"] = default_plan(exp)
    return proposal


def _parse_proposal(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    import json
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if "goal_metric" in data:
        out["goal_metric"] = str(data.get("goal_metric") or "").strip()
    if data.get("goal_target") is not None:
        try:
            out["goal_target"] = float(data["goal_target"])
        except (TypeError, ValueError):
            pass
    if "higher_better" in data:
        out["higher_better"] = bool(data.get("higher_better", True))
    if data.get("plan_text"):
        out["plan_text"] = str(data["plan_text"]).strip()
    steps = []
    for s in (data.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        if not title:
            continue
        steps.append({
            "title": title,
            "kind": str(s.get("kind") or "").strip() or _classify(title),
            "hypothesis": str(s.get("hypothesis") or "").strip(),
            "plan": str(s.get("plan") or "").strip(),
        })
    if steps:
        out["steps"] = steps
    return out


def step_prompt(exp: dict, step: dict) -> str:
    """A ready-to-send chat prompt that runs one plan step as an experiment
    variant, so the pipeline view captures it like any other turn."""
    lines = [f"Plan step {step.get('step_order')}: {step.get('title')}"]
    if step.get("hypothesis"):
        lines.append(f"Hypothesis: {step['hypothesis']}")
    if step.get("plan"):
        lines.append(f"Plan: {step['plan']}")
    goal = (exp.get("goal_metric") or "").strip()
    lines.append(
        f"Goal metric: {goal or '(none)'} "
        f"({'higher' if exp.get('higher_better', True) else 'lower'} is better). "
        "Run this step's variants with start_run/finish_run or run_sweep, report "
        "metrics with report_metric, and summarize how it compares to the baseline.")
    return "\n".join(lines)


__all__ = ["plan_to_steps", "default_plan", "propose_plan", "step_prompt",
           "MAX_STEPS"]
