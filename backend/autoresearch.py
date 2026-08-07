"""Autonomous research (karpathy/autoresearch-style) loop.

Give an experimentation agent a small, single-file experiment and let it iterate
autonomously: propose an edit to ``experiment.py``, run it under a fixed wall-clock
budget, keep the change only when the goal metric improves, otherwise revert, and
log every attempt. Modeled on karpathy/autoresearch — the agent edits one target
file, the harness enforces a fixed time budget and a single comparable metric.

Everything lives under ``<project>/research/``:

    program.md       human-edited research instructions (the agent's job spec)
    experiment.py    the single file the agent modifies (must print a METRIC line)
    log.md           append-only experiment log (change / metric / kept / reverted)

Integration: each experiment run is recorded as a run on the Experiments
timeline (attached to an "autoresearch" experiment), reviewer suggestions can
guide the next iteration, and kept runs auto-commit to the management repo.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

from .workflows import improve_stages

METRIC_RE = re.compile(r"^METRIC\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9.eE+-]+)", re.M)

DEFAULT_PROGRAM_MD = """\
# Autonomous research program

You are the experimentation agent. Your single editable target is `experiment.py`.
The harness runs it under a **fixed time budget** and reads the final `METRIC` line:

    METRIC <name>=<value>

Rules:
1. Suggest ONE focused change per iteration (architecture / hyperparameters /
   algorithm / data). Output ONLY the complete new `experiment.py` in a single
   ```python``` code block — no prose around it.
2. Keep the experiment importable and fast enough to finish within the budget.
3. The final line printed must be `METRIC <goal_metric>=<value>`.
4. If the metric does not improve the change is reverted automatically, so focus
   on changes that should genuinely help.
"""

DEFAULT_EXPERIMENT = '''\
"""Autonomous-research target: edit this file to improve the metric.

Must print a final line: METRIC <name>=<value>
"""
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split


def make_data(n=1000, seed=7):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 8))
    y = (X[:, 0] + 0.5 * X[:, 1] + rng.normal(scale=0.3, size=n) > 0).astype(int)
    return train_test_split(X, y, test_size=0.3, random_state=seed)


def main():
    Xtr, Xte, ytr, yte = make_data()
    m = LogisticRegression(C=1.0, max_iter=200)
    m.fit(Xtr, ytr)
    acc = m.score(Xte, yte)
    print(f"METRIC accuracy={acc:.4f}")


if __name__ == "__main__":
    main()
'''


def ensure_research_dir(rt) -> dict:
    """Create <project>/research/ with program.md + experiment.py + log.md."""
    rdir = rt.dir / "research"
    rdir.mkdir(parents=True, exist_ok=True)
    prog = rdir / "program.md"
    if not prog.exists():
        prog.write_text(DEFAULT_PROGRAM_MD)
    exp = rdir / "experiment.py"
    if not exp.exists():
        exp.write_text(DEFAULT_EXPERIMENT)
    log = rdir / "log.md"
    if not log.exists():
        log.write_text("# Research log\n\n")
    return {"dir": rdir, "program": prog, "experiment": exp, "log": log}


def parse_code_block(text: str) -> str | None:
    """Extract the first fenced python code block from an agent reply."""
    if not text:
        return None
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def extract_metric(output: str, metric_name: str | None = None) -> tuple[str, float] | None:
    """Read the last 'METRIC name=value' line from the experiment output."""
    found = None
    for m in METRIC_RE.finditer(output or ""):
        found = m
    if found is None:
        return None
    name, val = found.group(1), float(found.group(2))
    if metric_name and name != metric_name:
        return None
    return name, val


def run_research_experiment(exp_file: Path, budget: int) -> dict:
    """Run experiment.py under a fixed wall-clock budget.

    Returns {"ok": bool, "metric": (name, value)|None, "output": str, "timed_out": bool}."""
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(exp_file)],
            cwd=str(exp_file.parent),
            capture_output=True, text=True, timeout=budget)
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        elapsed = time.time() - start
    except subprocess.TimeoutExpired:
        output = f"[timed out after {budget}s]"
        ok = False
        elapsed = budget
    metric = extract_metric(output)
    return {"ok": ok, "metric": metric, "output": output[-3000:], "elapsed": elapsed,
            "timed_out": not ok and elapsed >= budget}


def _append_log(log: Path, entry: str) -> None:
    try:
        with open(log, "a") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def _propose_prompt(program: Path, exp: Path, metric_name: str,
                    higher: bool, best, experiment_id: int, iteration: int) -> str:
    best_txt = f"{best:.4g}" if best is not None else "none yet"
    return (
        f"You are iteration {iteration} of an autonomous research loop for "
        f"experiment #{experiment_id}.\n\n"
        f"=== program.md ===\n{program.read_text()}\n\n"
        f"=== current experiment.py ===\n{exp.read_text()}\n\n"
        f"Goal metric: {metric_name} ({'higher' if higher else 'lower'} is better). "
        f"Current best: {best_txt}.\n"
        f"Propose ONE focused change and output the COMPLETE new experiment.py in "
        f"a single ```python``` code block.")


def _summary(history: list[dict], best, goal_metric, higher, goal_target,
             experiment_id: int) -> str:
    lines = [f"## Autoresearch — experiment #{experiment_id}",
             "",
             f"- **Goal metric**: {goal_metric} ({'↑' if higher else '↓'}"
             + (f" target {goal_target:g}" if goal_target is not None else "") + ")",
             f"- **Iterations run**: {len(history)}",
             f"- **Best {goal_metric}**: {best:.4g}" if best is not None else "- **Best**: none",
             "",
             "| it | metric | kept? | change (first line) |",
             "|---|---|---|---|"]
    for h in history:
        m = f"{h['metric']:.4g}" if h.get("metric") is not None else "—"
        lines.append(f"| {h['iteration']} | {m} | "
                     f"{'✓ kept' if h.get('kept') else '✗ reverted'} | "
                     f"{h.get('note', '')[:40] or '—'} |")
    lines += ["", "> Each run is recorded on the Experiments timeline; kept runs "
                  "auto-commit to the experiment repo if configured."]
    return "\n".join(lines)


async def run_autoresearch_loop(rt, coordinator, build_llm_messages,
                                config: dict | None = None,
                                emit=None, workflow=None) -> dict:
    """Run the autonomous research loop for a project.

    config keys: goal_metric, higher_better, goal_target, max_iters (<=30),
    per_iter_budget (seconds, >=10), max_reverts (stop after N consecutive reverts).
    """
    emit = emit or (lambda *_a, **_k: None)
    cfg = config or {}
    metric_name = (cfg.get("goal_metric") or "accuracy").strip()
    higher = bool(cfg.get("higher_better", True))
    goal_target = cfg.get("goal_target")
    max_iters = max(1, min(int(cfg.get("max_iters", 8)), 30))
    budget = max(10, int(cfg.get("per_iter_budget", 30)))
    max_reverts = max(0, int(cfg.get("max_reverts", 5)))

    files = ensure_research_dir(rt)
    exp_file = files["experiment"]
    log = files["log"]

    # Create / reuse the autoresearch experiment for the timeline.
    exps = rt.store.list_experiments()
    eid = next((e["id"] for e in exps if e["name"] == "autoresearch"), None)
    if eid is None:
        eid = rt.store.create_experiment(
            "autoresearch", "Autonomous research loop over experiment.py",
            metric_name, goal_target, higher,
            plan=f"Autoresearch: iterate edits to research/experiment.py under "
                 f"{budget}s budgets, keep improvements on {metric_name}.")
    else:
        rt.store.update_experiment_status(eid, "active")

    if workflow is not None:
        await workflow.start(title=f"Autoresearch: {metric_name}",
                             stages=improve_stages(max_iters))

    best = None
    best_src = None
    reverts_in_a_row = 0
    history: list[dict] = []
    stopped_reason = ""
    goal_reached = False

    try:
        for i in range(1, max_iters + 1):
            if workflow is not None:
                await workflow.update_stage(f"iter{i}", "running",
                                            message=f"Iteration {i}/{max_iters} — proposing a change…")
            await emit("status", {"message": f"Autoresearch — iteration {i}/{max_iters}: proposing…"})
            mid = rt.store.add_message(
                "user", _propose_prompt(files["program"], exp_file, metric_name,
                                        higher, best, eid, i),
                {"tags": ["autoresearch", f"iteration {i}"], "experiment_id": eid})
            coordinator.ctx.message_id = str(mid)
            coordinator.ctx.experiment_id = str(eid)
            # Branching lineage: each attempt derives from the previous attempt's
            # run (first iteration from the experiment's last prior run).
            try:
                prev_runs = rt.store.experiment_runs(eid)
                if history:
                    last_run = rt.store.list_runs()
                    coordinator.ctx.parent_run_id = (last_run[-1]["id"]
                                                     if last_run else None)
                elif prev_runs:
                    coordinator.ctx.parent_run_id = prev_runs[-1]["id"]
            except Exception:  # noqa: BLE001
                pass
            try:
                result = await coordinator.run_turn(build_llm_messages())
            except Exception as exc:  # noqa: BLE001
                stopped_reason = f"iteration {i} failed: {type(exc).__name__}: {exc}"
                await emit("notice", {"message": f"Autoresearch {stopped_reason}"})
                break

            new_code = parse_code_block(result.get("text", ""))
            if not new_code:
                await emit("notice", {"message":
                    f"Autoresearch iteration {i}: agent produced no code — skipping."})
                history.append({"iteration": i, "metric": None, "kept": False,
                                "note": "no code block"})
                continue

            snapshot = exp_file.read_text()
            exp_file.write_text(new_code)
            if workflow is not None:
                await workflow.update_stage(f"iter{i}", "running",
                                            message=f"Running under {budget}s budget…")
            await emit("status", {"message": f"Running experiment (≤{budget}s)…"})
            run = await asyncio.to_thread(run_research_experiment, exp_file, budget)

            m = run["metric"]
            metric_val = float(m[1]) if m else None
            improved = (metric_val is not None and
                        (best is None or (metric_val > best if higher else metric_val < best)))
            keep = run["ok"] and metric_val is not None and improved
            if keep:
                best = metric_val
                best_src = new_code
                reverts_in_a_row = 0
            else:
                exp_file.write_text(snapshot)
                reverts_in_a_row = reverts_in_a_row + 1 if run["ok"] and metric_val is not None else reverts_in_a_row

            entry = {
                "iteration": i,
                "metric": metric_val,
                "kept": keep,
                "note": (new_code.splitlines()[0] if new_code.splitlines() else "")[:60],
                "timed_out": run.get("timed_out"),
            }
            history.append(entry)
            _append_log(log, (f"- iter {i}: {metric_name}={metric_val:.4g} → "
                              f"{'kept' if keep else 'reverted'} ({entry['note']})"))

            rt.store.add_run(
                prompt=f"autoresearch iter {i}: {entry['note']}",
                reply=run["output"][:1000],
                status="done" if run["ok"] else "error",
                started_at=time.time() - run["elapsed"], finished_at=time.time(),
                metrics={metric_name: metric_val} if metric_val is not None else {},
                experiment_id=eid,
                config={"kept": keep, "budget": budget,
                        "reverted": not keep, "iteration": i},
                label=f"iter {i}" + (" ✓" if keep else " ✗"),
                kind="autoresearch")
            await emit("notice", {"message": (
                f"Autoresearch iter {i}: {metric_name}={'%.4g' % metric_val if metric_val is not None else '—'} → "
                f"{'kept' if keep else 'reverted'}")})

            if workflow is not None:
                await workflow.update_stage(f"iter{i}", "done",
                                            message=f"{metric_name}={metric_val:.4g}" if metric_val is not None else "no metric")

            if metric_val is not None and goal_target is not None:
                if (metric_val >= goal_target if higher else metric_val <= goal_target):
                    goal_reached = True
                    stopped_reason = "goal reached"
                    break
            if reverts_in_a_row >= max_reverts:
                stopped_reason = f"{max_reverts} consecutive reverts — no improvement trend"
                break

        if not stopped_reason:
            stopped_reason = f"iteration budget ({max_iters}) spent"

        summary = _summary(history, best, metric_name, higher, goal_target, eid)
        rt.store.add_message("assistant", summary,
                             {"tags": ["autoresearch", "summary"], "experiment_id": eid})
        await emit("assistant_message", {"content": summary,
                                         "tags": ["autoresearch summary"]})
    finally:
        coordinator.ctx.experiment_id = ""
        if workflow is not None:
            await workflow.finish()

    return {"summary": summary, "iterations": history,
            "best": best, "goal_reached": goal_reached,
            "stopped_reason": stopped_reason,
            "research_dir": str(files["dir"])}
