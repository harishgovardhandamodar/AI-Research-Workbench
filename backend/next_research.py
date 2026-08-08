"""Round-12: proactive 'next research' agenda.

Derives what to do next from the recorded project state — experiments below
target, unfinished campaigns/evals, no-gain learnings, open goals, and untested
models — so the user (or a fresh campaign) knows where to go.
"""

from __future__ import annotations

from .experiments import compare_experiments


def next_research_agenda(rt) -> str:
    """Deterministic agenda of open threads + what worked/didn't."""
    store = rt.store
    lines = ["## Suggested next research", ""]

    # Experiments with an unreached target.
    try:
        lb = compare_experiments(store, store.list_experiments())
        below = [r for r in lb.get("rows") or []
                 if r.get("target") is not None and r.get("best") is not None
                 and r.get("to_target") is not None and r["to_target"] > 0]
        if below:
            lines.append("**Push toward target**:")
            for r in below[:5]:
                lines.append(f"- {r['name']}: best {r['best']:.4g}, "
                             f"target {r['target']} "
                             f"({r['to_target']:+.3g} to go, run #{r['best_run_id']})")
            lines.append("")
    except Exception:  # noqa: BLE001
        pass

    # Unfinished campaigns / evals.
    try:
        unfinished = [c for c in store.list_campaigns() if c["status"] != "done"]
        if unfinished:
            lines.append("**Unfinished campaigns**: " + "; ".join(
                f"{c['name']} ({c['status']})" for c in unfinished[:5]) + "\n")
        unevals = [e for e in store.list_evals() if e["status"] != "done"]
        if unevals:
            lines.append("**Unfinished benchmarks**: " + "; ".join(
                f"{e['name']} ({e['status']})" for e in unevals[:5]) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # Open Goals-panel goals.
    try:
        open_goals = [g for g in store.list_goals()
                      if not _goal_reached(store, g)]
        if open_goals:
            lines.append("**Open goals**: " + "; ".join(
                f"{g.get('label') or g['metric']} "
                f"{'↑' if g['higher_better'] else '↓'} {g['target']}"
                for g in open_goals[:5]) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # Learnings: what worked / didn't.
    try:
        learnings = store.list_learnings(limit=200)
        good = [l for l in learnings if l.get("improved") == 1]
        bad = [l for l in learnings if l.get("improved") == 0]
        if bad:
            lines.append("**What didn't work**: " + "; ".join(
                l["summary"] for l in bad[:4]) + "\n")
        if good:
            lines.append("**What worked**: " + "; ".join(
                l["summary"] for l in good[:4]) + "\n")
    except Exception:  # noqa: BLE001
        pass

    if len(lines) == 2:
        lines.append("No obvious open threads — the project looks settled. "
                     "Try a new research question, a model benchmark, or a fresh "
                     "campaign.")
    return "\n".join(lines)


async def suggest_next_research(rt) -> str:
    """The deterministic agenda plus (best-effort) an LLM paragraph proposing a
    concrete next campaign, and a note on untested models."""
    agenda = next_research_agenda(rt)
    if getattr(rt, "llm", None) is None:
        return agenda
    try:
        agenda += "\n\n## Suggested next campaign\n\n"
        prompt = (
            "Based on this research agenda, propose ONE concrete next research "
            "campaign (2-4 sentences): the question, the goal metric, and the "
            "first 2-3 experiments. Plain text, no markdown.\n\n"
            f"Agenda:\n{agenda[:4000]}")
        resp = await rt.llm.complete([{"role": "user", "content": prompt}],
                                     temperature=0.3, tools=None)
        proposal = (resp.get("content") or "").strip()
        if proposal:
            agenda += proposal
    except Exception:  # noqa: BLE001
        pass
    return agenda


def _goal_reached(store, goal: dict) -> bool:
    """Whether any run satisfies a Goals-panel goal (best-effort check)."""
    metric = goal.get("metric") or ""
    target = goal.get("target")
    higher = bool(goal.get("higher_better", True))
    if not metric or target is None:
        return False
    best = None
    try:
        for r in store.list_runs(limit=2000):
            v = (r.get("metrics") or {}).get(metric)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if best is None or (v > best if higher else v < best):
                best = v
    except Exception:  # noqa: BLE001
        return False
    return best is not None and (best >= target if higher else best <= target)
