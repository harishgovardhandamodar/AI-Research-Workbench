"""Round-10: comprehensive project research report.

Aggregates everything the workbench has recorded — experiments (with the
leaderboard), campaigns, model benchmarks, learnings, recent runs (with
integrity status) and the audit summary — into one shareable markdown write-up.
Optionally prepends an LLM executive summary (best-effort).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .experiment_loop import best_metric
from .experiments import compare_campaigns, compare_experiments


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts)


def build_report_body(rt) -> str:
    """Deterministic markdown write-up of the whole project (no LLM summary).
    Runs on the event loop (the store connection is thread-bound)."""
    store = rt.store
    exps = store.list_experiments()
    campaigns = store.list_campaigns()
    evals = store.list_evals()
    learnings = store.list_learnings(limit=200)
    runs = store.list_runs(limit=50)
    suggestions = store.list_suggestions()
    try:
        audit_summary = rt.audit_store.summary() or {}
    except Exception:  # noqa: BLE001
        audit_summary = {}
    try:
        open_devs = rt.audit_store.count_open_deviations() or 0
    except Exception:  # noqa: BLE001
        open_devs = 0
    try:
        chain = rt.audit_store.verify_chain() or {}
        chain_ok = bool(chain.get("verified"))
    except Exception:  # noqa: BLE001
        chain_ok = False

    artifact_count = 0
    try:
        artifact_count = len(list(rt.artifacts.list()))
    except Exception:  # noqa: BLE001
        pass

    lines = [f"# Research report — {rt.name}", "",
             f"*Generated {_fmt_ts(time.time())} UTC*", "",
             f"- **Experiments**: {len(exps)} · **Runs**: {store.count_runs()} · "
             f"**Campaigns**: {len(campaigns)} · **Benchmarks**: {len(evals)} · "
             f"**Artifacts**: {artifact_count} · **Learnings**: {len(learnings)}",
             ""]

    # ---- executive summary --------------------------------------------------
    # (Handled by the async wrapper build_project_report — LLM calls can't run
    # inside this synchronous body.)
    # ---- experiments --------------------------------------------------------
    lines += ["## Experiments", ""]
    if exps:
        try:
            lb = compare_experiments(store, exps)
            lines += ["| # | experiment | " + (lb.get("metric") or "metric")
                      + " | Δ best | status |", "|---|---|---|---|---|"]
            for i, row in enumerate(lb.get("rows") or [], 1):
                best = f"{row['best']:.4g}" if row["best"] is not None else "—"
                db = (f"{row['delta_best']:+.3g}" if row.get("delta_best") is not None
                      else "—")
                lines.append(f"| {i} | {row['name']} | {best} | {db} | {row['status']} |")
        except Exception:  # noqa: BLE001
            pass
        lines.append("")
        for e in exps:
            lines.append(f"### {e.get('name')} (experiment #{e.get('id')})")
            if e.get("hypothesis"):
                lines.append(f"- Hypothesis: {e['hypothesis']}")
            goal = e.get("goal_metric") or ""
            target = e.get("goal_target")
            tgt = "" if target is None else f" → {target}"
            lines.append(f"- Goal: {goal}{tgt} · status: {e.get('status', 'active')}")
            if e.get("plan"):
                lines.append(f"- Plan: {e['plan']}")
            if goal:
                runs_e = store.experiment_runs(e.get("id"))
                b, bid = best_metric(runs_e, goal, bool(e.get("higher_better", True)))
                lines.append(f"- Best {goal}: {b:.4g} (run #{bid})"
                             if b is not None else f"- Best {goal}: —")
            lines.append("")
    else:
        lines += ["(no experiments yet)", ""]

    # ---- campaigns ----------------------------------------------------------
    lines += ["## Campaigns", ""]
    if campaigns:
        try:
            for row in (compare_campaigns(store, campaigns).get("rows") or []):
                best = f"{row['best']:.4g}" if row["best"] is not None else "—"
                lines.append(f"- **{row['name']}** ({row['status']}) — "
                             f"best {row.get('metric') or 'metric'}: {best}")
        except Exception:  # noqa: BLE001
            pass
    else:
        lines += ["(no campaigns)", ""]

    # ---- model benchmarks ---------------------------------------------------
    lines += ["## Model benchmarks", ""]
    if evals:
        for e in evals:
            lines.append(f"- **{e['name']}** ({e['status']}) — "
                         f"{len(e.get('models') or [])} model(s)")
            if e.get("report"):
                snippet = " ".join(e["report"].split())[:400]
                lines.append(f"  - {snippet}")
    else:
        lines += ["(no benchmarks)", ""]

    # ---- learnings ----------------------------------------------------------
    lines += ["## Learnings", ""]
    if learnings:
        for l in learnings:
            mark = "✓" if l.get("improved") == 1 else ("✗" if l.get("improved") == 0 else "·")
            lines.append(f"- {mark} {l['summary']}")
    else:
        lines += ["(no learnings recorded yet)", ""]

    # ---- recent runs --------------------------------------------------------
    lines += ["## Recent runs", ""]
    if runs:
        lines += ["| run | kind | label | " + ("metrics" if any(
            (r.get("metrics") or {}) for r in runs) else "") + " | integrity |",
                  "|---|---|---|---|---|"]
        for r in runs:
            m = (r.get("metrics") or {})
            mstr = ", ".join(f"{k}={v:.3g}" for k, v in list(m.items())[:4]) if m else "—"
            integ = "✓" if r.get("integrity_hash") else "—"
            lines.append(f"| #{r['id']} | {r.get('kind')} | "
                         f"{r.get('label') or '—'} | {mstr} | {integ} |")
    else:
        lines += ["(no runs)", ""]

    # ---- audit --------------------------------------------------------------
    lines += ["## Audit", ""]
    total = (audit_summary or {}).get("total_events") or audit_summary.get("events") or 0
    lines += [f"- Audit events: {total}",
              f"- Open deviations: {open_devs}",
              f"- Hash-chain verified: {'yes ✓' if chain_ok else 'no'}",
              f"- Suggestions tracked: {len(suggestions)}", ""]
    return "\n".join(lines)


async def build_project_report(rt, include_summary: bool = True) -> str:
    """The project report, optionally with an LLM executive summary prepended
    and a literature-grounded 'Related work' section."""
    body = build_report_body(rt)
    lit = ""
    try:
        from .literature import literature_context, project_question
        q = project_question(rt)
        if q:
            lit = await literature_context(q, limit=4)
    except Exception:  # noqa: BLE001
        lit = ""
    if lit:
        body = body.rstrip() + "\n\n## Related work\n\n" + lit + "\n"
    if not include_summary or getattr(rt, "llm", None) is None:
        return body
    try:
        summary = await _exec_summary(rt, body)
    except Exception:  # noqa: BLE001
        return body
    if not summary:
        return body
    return "\n".join([
        f"# Research report — {rt.name}", "",
        "## Executive summary", "",
        summary, "", "---", "", body])


async def _exec_summary(rt, base: str) -> str:
    """Best-effort LLM executive summary of the report so far."""
    prompt = (
        "You are writing the executive summary of a research project report. "
        "Given the facts below, write 3-6 concise sentences: the main objective, "
        "what was tried (experiments/campaigns/benchmarks), the key results "
        "(best metrics, what worked), and what was learned. Plain sentences, "
        "no markdown headings.\n\n"
        f"Facts:\n{base[:6000]}")
    resp = await rt.llm.complete([{"role": "user", "content": prompt}],
                                 temperature=0.2, tools=None)
    return (resp.get("content") or "").strip()[:4000]
