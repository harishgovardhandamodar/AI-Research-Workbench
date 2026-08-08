"""Local - Open - Agentic Experimentation Workbench: FastAPI backend.

Serves the web UI + WebSocket chat. The REST API lives in backend/routers/; this
module owns the app assembly (CORS, lifespan, router mounting), the chat
WebSocket handler, the deterministic workflow/notebook intents, and static file
serving. Each project gets its own folder under <workbench>/projects with SQLite
persistence, artifact storage and a persistent Python kernel.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agents.approval import ApprovalBroker
from .agents.coordinator import Coordinator, TurnAborted
from .agents.reviewer import Reviewer
from .artifacts.store import Artifact
from .experiments import findings_from_run, metrics_from_run
from .experiment_loop import run_improve_loop
from .experiment_repo import management_repo_dir, maybe_autocommit
from .llm import LLMError
from .paths import FRONTEND_DIR, PROJECTS_DIR, ROOT
from .permissions import AllowAllPermissionManager
from .project_runtime import ProjectRuntime
from .routers import artifacts, notebooks, projects, runs, system
from .state import (CONFIG, allowed_origins, get_runtime, mcp_registry,
                    origin_allowed, runtimes)


# ------------------------------------------------------------------ app -----

_rkg_scheduler: "ScenarioScheduler | None" = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    global _rkg_scheduler
    from .research_knowledge_graphs.config import Config as RkgConfig
    from .research_knowledge_graphs.scheduler import ScenarioScheduler
    from .research_knowledge_graphs.router import get_workbench

    rkg_cfg = RkgConfig()
    if rkg_cfg.schedule_enabled:
        _rkg_scheduler = ScenarioScheduler(
            get_workbench,
            check_minutes=rkg_cfg.schedule_check_minutes,
            synthesize=rkg_cfg.schedule_synthesize,
        )
        _rkg_scheduler.start()
        app.state.rkg_scheduler = _rkg_scheduler
    from .research_knowledge_graphs.router import set_scheduler

    set_scheduler(_rkg_scheduler)
    yield
    if _rkg_scheduler is not None:
        await _rkg_scheduler.stop()
        _rkg_scheduler = None
    set_scheduler(None)
    for rt in list(runtimes.values()):
        await rt.stop()


app = FastAPI(title="Local - Open - Agentic Experimentation Workbench", lifespan=lifespan)
if allowed_origins():
    # Cross-origin REST access is opt-in (same-origin needs no CORS headers).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins()), allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

app.include_router(system.router)
app.include_router(projects.router)
app.include_router(runs.router)
app.include_router(artifacts.router)
app.include_router(notebooks.router)
from .routers.audit import router as audit_router

app.include_router(audit_router)
from .routers.kernel import router as kernel_router

app.include_router(kernel_router)
from .research_knowledge_graphs.router import router as rkg_router

app.include_router(rkg_router)


# ---------------------------------------------------------- workflows --------

PRIVACY_WORKFLOW = {
    "name": "peer_privacy",
    "title": "Privacy peer-exploitation / red-team / DP robustness",
    "script": "examples/privacy/run_peer_exploitation.py",
    "report_dir": "examples/privacy/reports",
    "trigger_words": [
        "peer", "distribut", "red team", "corner case", "audit trail",
        "ideation", "exploit", "population", "robustness", "differential privacy",
        "obfuscation",
    ],
}

# Injected as a system message for god-mode turns (full access, quarantined).
GODMODE_SYSTEM = (
    "You are running in GOD MODE with FULL ACCESS inside a quarantined sandbox. "
    "All permission checks are auto-approved: you may run any shell command, "
    "install packages, download data, and run experiments without asking.\n"
    "CONTAINMENT REQUIREMENT: do ALL work (files, installs, downloads, generated "
    "artifacts) inside the quarantine folder {dir} — never write outside it.\n"
    "Run the requested experiment thoroughly, then summarize what you did, the "
    "results, and the files produced under {dir}."
)


def match_workflow(text: str) -> str | None:
    """Detect a workflow-intent prompt (e.g. the privacy peer-exploitation prompt).

    Returns the workflow name when enough signature terms are present, so the
    researcher can trigger the pipeline with a plain chat message (no LLM
    required — deterministic).
    """
    low = (text or "").lower()
    if not ("privacy" in low or "red team" in low or "exploit" in low):
        return None
    hits = sum(1 for w in PRIVACY_WORKFLOW["trigger_words"] if w in low)
    return PRIVACY_WORKFLOW["name"] if hits >= 3 else None


# Words that make the workflow re-run with a NEW random seed (fresh results).
FRESH_WORKFLOW_WORDS = [
    "rerun", "re-run", "fresh", "new result", "new results", "new seed",
    "random seed", "randomize", "run again", "different result",
    "different results", "force rerun", "force re-run", "fresh results",
]

# Words that make the workflow COMPARE past runs instead of running a new one.
COMPARE_WORKFLOW_WORDS = ["compare", "comparison", "comparing"]


def fresh_requested(text: str) -> bool:
    """True when the prompt asks to force a fresh rerun with new results."""
    low = (text or "").lower()
    return any(w in low for w in FRESH_WORKFLOW_WORDS)


def compare_requested(text: str) -> bool:
    """True when the user asks to COMPARE the privacy-workflow runs.

    Only fires when there is a clear privacy-workflow context, so ordinary
    requests like "compare how DP protects data" are NOT hijacked into the
    privacy-run comparison.
    """
    low = (text or "").lower()
    if not any(w in low for w in COMPARE_WORKFLOW_WORDS):
        return False
    return any(w in low for w in (
        "privacy run", "privacy workflow", "workflow run", "experiment run",
        "results of the privacy", "results of the runs", "compare the runs",
        "compare results", "compare the privacy", "run comparison"))


def rerun_compare_requested(text: str) -> bool:
    """A fresh rerun AND a comparison in one command, e.g. "rerun with new seed
    and compare with last run" (the Fresh-rerun + Compare-runs quick actions).

    Requires both a rerun/fresh word and a compare word, so generic "compare X
    and Y" chats are not hijacked."""
    low = (text or "").lower()
    return (any(w in low for w in FRESH_WORKFLOW_WORDS)
            and any(w in low for w in COMPARE_WORKFLOW_WORDS))


# A "rerun" the researcher can trigger for a specific notebook:
# e.g. "rerun with fresh results Run 26_adversarial_model_comparison".
NOTEBOOK_RUN_WORDS = ["run", "rerun", "re-run", "execute"]
FRESH_RERUN_WORDS = ["fresh", "rerun", "re-run", "new seed", "different results",
                     "new results", "run again"]


def match_notebook_run(text: str) -> tuple | None:
    """Detect 'run/rerun <notebook>' intent. Returns (name, fresh) or None."""
    low = (text or "").lower()
    if not any(w in low for w in NOTEBOOK_RUN_WORDS):
        return None
    names = []
    nb_dir = ROOT / "examples" / "notebooks"
    if nb_dir.exists():
        names = sorted((f.stem for f in nb_dir.glob("*.ipynb")), key=len, reverse=True)
    found = next((n for n in names if n in low), None)
    if not found:
        return None
    fresh = any(w in low for w in FRESH_RERUN_WORDS)
    return found, fresh


def has_analysis_intent(text: str) -> bool:
    """True when the prompt asks for a parameterised deep analysis (seeds/DP)."""
    low = (text or "").lower()
    return any(w in low for w in ("seed", "epsilon", "differential privacy",
                                  "protect", "compare how"))


def _nb_artifact_cb(rt: ProjectRuntime, emit):
    async def on_artifact(fig_b64: str, source: str):
        env = await rt.kernels.get_env()
        art = Artifact(kind="figure", name="notebook-figure",
                       description="Figure produced by a notebook cell",
                       code=source, env=env, message_id="")
        rt.artifacts.add_artifact(art, data=base64.b64decode(fig_b64), data_type="png")
        if emit:
            try:
                await emit("artifact", {"artifact": art.to_dict()})
            except Exception:  # noqa: BLE001
                pass
        return art
    return on_artifact


async def run_notebook_intent(rt: ProjectRuntime, emit, name: str,
                              fresh: bool, message_id: str = "") -> str:
    """Execute a notebook (fresh seed when requested) and summarize the results."""
    from .notebooks import NotebookError
    svc = rt.notebooks
    try:
        svc.load(name)
    except NotebookError as e:
        return f"[error] {e}"
    prelude = ""
    seed_used = None
    if fresh:
        import random as _r
        seed_used = _r.randint(1, 10**9)
        prelude = f"import os; os.environ['FOX_RUN_SEED']='{seed_used}'"

    collected = []

    async def on_artifact(fig_b64, source):
        env = await rt.kernels.get_env()
        art = Artifact(kind="figure", name="notebook-figure",
                       description=f"Figure produced by notebook '{name}'",
                       code=source, env=env, message_id=message_id)
        rt.artifacts.add_artifact(art, data=base64.b64decode(fig_b64), data_type="png")
        collected.append({"name": art.name, "id": art.id})
        if emit:
            try:
                await emit("artifact", {"artifact": art.to_dict()})
            except Exception:  # noqa: BLE001
                pass
        return art

    wf = getattr(rt, "workflow", None)
    if wf is not None:
        from .workflows import generic_stage

        await wf.start(title=f"Notebook {name}", stages=generic_stage("Executing notebook"))
        await wf.update_stage("working", "running", message=f"Executing {name}")
    try:
        res = await svc.execute(name, on_artifact=on_artifact, prelude=prelude)
        nb = res["notebook"]
    except Exception:
        if wf is not None:
            await wf.finish()
        raise
    if wf is not None:
        await wf.update_stage("working", "done", message="Notebook complete")
        await wf.finish()

    metrics = {}
    try:
        resp = await rt.kernels.python.run_code(
            "import json\nfrom examples.adversarial import adversarial_data as _ad\n"
            "print(json.dumps(_ad.LAST_RESULT))")
        out = (resp.get("output") or "").strip()
        if out:
            parsed = json.loads(out.splitlines()[-1])
            if isinstance(parsed, dict):
                metrics = {k: v for k, v in parsed.items()
                           if isinstance(v, (int, float))}
    except Exception:  # noqa: BLE001
        pass
    try:
        rt.store.add_run(
            prompt=f"run notebook {name}",
            reply="\n".join(f"cell {r['index']}: "
                            f"{'ok' if r['ok'] else 'FAILED'}" for r in res["report"]),
            status="done",
            started_at=time.time(), finished_at=time.time(),
            artifact_ids=[a["id"] for a in collected],
            metrics=metrics,
            kind="notebook",
            label=name,
            config={"seed": seed_used, "fresh": bool(fresh)},
        )
    except Exception:  # noqa: BLE001
        pass

    lines = [f"Executed **{name}**" + (" with a **fresh seed** (results differ)" if fresh
                                       else "") + f" — {len(res['report'])} code cell(s)."]
    for r in res["report"]:
        if r["ok"]:
            lines.append(f"- cell {r['index']}: ok" +
                         (f" ({r['figures']} figure(s))" if r["figures"] else ""))
        else:
            lines.append(f"- cell {r['index']}: **FAILED** — {r['error'][:160]}")
    outs = []
    for c in nb.get("cells", []):
        for o in (c.get("outputs") or []):
            if o.get("output_type") == "stream":
                outs.append("".join(o.get("text", [])))
    if outs:
        lines.append("\n```text\n" + "\n".join(outs)[-2500:] + "\n```")
    try:
        from .audit import emit_tool_audit

        await emit_tool_audit(
            rt.audit_emitter, agent_id="Fox", session_id=rt.name,
            trace_id=message_id or None, tool_name="run_notebook",
            method="run_notebook", args={"notebook": name, "fresh": fresh},
            result="\n".join(lines), ok=True,
            duration_ms=0.0, source="coordinator")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


# Meaningful tags shown on chat messages so experiments are recognisable at a
# glance (rendered as small badges next to the message text).
def message_tags(role: str, text: str) -> list[str]:
    tags: list[str] = []
    low = (text or "").lower()
    if role == "user":
        if match_workflow(text) or rerun_compare_requested(text):
            tags.append("privacy workflow")
            if fresh_requested(text) or rerun_compare_requested(text):
                tags.append("fresh rerun")
            if compare_requested(text) or rerun_compare_requested(text):
                tags.append("compare runs")
        elif "obfusc" in low:
            tags.append("obfuscation")
        elif "privacy" in low or "differential privacy" in low:
            tags.append("privacy")
        elif "notebook" in low or ".ipynb" in low:
            tags.append("notebook")
        elif any(k in low for k in ("plot", "figure", "chart", "graph")):
            tags.append("figure")
        elif any(k in low for k in ("analysis", "experiment", "run ",
                                    "compute", "simulate", "data")):
            tags.append("experiment")
        else:
            tags.append("question")
    elif role == "assistant":
        if low.startswith("## privacy workflow — run comparison"):
            tags.append("comparison")
        elif "privacy workflow" in low:
            tags.append("privacy workflow")
        if "fresh rerun" in low:
            tags.append("fresh rerun")
        if not tags:
            tags.append("agent reply")
        if "error" in low[:300]:
            tags.append("error")
    return tags


def tool_turn_tags(tools: list[dict]) -> list[str]:
    """Derive navigable tags from a turn's tool trail: the distinct MCP servers
    touched (e.g. 'github', 'eda_profiler') plus the final action, so bubbles
    can be recognised and filtered without the noise of every intermediate
    tool call."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tools or []:
        mcp = (t.get("mcp") or "").strip()
        if mcp and mcp != "core" and mcp not in seen:
            seen.add(mcp)
            out.append(mcp)
    for t in reversed(tools or []):
        action = (t.get("action") or "").strip()
        if action and action not in out:
            out.append(action)
            break
    return out


def tool_turn_label(tools: list[dict]) -> tuple[str, str]:
    """The (mcp, action) pair that best labels a turn's final bubble — the last
    tool executed, since the reply usually follows straight from it."""
    for t in reversed(tools or []):
        mcp = (t.get("mcp") or "").strip()
        action = (t.get("action") or "").strip()
        if mcp and action:
            return mcp, action
    return "", ""


async def run_privacy_workflow(rt: ProjectRuntime, emit,
                               fresh: bool = False, compare: bool = False,
                               prompt: str = "") -> str:
    """Run (or compare) the privacy workflow and register outputs as artifacts."""
    script = PRIVACY_WORKFLOW["script"]
    base_report_dir = ROOT / PRIVACY_WORKFLOW["report_dir"]
    runs_file = PROJECTS_DIR.parent / "privacy_runs.json"   # persistent volume
    out_dir = base_report_dir / "compare" if compare else base_report_dir
    args = [script, "--runs-file", str(runs_file), "--out-dir", str(out_dir)]
    if fresh:
        args.append("--fresh")
    if compare:
        args.append("--compare")
    wf = getattr(rt, "workflow", None)
    if wf is not None and not compare:
        from .workflows import PRIVACY_STAGES

        await wf.start(title="Privacy workflow", stages=PRIVACY_STAGES)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args, cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        # Stream stdout so the workflow panel shows live stage progress.
        out_b = bytearray()
        if proc.stdout is not None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                out_b += line
                if wf is not None and not compare:
                    txt = line.decode(errors="replace").upper()
                    for sid, marker in (("stage1", "STAGE 1"), ("stage2", "STAGE 2"),
                                        ("stage3", "STAGE 3")):
                        if marker in txt:
                            await wf.update_stage(sid, "done",
                                                  message=line.decode(errors="replace").strip()[:90])
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        out, err = b"", b"[timeout] workflow exceeded 600s"
    summary = (bytes(out_b) or out).decode(errors="replace")
    if err:
        summary += "\n[stderr]\n" + err.decode(errors="replace")[-2000:]
    if wf is not None and not compare:
        await wf.update_stage("report", "done", message="Report & artifacts ready")
        await wf.finish()

    report_dir = out_dir
    artifact_names = []
    artifact_refs = []
    fig_links = []
    if report_dir.exists():
        try:
            env = await rt.kernels.get_env()
        except Exception:  # noqa: BLE001
            env = {}
        for f in sorted(report_dir.iterdir()):
            if f.suffix == ".md":
                art = Artifact(kind="text", name=f.stem,
                               description=f"Privacy workflow report: {f.stem}",
                               code="# privacy workflow (autogenerated)", env=env, message_id="")
                rt.artifacts.add_artifact(art, data=f.read_bytes(), data_type="text")
            elif f.suffix == ".png":
                art = Artifact(kind="figure", name=f.stem,
                               description=f"Privacy workflow figure: {f.stem}",
                               code="# privacy workflow (autogenerated)", env=env, message_id="")
                rt.artifacts.add_artifact(art, data=f.read_bytes(), data_type="png")
                fig_links.append((art.name, art.id))
            else:
                continue
            artifact_names.append(art.name)
            artifact_refs.append({"name": art.name, "id": art.id})
            if emit:
                try:
                    await emit("artifact", {"artifact": art.to_dict()})
                except Exception:  # noqa: BLE001
                    pass

    # Attach the produced artifact names to the run's history record so the
    # Experiments tab can link each run to its reports/figures.
    if not compare and runs_file.exists():
        try:
            runs = json.loads(runs_file.read_text())
            if runs and isinstance(runs, list):
                runs[-1]["artifacts"] = artifact_refs
                runs_file.write_text(json.dumps(runs, indent=2))
        except (json.JSONDecodeError, OSError):
            pass

    # Build a chat message that includes the run summary AND the full report
    # (audit trail or comparison), with figures embedded inline.
    report_file = "compare_report.md" if compare else "audit_trail.md"
    report_md = (report_dir / report_file).read_text() if (report_dir / report_file).exists() else ""
    summary_block = "\n".join(
        f"    {ln}" if ln.strip() else ln for ln in summary.splitlines())

    # Resolve inline figure placeholders (<!-- FIGURE:name -->) to artifact links.
    fig_by_name = {name: aid for name, aid in fig_links}
    if fig_by_name:
        import re as _re
        report_md = _re.sub(
            r"<!-- FIGURE:(\S+) -->",
            lambda m: f"![{m.group(1)}](/artifacts/{fig_by_name[m.group(1)]})",
            report_md)

    if compare:
        lines = [
            "## Privacy workflow — run comparison",
            "",
            "Compared the **stored workflow runs** (run history is kept in the "
            "project volume). The comparison report and figure below are also "
            "saved as artifacts.",
        ]
        if report_md:
            lines += ["", "### Comparison report", "", report_md]
        lines += [
            "",
            "> Tip: run the workflow a few more times (e.g. add \"rerun with "
            "fresh results\") to build up more runs to compare.",
        ]
    else:
        lines = [
            "## Privacy workflow — peer exploitation · red team · DP robustness",
            "",
            "The workflow ran **3 stages** on synthetic credit-card data "
            "(obfuscation-study generator) and produced the reports below, which "
            "are also saved as artifacts.",
        ]
        if fresh:
            lines += [
                "",
                "> **Fresh rerun** — this run used a new random seed, so the "
                "numbers, figures and reports below are **new** and differ from "
                "previous runs.",
            ]
        lines += [
            "",
            "### Run summary",
            "",
            "```text",
            summary_block[:6000],
            "```",
        ]
        if report_md:
            lines += ["", "### Full report (audit trail)", "", report_md]
    lines += [
        "",
        "> Artifacts registered: " + ", ".join(artifact_names),
    ]
    message = "\n".join(lines)

    # Record this workflow run in the project's own runs table (same source of
    # truth as agent runs) so the Experiments tab shows it with its metrics,
    # findings and artifacts. Comparisons don't produce new stage data, so only
    # real workflow executions are recorded.
    if not compare:
        try:
            last = {}
            if runs_file.exists():
                data = json.loads(runs_file.read_text())
                if data and isinstance(data, list):
                    last = data[-1]
            # Branching lineage: fresh reruns derive from the previous workflow
            # run so the branch-history graph chains them.
            parent_run_id = None
            try:
                prior = [r for r in rt.store.list_runs()
                         if r.get("kind") == "privacy_workflow"]
                if prior:
                    parent_run_id = prior[-1]["id"]
            except Exception:  # noqa: BLE001
                pass
            rt.store.add_run(
                prompt=prompt or "privacy workflow",
                reply=message[:4000],
                status="done",
                started_at=time.time(), finished_at=time.time(),
                artifact_ids=[a["id"] for a in artifact_refs],
                metrics=metrics_from_run(last),
                kind="privacy_workflow",
                label="privacy workflow" + (" (fresh)" if fresh else ""),
                config={"findings": findings_from_run(last),
                        "seed": last.get("seed"), "fresh": bool(fresh)},
                parent_run_id=parent_run_id,
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        from .audit import emit_tool_audit

        await emit_tool_audit(
            rt.audit_emitter, agent_id="Fox", session_id=rt.name,
            trace_id=prompt[:120] or None, tool_name="privacy_workflow",
            method="privacy_workflow",
            args={"fresh": fresh, "compare": compare},
            result=message[:2000], ok=True,
            duration_ms=0.0, source="coordinator")
    except Exception:  # noqa: BLE001
        pass
    return message[:60_000] if message else "(workflow produced no output)"


def goal_notices(rt: ProjectRuntime, run: dict) -> list[str]:
    """Human-readable goal-progress / new-best notices for a freshly recorded run.

    Goals scoped to an experiment only fire for that experiment's runs; an
    experiment's own goal_metric (in the experiments table) takes precedence over
    a project-wide goal on the same metric.
    """
    goals = rt.store.list_goals()
    if not goals:
        return []
    metrics = run.get("metrics") or {}
    if not metrics:
        return []
    eid = run.get("experiment_id")
    exp_metric = None
    if eid is not None:
        exp = rt.store.get_experiment(eid)
        exp_metric = (exp or {}).get("goal_metric")
    runs = rt.store.list_runs()
    notices = []
    for g in goals:
        scope = g.get("experiment_id")
        if eid is not None:
            if scope is not None and scope != eid:
                continue
            if scope is None and exp_metric and g["metric"] == exp_metric:
                continue
        else:
            if scope is not None:
                continue
        m = g["metric"]
        if m not in metrics:
            continue
        cur = metrics[m]
        higher = bool(g["higher_better"])
        better = (lambda a, b: a > b) if higher else (lambda a, b: a < b)
        prior = [(r["id"], r["metrics"][m]) for r in runs
                 if r["id"] != run["id"] and m in (r.get("metrics") or {})]
        best = min(prior, key=lambda p: (p[1] if higher else -p[1]),
                   default=None)
        label = g.get("label") or m
        parts = [f"Goal {label}: current {cur:.4g}", f"target {g['target']:.4g}"]
        if better(cur, g["target"]):
            parts.append("target reached ✓")
        elif best is not None:
            parts.append(f"{(cur - g['target']):+.3g} to go")
        if best is not None and better(cur, best[1]):
            pct = ((cur - best[1]) / best[1] * 100) if best[1] else 0.0
            parts.append(f"new best (was {best[1]:.4g} in run #{best[0]}"
                         f", {pct:+.1f}%)")
        notices.append(" · ".join(parts))
    return notices


def _autocommit_ready(run: dict) -> bool:
    """Auto-commit only for runs that belong to an experiment."""
    return bool(run.get("experiment_id"))


# ------------------------------------------------------------------ commands --

# Slash commands that map onto existing turn intents (rewritten, then flow into
# the normal handling below).
_SLASH_INTENTS = {
    "/godmode": "godmode",
    "/god": "godmode",
    "/sandbox": "godmode",
    "/improve": "improve_loop",
    "/autoresearch": "autoresearch",
}

_SLASH_PROMPTS = {
    "/godmode": "Run a thorough experiment with full access and summarize what you did.",
    "/god": "Run a thorough experiment with full access and summarize what you did.",
    "/sandbox": "Run a thorough experiment with full access and summarize what you did.",
    "/improve": "Improve the latest experiment toward its goal.",
    "/autoresearch": "accuracy",
}


def _slash_to_intent(text: str) -> str | None:
    cmd = (text or "").strip().split(maxsplit=1)[0].lower()
    return _SLASH_INTENTS.get(cmd)


def _slash_arg(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return _SLASH_PROMPTS.get(parts[0].lower(), "")
    return parts[1].strip()


HELP_TEXT = """\
**Fox slash commands** (type one in the chat box):

| Command | What it does |
|---|---|
| `/help` | Show this command list |
| `/godmode <request>` | Full access in a quarantined sandbox — run the experiment freely |
| `/improve [name]` | Run the improve loop for the latest (or named) experiment |
| `/experiments` | List experiments with status, goal and best metric |
| `/compare <a> <b>` | Compare two runs by id (metric deltas) |
| `/report [run_id]` | Generate a lab-notebook report for the last (or given) run |
| `/commit` | Commit this project's experiment artifacts to the management repo |
| `/push` | Push the management repo to GitHub |
| `/kaggle <owner/dataset>` | Import a public Kaggle dataset |
| `/notebook <name>` | Run a project notebook |
| `/status` | Show model / config / MCP status |
| `/clear` | Clear this project's conversation |
| `/autoresearch [metric]` | Run the autonomous research loop over `research/experiment.py` |

UI switches: `?flat=1` plain bubbles (default) · `?sets=1` grouped collapsible sets.
"""


async def _run_slash_command(rt: ProjectRuntime, emit, coordinator,
                             text: str) -> bool:
    """Handle a slash command. Returns True when fully handled (chat turn ends)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = (parts[1] if len(parts) > 1 else "").strip()

    async def reply(content: str, tags=None):
        amid = rt.store.add_message("assistant", content, {"tags": tags or ["command"]})
        await emit("assistant_message", {"id": amid, "content": content,
                                         "tags": tags or ["command"]})
        await emit("done", {})

    if cmd == "/help":
        await reply(HELP_TEXT, ["help"])
        return True

    if cmd == "/status":
        cfg = CONFIG
        content = [
            "**Fox status**",
            "",
            f"- Project: **{rt.name}**",
            f"- Model: **{cfg['llm'].get('model')}** (gateway {cfg['llm'].get('base_url')})",
            f"- Tool endpoint: {cfg['llm'].get('tool_base_url')}",
            f"- Reviewer: {'on' if cfg['agent'].get('reviewer_enabled') else 'off'} · "
            f"max_iters: {cfg['agent'].get('max_iters')}",
            f"- Experiment repo: {management_repo_dir() or 'not configured'}",
        ]
        try:
            statuses = await mcp_registry.statuses()
            ok = sum(1 for s in statuses if s["ok"])
            content.append(f"- MCP servers: {ok}/{len(statuses)} connected")
        except Exception:  # noqa: BLE001
            pass
        await reply("\n".join(content), ["status"])
        return True

    if cmd == "/experiments":
        exps = rt.store.list_experiments()
        if not exps:
            await reply("No experiments yet — ask Fox to plan and run one.", ["experiments"])
            return True
        lines = ["**Experiments**", ""]
        for e in exps:
            runs = rt.store.experiment_runs(e["id"], limit=1)
            best = None
            if runs and e.get("goal_metric"):
                vals = [r["metrics"].get(e["goal_metric"]) for r in
                        rt.store.experiment_runs(e["id"], limit=50)
                        if r["metrics"].get(e["goal_metric"]) is not None]
                if vals:
                    best = max(vals) if e.get("higher_better", True) else min(vals)
            goal = f"{e.get('goal_metric') or '—'} target {e.get('goal_target') or '—'}"
            best_txt = f"{best:.4g}" if best is not None else "—"
            lines.append(f"- **{e['name']}** (#{e['id']}, {e.get('status')}) — "
                         f"{goal} · best {best_txt}")
        await reply("\n".join(lines), ["experiments"])
        return True

    if cmd == "/compare":
        if not arg:
            runs = rt.store.list_runs()
            if len(runs) < 2:
                await reply("Not enough runs to compare. Run an experiment first.",
                            ["compare"])
                return True
            a, b = str(runs[-2]["id"]), str(runs[-1]["id"])
        else:
            ids = arg.split()
            if len(ids) < 2:
                await reply("Usage: /compare <run_a> <run_b> (or bare /compare for the last two).",
                            ["compare"])
                return True
            a, b = ids[0], ids[1]
        from .experiments import compare_runs

        def resolve(ref):
            return rt.store.get_run(int(ref)) if str(ref).isdigit() else None

        ra, rb = resolve(a), resolve(b)
        if ra is None or rb is None:
            await reply("Could not resolve those run ids.", ["compare"])
            return True
        c = compare_runs(ra, rb)
        lines = [f"**Compare** run {a} vs {b}", ""]
        if not c["rows"]:
            lines.append("No shared numeric metrics.")
        for row in c["rows"]:
            arrow = "▲" if row["delta"] > 0 else ("▼" if row["delta"] < 0 else "—")
            lines.append(f"- **{row['metric']}**: {row['a']:.4g} → {row['b']:.4g} "
                         f"({arrow} {row['delta']:+.4g}, {row['pct']:+.1f}%)")
        lines.append("")
        lines.append(f"{c['summary']['shared']} shared · {c['summary']['increased']} up · "
                     f"{c['summary']['decreased']} down")
        await reply("\n".join(lines), ["compare"])
        return True

    if cmd == "/report":
        from .routers.runs import build_run_report

        rid = int(arg) if arg.isdigit() else None
        run = rt.store.get_run(rid) if rid is not None else (
            rt.store.list_runs()[-1] if rt.store.list_runs() else None)
        if run is None:
            await reply("No runs to report on.", ["report"])
            return True
        report = await build_run_report(rt, run)
        await reply(report, ["report", f"run #{run['id']}"])
        return True

    if cmd in ("/commit", "/push"):
        from . import experiment_repo

        result = await experiment_repo.commit_project_async(rt) if cmd == "/commit" \
            else await asyncio.to_thread(experiment_repo.push)
        await reply((result.get("message") or "") if result.get("ok")
                    else ("Failed: " + (result.get("message") or "")),
                    ["command", "repo"])
        return True

    if cmd == "/kaggle":
        if not arg:
            await reply("Usage: /kaggle <owner/dataset> (e.g. /kaggle alexisbcook/titanic)",
                        ["kaggle"])
            return True
        from .kaggle import has_credentials, import_dataset, validate_slug

        try:
            validate_slug(arg)
        except ValueError as e:
            await reply(f"Invalid slug: {e}", ["kaggle"])
            return True
        if not has_credentials():
            await reply("Kaggle credentials are not configured (Settings).", ["kaggle"])
            return True
        await emit("status", {"message": f"Importing Kaggle dataset {arg}…"})
        result = await asyncio.to_thread(import_dataset, rt, arg)
        await reply(f"Imported **{result['dataset']}** — {len(result['files'])} file(s) "
                    f"into `{result['dir']}`.", ["kaggle"])
        return True

    if cmd == "/notebook":
        if not arg:
            await reply("Usage: /notebook <name> (e.g. /notebook 01_simple_decay_fit)",
                        ["notebook"])
            return True
        await emit("status", {"message": f"Executing notebook {arg}…"})
        result = await run_notebook_intent(rt, emit, arg, False, message_id="")
        await reply(result, ["notebook"])
        return True

    if cmd == "/clear":
        rt.store.clear_messages()
        await reply("Conversation cleared.", ["command"])
        return True

    return False


def _msg_created_at(rt: ProjectRuntime, mid: int) -> float | None:
    """Timestamp for a WS message event (so live chat can show times)."""
    row = rt.store.get_message(mid)
    return (row or {}).get("created_at")


def _resolve_experiment_id(rt: ProjectRuntime, text: str, experiment_id: str = "") -> int | None:
    """Resolve an experiment id for the improve loop.

    Uses the explicit id when given, otherwise matches the experiment name in the
    prompt, otherwise falls back to the most recently created experiment.
    """
    if str(experiment_id).isdigit():
        eid = int(experiment_id)
        if rt.store.get_experiment(eid) is not None:
            return eid
    low = (text or "").lower()
    exps = rt.store.list_experiments()
    if exps:
        for e in exps:
            if e["name"] and e["name"].lower() in low:
                return e["id"]
        return exps[-1]["id"]
    return None


# ---------------------------------------------------------- WebSocket ---------
@app.websocket("/ws/projects/{name}")
async def ws_chat(ws: WebSocket, name: str):
    origin = ws.headers.get("origin", "")
    if not origin_allowed(origin, ws.headers.get("host", "")):
        await ws.close(code=1008, reason="origin not allowed")
        return
    await ws.accept()
    rt = get_runtime(name)

    async def emit(event: str, payload: dict):
        try:
            await ws.send_json({"type": event, "payload": payload})
        except Exception:  # noqa: BLE001
            pass

    # Live workflow-progress events are pushed to this chat window; the tracker
    # keeps the latest snapshot so page/section loads can fetch it via REST.
    rt.workflow.subscribe(emit)

    broker = ApprovalBroker(emit, store=rt.store, audit=rt.audit_emitter,
                            session_id=rt.name, agent_id="Fox")

    def _record_run(r: dict) -> int:
        rid = rt.store.add_run(
            prompt=r.get("prompt", ""),
            reply=r.get("reply", ""),
            status=r.get("status", "done"),
            started_at=r.get("started_at", 0.0),
            finished_at=r.get("finished_at", time.time()),
            tool_sequence=r.get("tool_sequence"),
            artifact_ids=r.get("artifact_ids"),
            metrics=r.get("metrics"),
            review=r.get("review"),
            experiment_id=r.get("experiment_id") or None,
            config=r.get("config"),
            label=r.get("label"),
            parent_run_id=r.get("parent_run_id") or None,
            model=r.get("model") or None)
        # Auto-commit experiment artifacts to the management repo (best-effort,
        # off the event loop) when a run is part of an experiment.
        try:
            r["id"] = rid
            if _autocommit_ready(r):
                asyncio.get_running_loop().create_task(maybe_autocommit(rt, r))
        except Exception:  # noqa: BLE001
            pass
        return rid

    # Cooperative stop: the user's Stop button sets this; the coordinator checks
    # it at LLM/tool boundaries so the turn unwinds cleanly (no cancelled kernels).
    abort_event = asyncio.Event()
    coordinator = Coordinator(rt.llm, rt.ctx(emit, broker), emit=emit,
                              persist=lambda r, c, m: rt.store.add_message(r, c, m),
                              record=_record_run,
                              max_iters=rt.max_iters, mcp=mcp_registry,
                              audit=rt.audit_emitter,
                              check_abort=abort_event.is_set)

    async def handle_turn(text: str, intent: str = "", experiment_id: str = "",
                          msg_extra: dict | None = None):
        msg_extra = msg_extra or {}
        abort_event.clear()
        async with rt.lock:
            try:
                # Slash commands (e.g. "/godmode run x", "/commit", "/help").
                if text.startswith("/"):
                    slash_intent = _slash_to_intent(text)
                    if slash_intent is not None:
                        intent = slash_intent
                        text = _slash_arg(text)
                    elif await _run_slash_command(rt, emit, coordinator, text):
                        return
                user_tags = message_tags("user", text)
                # Explicit intents (from the UI quick-action buttons) route
                # deterministically instead of relying on keyword matching.
                workflow_mode = compare_mode = fresh_mode = god_mode = False
                if intent == "privacy_workflow":
                    workflow_mode = True
                    user_tags = ["privacy workflow"]
                elif intent == "privacy_workflow_fresh":
                    workflow_mode = fresh_mode = True
                    user_tags = ["privacy workflow", "fresh rerun"]
                elif intent == "privacy_compare":
                    workflow_mode = compare_mode = True
                    user_tags = ["privacy workflow", "compare runs"]
                elif intent == "godmode":
                    # God mode: full access (auto-approved) confined to a
                    # quarantined per-turn sandbox folder.
                    god_mode = True
                    user_tags = ["god mode"]
                elif intent == "autoresearch":
                    # Autonomous research loop (karpathy/autoresearch style).
                    user_tags = ["autoresearch"]
                elif intent == "improve_loop":
                    # B2: reviewer-driven improve loop — bounded iterations of
                    # run → review → apply best suggestion → rerun toward the
                    # experiment's goal. Run server-side, streaming to chat.
                    user_tags = ["improve loop"]
                elif intent == "rerun_suggestion":
                    # "Apply & rerun" from a reviewer suggestion: send the
                    # suggestion's prompt to the agent as a fresh turn.
                    user_tags = ["rerun suggestion"]
                else:
                    rcc = rerun_compare_requested(text)
                    workflow_mode = bool(match_workflow(text) or
                                         compare_requested(text) or rcc)
                    compare_mode = compare_requested(text) or rcc
                    fresh_mode = fresh_requested(text) or rcc
                    if rcc:
                        user_tags = ["privacy workflow", "fresh rerun", "compare runs"]
                if intent == "autoresearch":
                    from .autoresearch import run_autoresearch_loop

                    await emit("status", {"message": "Preparing autonomous research loop…"})
                    cfg = dict(msg_extra.get("autoresearch") or {})
                    if text and not cfg.get("goal_metric"):
                        cfg.setdefault("goal_metric", text.strip() or "accuracy")
                    result = await run_autoresearch_loop(
                        rt, coordinator, rt.build_llm_messages, cfg,
                        emit=emit, workflow=rt.workflow)
                    await emit("status", {"message": ""})
                    await emit("done", {})
                    return
                if intent == "improve_loop":
                    await emit("status", {"message": "Preparing improve loop…"})
                    eid = _resolve_experiment_id(rt, text, experiment_id)
                    if eid is None:
                        await emit("error", {"message":
                            "No experiment found. Create one first (chat or the Experiments tab)."})
                        await emit("done", {})
                        return
                    result = await run_improve_loop(
                        rt.store, coordinator, rt.build_llm_messages,
                        lambda: Reviewer(rt.llm, rt.store).review(),
                        eid, text, emit=emit, workflow=rt.workflow)
                    await emit("status", {"message": ""})
                    await emit("done", {})
                    return
                mid = rt.store.add_message("user", text, {"tags": user_tags})
                coordinator.ctx.message_id = str(mid)
                broker.trace_id = str(mid)
                await emit("user_message", {"id": mid, "content": text,
                                            "tags": user_tags,
                                            "created_at": _msg_created_at(rt, mid)})
                if workflow_mode:
                    if compare_mode and fresh_mode:
                        # "rerun with new seed and compare with last run": the
                        # workflow script can't do both in one call (--compare
                        # skips the execution), so run the fresh execution first
                        # (records a new run), then compare against the last one.
                        await emit("status", {"message":
                            "Rerunning the privacy workflow with a fresh seed…"})
                        fresh_result = await run_privacy_workflow(
                            rt, emit, fresh=True, compare=False, prompt=text)
                        await emit("status", {"message":
                            "Comparing with the last run…"})
                        cmp_result = await run_privacy_workflow(
                            rt, emit, fresh=False, compare=True, prompt=text)
                        result = (fresh_result + "\n\n---\n\n" + cmp_result)[:100_000]
                    elif compare_mode:
                        await emit("status", {"message":
                            "Comparing previous workflow runs…"})
                        result = await run_privacy_workflow(
                            rt, emit, fresh=False, compare=True, prompt=text)
                    else:
                        await emit("status", {"message":
                            "Running the privacy workflow — peer exploitation · "
                            "red team · DP robustness…"})
                        result = await run_privacy_workflow(
                            rt, emit, fresh=fresh_mode, compare=False, prompt=text)
                    if not (result or "").strip():
                        result = ("Privacy workflow produced no output — check "
                                  "the server log for errors.")
                    amid = rt.store.add_message(
                        "assistant", result,
                        {"tags": message_tags("assistant", result)})
                    await emit("assistant_message", {"id": amid, "content": result,
                                                     "tags": message_tags("assistant", result),
                                                     "created_at": _msg_created_at(rt, amid)})
                    await emit("done", {})
                    return
                nb = match_notebook_run(text)
                if nb:
                    name, fresh = nb
                    await emit("status", {"message": f"Executing notebook {name}"
                                        + (" with a fresh seed…" if fresh else "…")})
                    result = await run_notebook_intent(rt, emit, name, fresh,
                                                        message_id=str(mid))
                    tags = ["notebook", "fresh rerun" if fresh else "run"]
                    amid = rt.store.add_message("assistant", result, {"tags": tags})
                    await emit("assistant_message", {"id": amid, "content": result,
                                                     "tags": tags,
                                                     "created_at": _msg_created_at(rt, amid)})
                    if has_analysis_intent(text):
                        # The rerun result is already shown; also hand the deep
                        # parameterised analysis (seeds / DP) to the agent.
                        rt.store.add_message(
                            "tool", result,
                            {"name": "run_notebook", "tool_call_id": "notebook"})
                    else:
                        await emit("done", {})
                        return
                await emit("status", {"message": "Agent is thinking…"})
                await rt.maybe_compact()
                llm_msgs = rt.build_llm_messages()
                # Branching lineage: a turn that applies a reviewer suggestion
                # ("Apply & rerun") derives from the run it improves; anything
                # after a "fresh rerun"/autoresearch rerun derives from the last
                # run of the same kind. Used by the branch-history graph.
                try:
                    if intent == "rerun_suggestion":
                        last = rt.store.list_runs()
                        if last:
                            coordinator.ctx.parent_run_id = last[-1]["id"]
                    elif intent == "autoresearch" or (text and "autoresearch" in text):
                        pass  # handled by the autoresearch loop itself
                except Exception:  # noqa: BLE001
                    pass
                # God mode: auto-approve everything and confine shell work to a
                # quarantined per-turn folder. Restore normal policy afterwards.
                saved_perms = coordinator.ctx.permissions
                saved_quar = coordinator.ctx.quarantine_dir
                god_dir = None
                try:
                    if god_mode:
                        god_dir = rt.dir / "godmode" / str(int(time.time()))
                        god_dir.mkdir(parents=True, exist_ok=True)
                        coordinator.ctx.permissions = AllowAllPermissionManager()
                        coordinator.ctx.quarantine_dir = str(god_dir)
                        llm_msgs.insert(0, {
                            "role": "system",
                            "content": (GODMODE_SYSTEM.format(dir=str(god_dir))),
                        })
                        await emit("status", {"message":
                            f"⚡ GOD MODE — full access in quarantined sandbox {god_dir}…"})
                    result = await coordinator.run_turn(llm_msgs)
                finally:
                    coordinator.ctx.permissions = saved_perms
                    coordinator.ctx.quarantine_dir = saved_quar
                if god_mode:
                    await emit("notice", {"message":
                        f"⚡ God-mode run finished — sandbox: {god_dir}"})
                tools = (result or {}).get("tools") or []
                model = (result or {}).get("model") or ""
                mcp, action = tool_turn_label(tools)
                atags = message_tags("assistant", result.get("text", ""))
                atags = list(dict.fromkeys(atags + tool_turn_tags(tools)))
                amid = rt.store.add_message(
                    "assistant", result.get("text", ""),
                    {"tags": atags, "mcp_name": mcp, "action": action,
                     "tools": tools, "model": model})
                await emit("assistant_message", {"id": amid,
                                                 "content": result.get("text", ""),
                                                 "tags": atags,
                                                 "mcp_name": mcp, "action": action,
                                                 "tools": tools, "model": model,
                                                 "created_at": _msg_created_at(rt, amid)})
                # Goal progress vs. the best-known run (improvement tracking).
                runs_now = rt.store.list_runs()
                if runs_now:
                    for notice in goal_notices(rt, runs_now[-1]):
                        await emit("notice", {"message": notice})
                if rt.reviewer_enabled:
                    await emit("status", {"message": "Reviewing the turn…"})
                    await emit("review_start", {})
                    try:
                        review = await Reviewer(rt.llm, rt.store).review()
                        if runs_now:
                            rt.store.update_run_review(runs_now[-1]["id"], review)
                        await emit("review", review)
                    except Exception:  # noqa: BLE001
                        await emit("review", {"findings": [], "suggestions": []})
                await emit("status", {"message": ""})
                # Background deviation scan: flag novel tools, sequences,
                # data classes and network destinations after the turn.
                try:
                    rt.audit_scanner.scan()
                except Exception:  # noqa: BLE001
                    pass
                await emit("done", {})
            except TurnAborted:
                await emit("status", {"message": ""})
                await emit("notice",
                           {"message": "Turn stopped by user — progress so far was saved."})
                await emit("done", {})
            except LLMError as e:
                await emit("status", {"message": ""})
                await emit("error", {"message": str(e)})
            except Exception as e:  # noqa: BLE001
                await emit("status", {"message": ""})
                await emit("error", {"message": f"{type(e).__name__}: {e}"})

    incoming: asyncio.Queue = asyncio.Queue()

    async def receive_loop():
        # Single reader on the socket: approvals resolve the broker directly (so
        # the coordinator can block on them mid-turn), chat goes to the queue.
        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "approval":
                    broker.resolve(msg.get("request_id", ""), bool(msg.get("decision")),
                                   bool(msg.get("temporary", False)))
                elif mtype == "ping":
                    await emit("pong", {})
                elif mtype == "stop":
                    abort_event.set()
                else:
                    await incoming.put(msg)
        except WebSocketDisconnect:
            broker.reject_all()  # resolve pending approvals so the agent can't hang
            pass

    recv_task = asyncio.create_task(receive_loop())
    try:
        while True:
            msg = await incoming.get()
            if msg.get("type") == "chat":
                text = (msg.get("content") or "").strip()
                if text:
                    await handle_turn(text, intent=msg.get("intent") or "",
                                      experiment_id=msg.get("experiment_id") or "",
                                      msg_extra=msg)
    except WebSocketDisconnect:
        pass
    finally:
        recv_task.cancel()
        broker.reject_all()  # don't let the agent hang on a vanished client
        rt.workflow.unsubscribe(emit)
# ------------------------------------------------------------ static files ---

class NoCacheStaticFiles(StaticFiles):
    """Serve frontend assets with no-store headers so UI changes always apply
    immediately (defeats stale browser caches, incl. the HTML entrypoint)."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
