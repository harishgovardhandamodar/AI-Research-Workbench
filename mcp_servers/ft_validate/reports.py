"""Report generation for ft-validate: JSON + Markdown, plus actionable
improvement suggestions derived from a run's failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import ValidateStore
from .models import ValidationRun

SCORE_LABELS = {
    "faithfulness": "Faithfulness (answer supported by retrieved evidence)",
    "accuracy": "Factual accuracy vs gold/evidence",
    "hallucination": "Hallucination rate (1 - evidence coverage; lower is better)",
    "retention": "Retention (base↔adapter agreement; forgetting proxy)",
}


def _verdict(v: float) -> str:
    if v >= 0.85:
        return "strong"
    if v >= 0.6:
        return "adequate"
    if v >= 0.4:
        return "weak"
    return "poor"


def render_report_markdown(run: ValidationRun) -> str:
    """Render a run's results to a human-readable Markdown report."""
    agg = run.aggregate or {}
    base = agg.get("base") or {}
    adapter = agg.get("adapter") or {}
    lines = [f"# Fine-Tune Validation Report — {run.id}",
             "",
             f"- **Eval set:** `{run.eval_set_id}`",
             f"- **Base model:** {run.base_model or '(not set)'}",
             f"- **Adapter:** {run.adapter_path or '(none — base vs base)'}",
             f"- **Questions:** {len(run.per_question)}",
             f"- **Status:** {run.status}",
             ""]
    if run.status != "done":
        lines += [f"> Run is `{run.status}`. {run.error or ''}", ""]
        return "\n".join(lines)

    lines.append("## Aggregate metrics")
    lines.append("")
    lines.append("| Metric | Base | Adapter | Verdict (adapter) |")
    lines.append("|--------|------|---------|-------------------|")
    all_keys = sorted(set(base) | set(adapter))
    for k in all_keys:
        b = base.get(k, {}).get("mean", "—")
        a = adapter.get(k, {}).get("mean", "—")
        av = _verdict(float(a)) if isinstance(a, float) else "—"
        lines.append(f"| {SCORE_LABELS.get(k, k)} | {b} | {a} | {av} |")
    lines.append("")

    if run.failures:
        lines.append("## Top failure cases (worst faithfulness)")
        lines.append("")
        for i, f in enumerate(run.failures[:5], 1):
            lines.append(f"### {i}. {f.get('question', '')}")
            lines.append("")
            lines.append(f"- **Score:** {f.get('score', '—')} ({f.get('metric', '')})")
            gold = f.get("gold_answer") or ""
            if gold:
                lines.append(f"- **Gold:** {gold[:200]}")
            ev = (f.get("evidence") or [""])[0]
            if ev:
                lines.append(f"- **Evidence:** {ev[:200]}")
            lines.append("")
    return "\n".join(lines)


def generate_report(store: ValidateStore, run_id: str,
                    report_format: str = "both") -> dict:
    """Materialize the run's report (Markdown + JSON on disk)."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    report_format = report_format or "both"
    if report_format not in ("json", "markdown", "both"):
        raise ValueError("report_format must be json/markdown/both")

    report = {"run_id": run.id, "status": run.status,
              "eval_set_id": run.eval_set_id, "base_model": run.base_model,
              "adapter_path": run.adapter_path, "aggregate": run.aggregate,
              "failures": run.failures, "per_question": run.per_question}
    paths: dict[str, str] = {}
    if report_format in ("markdown", "both"):
        md = render_report_markdown(run)
        p = store.reports_dir / f"{run_id}.md"
        p.write_text(md, encoding="utf-8")
        paths["markdown"] = str(p)
    if report_format in ("json", "both"):
        p = store.reports_dir / f"{run_id}.json"
        p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        paths["json"] = str(p)
    if paths:
        store.update_run(run_id, report_path=paths.get("markdown", ""))
    return {"run_id": run_id, "report_format": report_format,
            "paths": paths, "report": report}


def compare_base_vs_adapter(store: ValidateStore, run_id: str,
                            report_format: str = "both") -> dict:
    """Summarize base vs adapter deltas for a finished run + generate report."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    if run.status != "done":
        raise ValueError(f"run is '{run.status}', not 'done' — poll "
                         "get_validation_report until it finishes")
    base = (run.aggregate or {}).get("base", {})
    adapter = (run.aggregate or {}).get("adapter", {})
    deltas: dict[str, float] = {}
    for k in ("faithfulness", "accuracy", "hallucination", "retention"):
        b = base.get(k, {}).get("mean")
        a = adapter.get(k, {}).get("mean")
        if b is not None and a is not None:
            deltas[k] = round(a - b, 4)
    gen = generate_report(store, run_id, report_format)
    return {"run_id": run_id, "base": base, "adapter": adapter,
            "deltas": deltas, **gen}


def suggest_improvements(store: ValidateStore, run_id: str) -> dict:
    """Actionable re-training/data recommendations from a finished run."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    if run.status != "done":
        raise ValueError(f"run is '{run.status}', not 'done'")
    adapter = (run.aggregate or {}).get("adapter", {})
    suggestions: list[str] = []
    reasons: list[str] = []

    for metric, cfg in (("faithfulness", 0.8), ("accuracy", 0.8),
                        ("hallucination", 0.25)):
        mean = adapter.get(metric, {}).get("mean")
        if mean is None:
            continue
        if metric == "hallucination":
            if mean > cfg:
                suggestions.append(
                    "Reduce hallucination: add more Q&A pairs that explicitly "
                    "quote source passages, and prefer lower temperature "
                    "(<=0.3) at inference.")
                reasons.append(f"hallucination={mean:.2f} > {cfg}")
        elif mean < cfg:
            suggestions.append(
                f"Improve {metric}: add more training examples covering the "
                "failing sections, re-run generate_dataset with a stricter "
                "quality gate, and consider epochs=3.")
            reasons.append(f"{metric}={mean:.2f} < {cfg}")

    # Section-level recommendation from failure evidence sources.
    from collections import Counter
    srcs: Counter[str] = Counter()
    for f in run.failures:
        ev = f.get("evidence") or []
        if ev and isinstance(ev[0], str):
            srcs[ev[0]] += 1
    if srcs:
        top_src, cnt = srcs.most_common(1)[0]
        suggestions.append(
            f"Add more training data from '{top_src[:60]}' ({cnt} failing "
            "questions point there).")
        reasons.append(f"source '{top_src[:60]}' appears in {cnt} failure case(s)")

    if run.failures:
        suggestions.append(
            "Reserve a held-out eval set (heldout mode) and re-run verification "
            "after each training change to track the delta.")
        reasons.append(f"{len(run.failures)} failure cases ranked")

    return {"run_id": run_id, "suggestions": suggestions, "reasons": reasons,
            "retraining_note": (
                "Loosen quality_threshold or add data, then re-run "
                "dk-lora start_training with the same base model for a fair "
                "comparison.")}


def get_validation_report(store: ValidateStore, run_id: str) -> dict:
    from .verify import get_validation_report as _gvr
    return _gvr(store, run_id)


def list_validation_runs(store: ValidateStore) -> dict:
    from .verify import list_validation_runs as _lvr
    return _lvr(store)
