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
from pathlib import Path
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
from .routers import artifacts, finetune, notebooks, projects, runs, system
from .state import (CONFIG, allowed_origins, get_runtime, mcp_registry,
                    origin_allowed, runtimes)


# ------------------------------------------------------------------ app -----

_rkg_scheduler: "ScenarioScheduler | None" = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # Recover plans a previous process left RUNNING (killed by a restart).
    try:
        from .experiment_planner import PlanStore
        for sub in PROJECTS_DIR.iterdir():
            if sub.is_dir():
                PlanStore(sub).recover_interrupted()
    except Exception:  # noqa: BLE001
        pass
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
    # Close MCP server subprocesses so stdio children don't leak on restart.
    try:
        from .state import mcp_registry as _mcp_registry
        await _mcp_registry.close()
    except Exception:  # noqa: BLE001
        pass


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
app.include_router(finetune.router)
app.include_router(runs.router)
app.include_router(artifacts.router)
app.include_router(notebooks.router)
from .routers.dataset import router as dataset_router

app.include_router(dataset_router)
from .routers.planner import router as planner_router

app.include_router(planner_router)
from .routers.eda import router as eda_router

app.include_router(eda_router)
from .routers.peer import router as peer_router

app.include_router(peer_router)
from .routers.experiment_planner import router as experiment_planner_router

app.include_router(experiment_planner_router)
import backend.experiment_registry  # noqa: F401  (registers deterministic experiments)
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


_PEER_EXPERIMENT_WORDS = [
    "peer identification", "peer-identification", "identify.*bank", "market share",
    "market-share", "estimating other", "other's share", "bank.*share",
    "share per segment", "share per payment", "peer.*experiment",
    "which bank", "sender_bank",
]


def _render_plan_md(payload: dict) -> str:
    """Markdown for a proposed experiment plan (chat plan card body)."""
    lines = [
        f"- **Experiment:** {payload.get('name')} (`{payload.get('experiment_id')}`)",
        f"- **Dataset:** `{payload.get('dataset') or '—'}` · seed `{payload.get('seed')}`",
        f"- **Plan id:** `{payload.get('plan_id')}`",
        "",
        "**Steps**",
    ]
    for i, s in enumerate(payload.get("steps") or [], 1):
        lines.append(f"{i}. {s}")
    lines += ["", "Nothing has run yet — **confirm** to execute, or **reject** to cancel."]
    return "\n".join(lines)


_EXPERIMENT_PLAN_WORDS = [
    "experiment plan", "plan an experiment", "plan the experiment",
    "propose a plan", "propose plan", "plan before", "plan first",
    "confirm before", "ask before running", "plan and confirm",
    "experiment planner", "orchestrator", "peer experiment",
    "plan for the experiment", "make a plan",
]


def _is_experiment_plan_request(text: str) -> bool:
    """Detect requests to PLAN an experiment before running it (deterministic
    planner flow: propose -> confirm -> execute)."""
    low = (text or "").lower()
    return any(w in low for w in _EXPERIMENT_PLAN_WORDS)


_CHART_NOUNS = ("chart", "plot", "graph", "distribution", "histogram",
                "scatter", "correlation", "trend", "bar chart", "line chart",
                "pie chart", "barplot", "countplot", "visualize", "visualise")
_CHART_PREPS = (" of ", " between ", " vs ", " by ", " over ", "against")


def _is_chart_request(text: str) -> bool:
    """Detect a natural-language chart request (e.g. "make a distribution of
    transaction type") so it renders deterministically via the Flint charts MCP
    instead of going through the (tool-light) LLM loop."""
    low = (text or "").lower()
    if not any(n in low for n in _CHART_NOUNS):
        return False
    if any(p in low for p in _CHART_PREPS):
        return True
    return bool(re.search(r"(make|draw|show|generate|plot|chart|visuali[sz]e)\b",
                          low))


def _is_upi_generate_request(text: str) -> bool:
    """Detect a request to GENERATE a synthetic UPI dataset (e.g. "generate a
    synthetic UPI transaction dataset of 100k rows") so it runs deterministically
    via the adapted notebook generator."""
    low = (text or "").lower()
    if "@upi" in low:
        return True
    if not ("upi" in low or ("transaction" in low and "dataset" in low)):
        return False
    if "dataset" not in low:
        return False
    return any(w in low for w in ("generate", "create", "produce", "synthesi",
                                  "make a", "new upi", "synthetic upi"))


async def _handle_upi_generate(rt, emit, text: str,
                               msg_extra: dict | None = None) -> None:
    """Deterministically generate a synthetic UPI dataset into the project using
    the adapted notebook generator, and post a summary + preview."""
    import re as _re
    from .upi_generator import generate_upi_csv
    from .experiment_planner import is_dataset_file
    low = (text or "").lower()
    n = 50000
    m = _re.search(r"(\d+(?:[.,]\d+)?)\s*(k|thousand)?\s*(rows|records|transactions)?",
                   low)
    if m:
        num = float(m.group(1).replace(",", ""))
        if m.group(2):
            num *= 1000
        n = int(min(max(num, 100), 500000))
    name = "synthetic_upi_transactions.csv"
    try:
        path = generate_upi_csv(rt.dir / name, n_records=n)
    except Exception as e:  # noqa: BLE001
        await emit("error", {"message": f"Could not generate dataset: {e}"})
        await emit("done", {})
        return
    try:
        import pandas as pd
        head = pd.read_csv(path, nrows=5)
        cols = ", ".join(str(c) for c in head.columns)
    except Exception:  # noqa: BLE001
        cols = ""
    content = (f"✅ Generated synthetic UPI transaction dataset — **{n:,} rows** · "
               f"17 columns → `{name}`\n\n"
               f"**Columns:** {cols}\n\n"
               f"Use it with `@eda {name}`, `@chart`, `@mcp`, or ask for a plan / "
               "privacy suite — the experiments will run on this dataset.")
    amid = rt.store.add_message("assistant", content, {"tags": ["dataset", "synthetic", "upi"]})
    await emit("assistant_message", {"id": amid, "content": content,
                                     "tags": ["dataset", "synthetic", "upi"],
                                     "created_at": _msg_created_at(rt, amid)})
    await emit("done", {})


# Deterministic experiment aliases: common phrasings → planner experiment id.
_EXPERIMENT_ALIASES = {
    "reid_risk": ["reidentif", "re-identif", "k-anonymity", "quasi-identifier",
                  "quasi identifier", "deanonym", "de-anonym", "uniqueness",
                  "identifiability", "linkage attack", "population reidentification"],
    "pii_scan": ["pii", "privacy scan", "identifier scan", "personally identifiable"],
    "dp_privacy": ["differential privacy", "differential-privacy", "dp privacy",
                   "laplace mechanism", "privacy-utility", "epsilon"],
    "anomaly": ["outlier", "anomaly", "anomalies"],
    "clean": ["cleaning", "clean the data", "dedupe", "missing values"],
    "correlation": ["correlation"],
    "eda": ["eda", "dataset overview", "profile the data"],
    "peer": ["peer", "market share", "market-share", "which bank", "sender_bank",
             "identif.*bank"],
}


def _experiment_from_text(text: str) -> str | None:
    """Resolve a planner experiment id from free text via aliases (or None)."""
    low = (text or "").lower()
    for eid, aliases in _EXPERIMENT_ALIASES.items():
        for a in aliases:
            if a in low:
                return eid
    return None


def _is_privacy_experiment_request(text: str) -> bool:
    """Detect re-identification / privacy-exploit requests so they run through
    the deterministic planner (e.g. reid_risk, pii_scan, dp_privacy) instead of
    the tool-light LLM loop that tends to describe work without doing it."""
    low = (text or "").lower()
    if not any(w in low for w in ("privacy", "identif", "anonym", "pii",
                                  "exploit", "k-anonymity", "quasi")):
        return False
    if _experiment_from_text(text):
        return True
    return ("bank" in low or "financial" in low) and "privacy" in low


def _is_privacy_suite_request(text: str) -> bool:
    """Detect a request to run the WHOLE privacy exploit suite on the project's
    datasets (e.g. "run all privacy exploits on these datasets and prepare a
    detailed report") so it runs deterministically (all experiments + a report)
    instead of through the LLM loop."""
    low = (text or "").lower()
    if not any(w in low for w in ("privacy", "re-ident", "de-anonym", "pii")):
        return False
    if not any(w in low for w in ("exploit", "attack", "suite", "scenario",
                                  "adversarial")):
        return False
    return any(w in low for w in ("all", "every", "full", "complete",
                                  "comprehensive", "both datasets",
                                  "these datasets", "datasets"))


# ------------------------------------------------------------ loop guard -----
# Local models sometimes "loop": instead of finishing, they keep producing a
# near-identical reply (often re-planning) turn after turn. We detect that and
# break the loop with clear guidance rather than burning more turns.

def _text_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _recent_assistant_messages(rt, n: int = 3) -> list[tuple[str, float]]:
    """Most recent non-empty assistant message (content, created_at)."""
    rows = rt.store.list_messages(limit=120)
    out = []
    for r in reversed(rows):
        if r.get("role") == "assistant" and (r.get("content") or "").strip():
            out.append((r.get("content"), r.get("created_at") or 0))
        if len(out) >= n:
            break
    return out


def _agent_looping(rt) -> bool:
    """True when the last two assistant replies are near-duplicates (the model
    is re-planning / repeating instead of making progress)."""
    msgs = _recent_assistant_messages(rt, 3)
    for i in range(1, len(msgs)):
        a, _ = msgs[i - 1]
        b, _ = msgs[i]
        if _text_similarity(a, b) >= 0.85:
            return True
    return False


def _is_continue_request(text: str) -> bool:
    """The one-click Continue button (and equivalents) sends a message that
    means "pick up where the last turn stopped"."""
    low = (text or "").lower()
    return any(p in low for p in (
        "continue from where you left off", "keep going", "go on",
        "don't stop early", "finish the task", "pick up where"))


def _is_peer_experiment_request(text: str) -> bool:
    """Detect a peer-identification / market-share experiment request so it runs
    deterministically instead of through the (often tool-less) LLM loop."""
    low = (text or "").lower()
    if not any(w in low for w in ("bank", "peer", "upi")):
        return False
    import re as _re
    hits = 0
    for w in _PEER_EXPERIMENT_WORDS:
        if _re.search(w, low):
            hits += 1
    return hits >= 2


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


def _project_tabular_file(rt) -> str | None:
    """The first tabular data file (CSV/Parquet/Excel/JSON) attached to the
    project — at its root or in data/. Used to run the privacy workflow on the
    user's actual source instead of synthetic data."""
    try:
        suffixes = (".csv", ".tsv", ".txt", ".parquet", ".xlsx", ".xls",
                    ".json", ".jsonl")
        for sub in (rt.dir, rt.dir / "data"):
            if not sub.is_dir():
                continue
            for p in sorted(sub.iterdir()):
                if p.is_file() and p.suffix.lower() in suffixes:
                    return str(p)
    except Exception:  # noqa: BLE001
        pass
    return None


async def run_privacy_workflow(rt: ProjectRuntime, emit,
                               fresh: bool = False, compare: bool = False,
                               prompt: str = "") -> str:
    """Run (or compare) the privacy workflow and register outputs as artifacts.

    If the project has an attached tabular data file (CSV/Parquet/Excel/JSON),
    it is passed to the workflow via ``--data`` so the peer-exploitation /
    red-team / DP stages run on the user's actual source instead of the bundled
    synthetic credit-card generator.
    """
    script = PRIVACY_WORKFLOW["script"]
    base_report_dir = ROOT / PRIVACY_WORKFLOW["report_dir"]
    runs_file = PROJECTS_DIR.parent / "privacy_runs.json"   # persistent volume
    out_dir = base_report_dir / "compare" if compare else base_report_dir
    args = [script, "--runs-file", str(runs_file), "--out-dir", str(out_dir)]
    if fresh:
        args.append("--fresh")
    if compare:
        args.append("--compare")
    else:
        # Attach the project's data file (first tabular file at the project
        # root or data/) so the workflow runs on the real source — or, if none
        # is usable, so the privacy MCP's synthetic generator can build a
        # population from the real schema instead of the bundled generator.
        data_file = _project_tabular_file(rt)
        if data_file:
            args += ["--data", str(data_file)]
            args += ["--real-data", str(data_file)]
    wf = getattr(rt, "workflow", None)
    if wf is not None and not compare:
        from .workflows import PRIVACY_STAGES

        await wf.start(title="Privacy workflow", stages=PRIVACY_STAGES)

    async def _run_to_completion():
        """Run the workflow subprocess + register artifacts + build the summary.

        Everything from subprocess creation to message posting lives in this
        coroutine so a client disconnect (WS handler cancellation) cannot kill
        it: the workflow keeps running and posts its report even after the tab
        closes, like background campaigns.
        """
        import logging as _log
        proc = await asyncio.create_subprocess_exec(
            sys.executable, *args, cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
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
            data_src = "synthetic credit-card data (obfuscation-study generator)"
            if data_file:
                data_src = (f"the project's attached dataset "
                            f"(`{Path(data_file).name}`)")
            lines = [
                "## Privacy workflow — peer exploitation · red team · DP robustness",
                "",
                f"The workflow ran **3 stages** on {data_src} and produced the "
                "reports below, which are also saved as artifacts.",
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

        # Post the result even if the original caller disconnected: persist it
        # and broadcast to any connected window (like background campaigns).
        try:
            amid = rt.store.add_message(
                "assistant", message,
                {"tags": message_tags("assistant", message)})
            if emit:
                await emit("assistant_message", {
                    "id": amid, "content": message,
                    "tags": message_tags("assistant", message),
                    "created_at": time.time()})
                await emit("done", {})
        except Exception:  # noqa: BLE001
            pass
        return message[:60_000] if message else "(workflow produced no output)"

    # Launch as an independent task so a client disconnect (WS handler
    # cancellation) can never cancel the workflow mid-flight — it keeps running
    # and posts its report/message even if the tab closed. Fire-and-forget: the
    # caller returns immediately and the task runs to completion on its own.
    import logging as _log
    _log.getLogger("uvicorn.error").info("[privacy-workflow] launching task")
    task = asyncio.create_task(_run_to_completion())
    def _log_done(t):
        try:
            exc = t.exception()
            if exc:
                _log.getLogger("uvicorn.error").error(
                    "[privacy-workflow] task failed: %r", exc, exc_info=exc)
            else:
                _log.getLogger("uvicorn.error").info(
                    "[privacy-workflow] task completed")
        except asyncio.CancelledError:
            _log.getLogger("uvicorn.error").warning("[privacy-workflow] task CANCELLED")
        except Exception:  # noqa: BLE001
            pass
    task.add_done_callback(_log_done)
    try:
        rt._privacy_workflow_task = task
    except Exception:  # noqa: BLE001
        pass
    # Do NOT await the task here: awaiting (even via shield) lets a caller
    # cancellation propagate. Return immediately; the task posts its own result.
    return "(privacy workflow running in the background)"


def _attach_suggestion_ids(store, source_run_id: int, review: dict) -> None:
    """Persist a review's suggestions as first-class records and attach their
    ids to the dict (so the WS review payload / UI can reference and track them)."""
    ids = store.add_suggestions(
        _review_experiment_id(store, source_run_id), source_run_id, review)
    for s, sid in zip((review or {}).get("suggestions") or [], ids):
        if isinstance(s, dict):
            s["id"] = sid


def _review_experiment_id(store, run_id: int) -> int | None:
    run = store.get_run(run_id)
    return (run or {}).get("experiment_id")


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
    "/campaign": "campaign",
}

_SLASH_PROMPTS = {
    "/godmode": "Run a thorough experiment with full access and summarize what you did.",
    "/god": "Run a thorough experiment with full access and summarize what you did.",
    "/sandbox": "Run a thorough experiment with full access and summarize what you did.",
    "/improve": "Improve the latest experiment toward its goal.",
    "/autoresearch": "accuracy",
    "/campaign": "Plan and run a research campaign to investigate this question.",
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
| `/focus <name|id>` | Steer the agent toward one experiment's objective (or `/focus off`) |
| `/godmode <request>` | Full access in a quarantined sandbox — run the experiment freely |
| `/improve [name]` | Run the improve loop for the latest (or named) experiment |
| `/experiments` | List experiments with status, goal and best metric |
| `/complete` / `/cancel` / `/activate <name|id>` | Change an experiment's lifecycle status (complete also publishes its aggregate report) |
| `/chart <experiment|run_id> [metric]` | Render a Flint chart of a run's metrics or an experiment's goal-metric evolution || `/compare <a> <b>` | Compare two runs by id (metric deltas) |
| `/report [run_id]` | Generate a lab-notebook report for the last (or given) run |
| `/commit` | Commit this project's experiment artifacts to the management repo |
| `/push` | Push the management repo to GitHub |
| `/kaggle <owner/dataset>` | Import a public Kaggle dataset |
| `/notebook <name>` | Run a project notebook |
| `/status` | Show model / config / MCP status |
| `/clear` | Clear this project's conversation |
| `/autoresearch [metric]` | Run the autonomous research loop over `research/experiment.py` |
| `/campaign [question]` | Plan and run a multi-step research campaign (synthesis report) |

UI switches: `?flat=1` plain bubbles (default) · `?sets=1` grouped collapsible sets.
"""


async def _mcp_tool_listing(limit: int = 120) -> str:
    """A discoverable ``server__tool`` listing for the @mcp command."""
    try:
        statuses = await mcp_registry.statuses()
    except Exception:  # noqa: BLE001
        return "MCP registry unavailable."
    lines = []
    for s in statuses:
        if not s.get("ok"):
            lines.append(f"🔌 {s['name']} — {s.get('error') or 'offline'}")
            continue
        for t in s.get("tool_catalog") or []:
            required = [p["name"] for p in (t.get("params") or []) if p.get("required")]
            sig = f"({' '.join(required)})" if required else ""
            lines.append(f"  {s['name']}__{t['name']}{sig}"
                         f"{' (read-only)' if t.get('read_only') else ''}"
                         f" — {(t.get('description') or '')[:90]}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "No MCP servers connected."


async def _handle_privacy_suite(rt, emit, text: str,
                                msg_extra: dict | None = None) -> None:
    """Deterministic privacy exploit suite: propose a suite plan for approval,
    then run all privacy experiments across the dataset(s) in the background
    (with a live run-log bubble) and post the aggregated report + figures."""
    import asyncio as _asyncio
    from .privacy_suite import run_privacy_suite, SUITE_EXPERIMENTS
    msg_extra = msg_extra or {}

    # Resolve the dataset(s): explicit list, a filename named in the request, or
    # the generated synthetic dataset when asked for "the generated dataset".
    datasets = [d.strip() for d in (msg_extra.get("datasets") or "").split(",")
                if d.strip()]
    low = (text or "").lower()
    if not datasets:
        for f in rt.dir.iterdir():
            if (f.is_file() and f.suffix.lower() in (".csv", ".parquet", ".xlsx")
                    and f.name.lower() in low):
                datasets = [f.name]
                break
    if not datasets and ("generated" in low or "synthetic" in low):
        cand = rt.dir / "synthetic_upi_transactions.csv"
        if cand.exists():
            datasets = ["synthetic_upi_transactions.csv"]
    ds_label = ", ".join(datasets) if datasets else "all project datasets"

    suite_id = f"suite-{int(time.time())}"
    title = "Privacy exploit suite"
    payload = {
        "plan_id": suite_id,
        "experiment_id": "privacy_suite",
        "name": title,
        "description": ("Run all privacy experiments (PII scan, re-identification, "
                        "differential privacy, anomaly, correlation, bank peer "
                        "identification) across the dataset(s) and prepare a "
                        "detailed aggregate report."),
        "request": text,
        "dataset": ds_label,
        "seed": 42,
        "steps": [f"Run `{e}` on `{ds_label}`" for e in SUITE_EXPERIMENTS]
                 + ["Aggregate the per-dataset metrics into a report"],
        "expected_outputs": ["cross-dataset goal-metric table",
                             "detailed markdown report",
                             "figures saved as artifacts"],
        "status": "WAITING_APPROVAL",
        "created_at": time.time(),
    }
    # Propose for approval (same plan card + popup as single experiments).
    await emit("experiment_plan_proposal", payload)
    amid = rt.store.add_message(
        "assistant",
        "**🧪 Privacy suite proposed — confirm to execute**\n\n"
        + _render_plan_md(payload),
        {"tags": ["privacy", "suite", "plan", "proposal"]})
    await emit("assistant_message", {"id": amid,
                                     "content": "**🧪 Privacy suite proposed — confirm to execute**\n\n" + _render_plan_md(payload),
                                     "tags": ["privacy", "suite", "plan", "proposal"],
                                     "created_at": _msg_created_at(rt, amid)})
    await emit("status", {"message":
        "⏸ Awaiting your approval to run the privacy suite…"})
    try:
        if not hasattr(rt, "_plan_approvals"):
            rt._plan_approvals = {}
    except Exception:  # noqa: BLE001
        pass
    fut = _asyncio.get_event_loop().create_future()
    try:
        rt._plan_approvals[suite_id] = fut
    except Exception:  # noqa: BLE001
        pass
    ok = False
    try:
        ok = await _asyncio.wait_for(fut, timeout=300)
    except _asyncio.TimeoutError:
        ok = False
    try:
        rt._plan_approvals.pop(suite_id, None)
    except Exception:  # noqa: BLE001
        pass
    await emit("status", {"message": ""})
    if not ok:
        await emit("notice", {"message": "Privacy suite rejected — nothing ran."})
        await emit("done", {})
        return

    await emit("workflow", {"status": "running", "title": title,
                            "message": "starting", "pct": 0})

    async def _progress(done, total, message):
        try:
            await emit("workflow", {
                "status": "running", "title": title,
                "message": f"{done}/{total} {message}",
                "pct": round(done / max(total, 1) * 100)})
        except Exception:  # noqa: BLE001
            pass

    async def _run():
        try:
            out = await run_privacy_suite(
                rt, datasets=datasets or None,
                progress=_progress)
            await emit("workflow", {"status": "done", "title": title,
                                    "message": "completed", "pct": 100})
            fig_html = "".join(
                f"![fig](/artifacts/{fid})" for fid in out["figure_ids"])
            content = (f"**🔐 Privacy exploit suite — report**\n\n"
                       f"- Datasets: {', '.join(out['datasets']) or '—'}\n"
                       f"- [Report artifact](/artifacts/{out['report_id']})\n\n"
                       f"{fig_html}\n\n{out['report']}")
            amid = rt.store.add_message(
                "assistant", content,
                {"tags": ["privacy", "suite", "report"]})
            await emit("assistant_message", {"id": amid, "content": content,
                                             "tags": ["privacy", "suite", "report"],
                                             "created_at": _msg_created_at(rt, amid)})
            await emit("notice", {"message":
                f"✅ Privacy suite done — {len(out['datasets'])} dataset(s), "
                "report saved as an artifact."})
        except Exception as e:  # noqa: BLE001
            try:
                await emit("workflow", {"status": "failed", "title": title,
                                        "message": str(e), "pct": 100})
                await emit("error", {"message":
                    f"Privacy suite failed: {type(e).__name__}: {e}"})
            except Exception:  # noqa: BLE001
                pass
        await emit("done", {})

    _asyncio.get_running_loop().create_task(_run())
    await emit("done", {})
    return


async def _handle_chart_request(rt, emit, text: str,
                                msg_extra: dict | None = None) -> None:
    """Deterministic natural-language chart intent: load the project dataset,
    build a Flint chart spec from the request, render it (flint MCP, with a
    matplotlib fallback), register the PNG as an artifact and post it inline."""
    import asyncio as _asyncio
    from .flint_charts import chart_spec_from_request, render_chart_artifact
    from .experiment_planner import load_dataset, is_dataset_file
    from .state import mcp_registry
    msg_extra = msg_extra or {}

    dataset = (msg_extra.get("dataset") or "").strip()
    cands = []
    if dataset and (rt.dir / dataset).exists():
        cands = [rt.dir / dataset]
    else:
        cands = sorted(p for p in rt.dir.iterdir()
                       if p.is_file() and is_dataset_file(p.name)
                       and not p.name.lower().startswith("synthetic_"))
    if not cands:
        await emit("notice", {"message":
            "No dataset in the project — upload a CSV/Parquet/Excel file first."})
        await emit("done", {})
        return
    try:
        df = await _asyncio.to_thread(load_dataset, cands[0])
    except Exception as e:  # noqa: BLE001
        await emit("error", {"message": f"Could not load dataset: {e}"})
        await emit("done", {})
        return
    spec = chart_spec_from_request(text, df)
    if not spec:
        cols = ", ".join(str(c) for c in df.columns[:24])
        content = ("I can chart your dataset. Try:\n\n"
                   "- `make a distribution of <column>`\n"
                   "- `histogram of <numeric column>`\n"
                   "- `scatter <a> vs <b>`\n"
                   "- `trend of <metric> over <x>`\n\n"
                   f"**Columns:** {cols}")
        amid = rt.store.add_message("assistant", content, {"tags": ["chart", "help"]})
        await emit("assistant_message", {"id": amid, "content": content,
                                         "tags": ["chart", "help"],
                                         "created_at": _msg_created_at(rt, amid)})
        await emit("done", {})
        return
    fname = f"chart-{int(time.time())}.png"
    aid = await render_chart_artifact(rt, mcp_registry, spec, fname)
    if not aid:
        await emit("error", {"message":
            "Chart rendering failed — the flint server is unavailable and the "
            "matplotlib fallback errored."})
        await emit("done", {})
        return
    content = (f"📊 **{spec.get('title', 'Chart')}** — dataset "
               f"`{cands[0].name}`\n\n![chart](/artifacts/{aid})")
    amid = rt.store.add_message("assistant", content, {"tags": ["chart", "figure"]})
    await emit("assistant_message", {"id": amid, "content": content,
                                     "tags": ["chart", "figure"],
                                     "created_at": _msg_created_at(rt, amid)})
    try:
        art = rt.artifacts.get(aid)
        await emit("artifact", {"artifact": art.to_dict()})
    except Exception:  # noqa: BLE001
        pass
    await emit("done", {})


async def _handle_mcp_command(rt, emit, broker, text: str) -> None:
    """Deterministic ``@mcp <server>__<tool> [json args]`` invocation from chat.

    ``@mcp bg <server>__<tool> [json args]`` starts the call as a background
    run and returns immediately (result lands as a run / notice). Otherwise the
    call runs synchronously. Routes through the same permission model as the
    agent's MCP calls (read-only tools run freely; writable tools go to the
    ApprovalBroker), so results land in chat without an LLM round-trip. Emits
    its own ``done`` (the caller returns early).
    """
    import asyncio as _asyncio
    body = text[len("@mcp "):].strip()
    bg = False
    if body.startswith("bg "):
        bg = True
        body = body[3:].strip()
    full, _, rest = body.partition(" ")
    if "__" not in full:
        listing = await _mcp_tool_listing()
        content = ("**MCP tools** — `@mcp <server>__<tool> [json args]`"
                   " (`@mcp bg …` runs in the background)\n\n"
                   f"```\n{listing[:4000]}\n```")
        amid = rt.store.add_message("assistant", content, {"tags": ["mcp", "help"]})
        await emit("assistant_message", {"id": amid, "content": content,
                                         "tags": ["mcp", "help"],
                                         "created_at": _msg_created_at(rt, amid)})
        await emit("done", {})
        return
    server, tool = full.split("__", 1)
    args: dict = {}
    if rest.strip():
        try:
            args = json.loads(rest)
        except Exception:  # noqa: BLE001
            args = {"arg": rest.strip()}
    from .mcp import call_mcp_tool

    prompt = json.dumps(args)[:500] or f"{server}__{tool}"

    if bg:
        # Background mode: record a running run, complete it in a task so a long
        # tool doesn't block the chat turn.
        run_id = rt.store.add_run(
            prompt=prompt, reply="", status="running",
            started_at=time.time(), finished_at=time.time(),
            kind="mcp_tool", label=f"{server}__{tool}", model="MCP")

        async def _bg_run():
            try:
                btext, berr, _bimg = await call_mcp_tool(
                    mcp_registry, server, tool, args,
                    permissions=rt.permissions, broker=broker, emit=emit)
                rt.store.update_run(
                    run_id, status="done" if not berr else "error",
                    reply=btext[:8000], finished_at=time.time())
                icon = "❌" if berr else "🔌"
                note = (f"{icon} `{server}__{tool}` finished — "
                        f"[run #{run_id}](/api/projects/{rt.name}/runs/{run_id})")
                try:
                    await emit("notice", {"message": note})
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                rt.store.update_run(run_id, status="error",
                                    reply=f"{type(e).__name__}: {e}"[:4000],
                                    finished_at=time.time())
                try:
                    await emit("error", {"message": f"{server}__{tool} failed: {e}"})
                except Exception:  # noqa: BLE001
                    pass
        _asyncio.get_running_loop().create_task(_bg_run())
        content = (f"⏳ `{server}__{tool}` started in the background — "
                   f"[run #{run_id}](/api/projects/{rt.name}/runs/{run_id}). "
                   "You'll get a notice when it finishes.")
        tags = ["mcp", "tool", "background"]
        amid = rt.store.add_message("assistant", content, {"tags": tags})
        await emit("assistant_message", {"id": amid, "content": content,
                                         "tags": tags,
                                         "created_at": _msg_created_at(rt, amid)})
        await emit("done", {})
        return

    res_text, is_err, _imgs = await call_mcp_tool(
        mcp_registry, server, tool, args,
        permissions=rt.permissions, broker=broker, emit=emit)
    if is_err and "not found" in res_text:
        listing = await _mcp_tool_listing()
        res_text += f"\n\nAvailable tools:\n{listing[:2000]}"
    icon = "❌" if is_err else "🔌"
    shown = res_text
    try:
        parsed = json.loads(res_text)
        shown = json.dumps(parsed, indent=2, default=str)
    except Exception:  # noqa: BLE001
        pass
    truncated = len(shown) > 4000
    content = (f"{icon} `{server}__{tool}`\n\n```json\n{shown[:4000]}\n```")
    tags = ["mcp", "tool"]
    amid = rt.store.add_message("assistant", content, {"tags": tags})
    await emit("assistant_message", {"id": amid, "content": content,
                                     "tags": tags,
                                     "created_at": _msg_created_at(rt, amid)})
    # Record the direct call as a run so the Experiments timeline shows it.
    run_id = None
    if not res_text.strip().startswith("[denied]") \
            and not res_text.strip().startswith("[error]"):
        try:
            run_id = rt.store.add_run(
                prompt=json.dumps(args)[:500] or f"{server}__{tool}",
                reply=res_text[:8000], status="done" if not is_err else "error",
                started_at=time.time(), finished_at=time.time(),
                kind="mcp_tool", label=f"{server}__{tool}", model="MCP")
        except Exception:  # noqa: BLE001
            pass
    if truncated:
        note = (f"\n\n_Result truncated in chat — "
                + (f"full output saved as run #{run_id}." if run_id else "see the MCP panel.")
                + "_")
        await emit("assistant_message", {"id": None, "content": note,
                                         "tags": ["mcp", "note"],
                                         "created_at": time.time()})
    await emit("done", {})


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

    if cmd == "/chart":
        if not arg:
            await reply("Usage: `/chart <experiment|run_id> [metric]` — chart a "
                        "run's metrics or an experiment's goal-metric evolution "
                        "with Flint.", ["experiments", "chart"])
            return True
        parts = arg.split()
        target = parts[0].lstrip("#")
        metric = parts[1] if len(parts) > 1 else ""
        from .routers.runs import run_chart, experiment_chart
        res = None
        label = ""
        if target.isdigit():
            res = await run_chart(rt.name, int(target))
            label = f"Run #{target} metrics"
        else:
            eid = _resolve_experiment_id(rt, target, "")
            e = rt.store.get_experiment(eid) if eid is not None else None
            if e is None:
                await reply(f"No experiment matched “{target}”.", ["experiments"])
                return True
            res = await experiment_chart(rt.name, eid, metric)
            label = (f"{e['name']} — "
                     f"{res.get('metric') or metric or e.get('goal_metric') or 'metric'}"
                     " across runs") if isinstance(res, dict) \
                else f"{e['name']} chart"
        if isinstance(res, dict) and res.get("ok"):
            content = (f"📊 {label}\n\n"
                       f"![chart](/artifacts/{res['artifact_id']})")
            await reply(content, ["experiments", "chart"])
        else:
            detail = getattr(res, "body", b"").decode("utf-8", "replace") \
                if hasattr(res, "body") else "flint charts server unavailable"
            await reply(f"❌ Chart failed: {detail}", ["experiments", "chart"])
        return True

    if cmd in ("/complete", "/cancel", "/activate"):
        status = {"complete": "completed", "cancel": "cancelled",
                  "activate": "active"}[cmd[1:]]
        if not arg:
            await reply(f"Usage: `{cmd} <name|id>` — mark an experiment "
                        f"`{status}`.", ["experiments"])
            return True
        eid = int(arg) if arg.isdigit() else _resolve_experiment_id(rt, arg, "")
        e = rt.store.get_experiment(eid) if eid is not None else None
        if e is None:
            await reply(f"No experiment matched “{arg}”.", ["experiments"])
            return True
        rt.store.update_experiment_status(eid, status)
        note = f"**{e['name']}** (#{eid}) → `{status}`."
        if status == "completed":
            note += " Publishing the aggregate report…"
            try:
                from .routers.runs import publish_experiment_report
                await publish_experiment_report(rt.name, eid)
            except Exception:  # noqa: BLE001
                pass
        await reply(note, ["experiments"])
        return True

    if cmd == "/focus":
        focus = rt.store.get_setting("focus_experiment_id", "")
        if not arg:
            if str(focus).isdigit() and rt.store.get_experiment(int(focus)) is not None:
                e = rt.store.get_experiment(int(focus))
                await reply(f"Focused experiment: **{e['name']}** (#{e['id']}). "
                            "Use `/focus <name|id>` or `/focus off` to change it.",
                            ["experiments", "focus"])
            else:
                await reply("No experiment focused. Use `/focus <name|id>` to steer the "
                            "agent toward one objective, or `/focus off` to clear.",
                            ["experiments", "focus"])
            return True
        if arg.lower() in ("off", "none", "clear"):
            rt.store.set_setting("focus_experiment_id", "")
            await reply("Focus cleared — the agent will follow the most recently "
                        "active experiment.", ["experiments", "focus"])
            return True
        eid = _resolve_experiment_id(rt, arg, "")
        if eid is None:
            await reply(f"No experiment matches {arg!r}.", ["experiments", "focus"])
            return True
        rt.store.set_setting("focus_experiment_id", str(eid))
        e = rt.store.get_experiment(eid)
        await reply(f"Now focusing on: **{e['name']}** (#{eid}). All future runs "
                    "will be associated with this experiment.", ["experiments", "focus"])
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


async def _launch_experiment_job(rt, coordinator, emit, text, intent,
                                 experiment_id, msg_extra, user_tags):
    """Run a UI-launched experiment job (parameter sweep or finetune setup).

    Both are deterministic — no LLM round-trip. The job resolves the experiment,
    records a user message, runs the coordinator tool, and emits the assistant
    reply + done, so the chat window and pipeline view capture the whole launch.
    """
    from .sweep import (expand_sweep_grid, sweep_label_prefix,
                        validate_sweep_request)
    from .finetune import finetune_summary, normalize_finetune_config

    eid = _resolve_experiment_id(rt, text, experiment_id)
    if eid is None:
        await emit("error", {"message":
            "No experiment found. Create one first (chat or the Experiments tab)."})
        await emit("done", {})
        return
    coordinator.ctx.experiment_id = str(eid)
    # A launched variant derives from the experiment's best run (branch lineage).
    best_id = _best_run_id(rt.store, eid)
    coordinator.ctx.parent_run_id = best_id

    mid = rt.store.add_message("user", text or intent,
                               {"tags": user_tags, "experiment_id": eid})
    await emit("user_message", {"id": mid, "content": text or intent,
                                "tags": user_tags, "created_at": _msg_created_at(rt, mid)})
    try:
        if intent == "run_sweep":
            sweep = dict(msg_extra.get("sweep") or {})
            code = str(sweep.get("code") or "").strip()
            grid = sweep.get("grid") or {}
            configs = sweep.get("configs") or None
            points = expand_sweep_grid(grid, configs)
            warn = validate_sweep_request(code, points)
            if warn:
                await emit("status", {"message": warn})
            await emit("status", {"message":
                f"Running parameter sweep · {len(points)} point(s)…"})
            result = await coordinator.tools["run_sweep"](
                code, points, sweep_label_prefix(
                    sweep.get("label_prefix") or "", len(points)))
        else:  # finetune
            fcfg = normalize_finetune_config(dict(msg_extra.get("finetune") or {}))
            await emit("status", {"message":
                f"Setting up finetune of {fcfg.get('base_model') or '?'}…"})
            result = await coordinator.tools["run_finetune"](
                base_model=fcfg["base_model"], dataset=fcfg["dataset"],
                epochs=fcfg["epochs"], learning_rate=fcfg["learning_rate"],
                batch_size=fcfg["batch_size"], lora_r=fcfg["lora_r"],
                task=fcfg.get("task") or "classification")
    except Exception as e:  # noqa: BLE001
        await emit("status", {"message": ""})
        await emit("error", {"message": f"{type(e).__name__}: {e}"})
        await emit("done", {})
        return
    await emit("status", {"message": ""})
    amid = rt.store.add_message(
        "assistant", result or "(no output)",
        {"tags": message_tags("assistant", result or "")})
    await emit("assistant_message", {"id": amid, "content": result or "(no output)",
                                     "tags": message_tags("assistant", result or ""),
                                     "created_at": _msg_created_at(rt, amid)})
    # Refresh the experiments tab (runs were recorded under the experiment).
    await emit("done", {})


def _best_run_id(store, eid: int) -> int | None:
    """The experiment's best run id (by its goal metric) — the parent for
    launched sweep/finetune variants."""
    exp = store.get_experiment(eid)
    if exp is None:
        return None
    metric = (exp.get("goal_metric") or "").strip()
    higher = bool(exp.get("higher_better", True))
    best, best_id = None, None
    for r in store.experiment_runs(eid):
        m = (r.get("metrics") or {}).get(metric) if metric else None
        if m is None:
            continue
        try:
            m = float(m)
        except (TypeError, ValueError):
            continue
        if best is None or (m > best if higher else m < best):
            best, best_id = m, r.get("id")
    if best_id is not None:
        return best_id
    runs = store.experiment_runs(eid)
    return runs[-1]["id"] if runs else None


def _resolve_experiment_id(rt: ProjectRuntime, text: str, experiment_id: str = "") -> int | None:
    """Resolve an experiment id for the improve loop.

    Uses the explicit id when given, otherwise matches the experiment name in the
    prompt, otherwise the project's focused experiment, otherwise falls back to
    the most recently created experiment.
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
        focus = rt.store.get_setting("focus_experiment_id", "")
        if str(focus).isdigit():
            for e in exps:
                if e["id"] == int(focus):
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
    # Round-6: background tasks (campaigns) broadcast to every open window.
    rt.subscribe_events(emit)

    # Finetune chat integration: tail dk-lora job logs into this chat window and
    # push the current pipeline snapshot so the pipeline card renders instantly
    # (subsequent updates stream via finetune_log / finetune_pipeline events).
    # Only the session that owns the finetune workspace shows the card — a
    # freshly created session must not inherit another project's pipeline.
    from . import finetune_status as fs
    if fs.owns_finetune(name):
        rt.start_finetune_monitor()
        try:
            await emit("finetune_pipeline", {"pipeline": fs.pipeline_snapshot()})
        except Exception:  # noqa: BLE001
            pass

    broker = ApprovalBroker(emit, store=rt.store, audit=rt.audit_emitter,
                            session_id=rt.name, agent_id="Fox")
    if not hasattr(rt, "_plan_approvals"):
        rt._plan_approvals = {}

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
            model=r.get("model") or None,
            code=r.get("code"),
            env=r.get("env"),
            message_id=r.get("message_id") or None)
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
                # Deterministic MCP orchestration: @mcp <server>__<tool> [json].
                if text.startswith("@mcp "):
                    await _handle_mcp_command(rt, emit, broker, text)
                    return
                user_tags = message_tags("user", text)
                # Deterministic privacy / re-identification routing — BEFORE the
                # LLM loop. These requests must always produce a plan, never the
                # (tool-light) model loop that describes work without doing it.
                if not intent and _is_privacy_experiment_request(text):
                    intent = "experiment_plan"
                    user_tags = ["experiment", "plan", "privacy"]
                    if _agent_looping(rt):
                        # The model already repeated the same reply: skip the
                        # loop entirely and go straight to the plan.
                        try:
                            await emit("notice", {"message":
                                "🔁 Detected a repeated response — routing this "
                                "request to the deterministic planner (propose → "
                                "confirm → execute) instead of the model loop."})
                        except Exception:  # noqa: BLE001
                            pass
                # Explicit intents (from the UI quick-action buttons) route
                # deterministically instead of relying on keyword matching.
                workflow_mode = compare_mode = fresh_mode = god_mode = False
                plan_step_id = None
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
                elif intent == "rerun_run":
                    # "Revert to this run": re-issue the run's prompt as a fresh
                    # turn so work continues from that exact configuration
                    # (child of the reverted run in the branch graph).
                    user_tags = ["rerun run"]
                elif intent == "retry_stage":
                    # Retry a failed workflow stage (resumable pipelines only).
                    user_tags = ["retry stage"]
                elif intent == "campaign":
                    # Round-5: plan + run a multi-step research campaign.
                    user_tags = ["campaign"]
                elif intent == "eval":
                    # Round-9: benchmark the workbench's LLMs on a task.
                    user_tags = ["eval"]
                elif intent == "run_sweep":
                    # Round-29: UI-launched parameter sweep. Configs come from
                    # msg_extra.sweep {code, grid|configs, label_prefix}; the
                    # code reads `config` and reports metrics via report_metric.
                    user_tags = ["parameter sweep"]
                elif intent == "finetune":
                    # Round-29: UI-launched finetune setup. msg_extra.finetune
                    # carries base_model/dataset/hyperparameters; records a
                    # kind=finetune run with a generated training script.
                    user_tags = ["finetune"]
                elif intent == "plan_step":
                    # Round-30: run one experiment plan step as a chat turn.
                    user_tags = ["experiment", "plan step"]
                elif intent == "peer_experiment":
                    user_tags = ["peer experiment"]
                elif intent == "experiment_plan":
                    user_tags = ["experiment", "plan"]
                elif intent == "privacy_suite":
                    user_tags = ["privacy", "suite"]
                elif intent == "upi_generate":
                    user_tags = ["dataset", "synthetic", "upi"]
                elif intent == "chart":
                    user_tags = ["chart"]
                else:
                    rcc = rerun_compare_requested(text)
                    workflow_mode = bool(match_workflow(text) or
                                         compare_requested(text) or rcc)
                    compare_mode = compare_requested(text) or rcc
                    fresh_mode = fresh_requested(text) or rcc
                    if rcc:
                        user_tags = ["privacy workflow", "fresh rerun", "compare runs"]
                    # Deterministic routing for the peer-identification /
                    # market-share experiment — the agent's free-form version
                    # was producing empty replies with no tool calls, so detect
                    # the request and run the deterministic experiment instead.
                    if _is_peer_experiment_request(text):
                        intent = "peer_experiment"
                        user_tags = ["peer experiment"]
                    elif _is_experiment_plan_request(text):
                        intent = "experiment_plan"
                        user_tags = ["experiment", "plan"]
                    elif _is_chart_request(text):
                        intent = "chart"
                        user_tags = ["chart"]
                    elif _is_upi_generate_request(text):
                        intent = "upi_generate"
                        user_tags = ["dataset", "synthetic", "upi"]
                    elif _is_privacy_suite_request(text):
                        intent = "privacy_suite"
                        user_tags = ["privacy", "suite"]
                    elif _is_privacy_experiment_request(text):
                        intent = "experiment_plan"
                        user_tags = ["experiment", "plan", "privacy"]
                if intent == "rerun_run":
                    # Revert-to-this-run: derive from the requested run and
                    # re-issue its prompt (if no explicit text was provided).
                    rid = (msg_extra.get("run_id") if msg_extra else None)
                    if str(rid).isdigit():
                        target = rt.store.get_run(int(rid))
                        if target is not None:
                            coordinator.ctx.parent_run_id = int(rid)
                            if not (text or "").strip():
                                text = target.get("prompt") or text
                if intent == "chart":
                    await _handle_chart_request(rt, emit, text, msg_extra)
                    return
                if intent == "privacy_suite":
                    await _handle_privacy_suite(rt, emit, text, msg_extra)
                    return
                if intent == "upi_generate":
                    await _handle_upi_generate(rt, emit, text, msg_extra)
                    return
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
                        lambda extra="": Reviewer(rt.llm, rt.store).review(extra),
                        eid, text, emit=emit, workflow=rt.workflow)
                    await emit("status", {"message": ""})
                    await emit("done", {})
                    return
                if intent == "eval":
                    # Launch a background model benchmark (round-9).
                    cfg = dict(msg_extra.get("eval") or {})
                    models = [m for m in (cfg.get("models") or []) if str(m).strip()]
                    if not models:
                        await emit("error", {"message":
                            "No models specified for the eval."})
                        await emit("done", {})
                        return
                    name = (cfg.get("name") or "Eval").strip() or "Eval"
                    prompt = cfg.get("prompt") or text or "Run the experiment and report the goal metric."
                    goal_metric = (cfg.get("goal_metric") or "").strip()
                    higher = bool(cfg.get("higher_better", True))
                    eid = rt.store.create_eval(name, prompt, models, goal_metric, higher)
                    ok, msg = rt.start_eval(eid)
                    if ok:
                        await emit("status", {"message": "Model benchmark started in the background…"})
                        await emit("notice", {"message":
                            f"Model benchmark '{name}' running in the background "
                            f"across {len(models)} model(s)."})
                    else:
                        await emit("error", {"message": msg})
                    await emit("done", {})
                    return
                if intent == "campaign":
                    cfg = dict(msg_extra.get("campaign") or {})
                    name = (cfg.get("name") or "campaign").strip() or "Campaign"
                    question = text or cfg.get("question") or ""
                    goal_metric = (cfg.get("goal_metric") or "").strip()
                    higher = bool(cfg.get("higher_better", True))
                    cid = rt.store.create_campaign(name, question, goal_metric, higher)
                    plan_steps = cfg.get("plan_steps")
                    ok, msg = rt.start_campaign(cid, plan_steps=plan_steps)
                    if ok:
                        await emit("status", {"message": "Campaign started — running in the background…"})
                        await emit("notice", {"message":
                            f"Campaign '{name}' started in the background. "
                            "You can keep using the workbench; progress streams live."})
                    else:
                        await emit("error", {"message": msg})
                    await emit("done", {})
                    return
                if intent == "run_sweep" or intent == "finetune":
                    await _launch_experiment_job(
                        rt, coordinator, emit, text, intent, experiment_id,
                        msg_extra, user_tags)
                    return
                if intent == "finetune_summary":
                    # Deterministic summary + report of the finetune pipeline,
                    # posted straight to the chat (no LLM round-trip).
                    from . import finetune_status as fs
                    snap = fs.pipeline_snapshot()
                    summary = ["**🔧 LoRA finetune pipeline — summary**", ""]
                    for s in snap.get("stages") or []:
                        ico = {"pending": "○", "running": "◔",
                               "done": "✓", "failed": "✗"}.get(s.get("state"), "·")
                        summary.append(f"- {ico} **{s.get('label')}** — {s.get('detail') or s.get('state')}")
                    summary.append("")
                    summary.append(f"**Pipeline:** {snap.get('pct')}% · {snap.get('message')} · ETA {snap.get('eta') or '—'}")
                    latest_report = ""
                    try:
                        vruns = fs.validate_runs()
                        done = [r for r in vruns if r.get("status") == "done"]
                        if done:
                            latest_report = done[0].get("report") or ""
                    except Exception:  # noqa: BLE001
                        pass
                    content = "\n".join(summary)
                    if latest_report:
                        content += "\n\n---\n\n" + latest_report
                    amid = rt.store.add_message("assistant", content, {"tags": ["finetune", "report", "summary"]})
                    await emit("assistant_message", {"id": amid, "content": content,
                                                     "tags": ["finetune", "report", "summary"],
                                                     "created_at": _msg_created_at(rt, amid)})
                    await emit("done", {})
                    return
                if intent == "peer_experiment":
                    # Deterministic bank peer-identification / market-share
                    # experiment — runs directly (no LLM loop), posts results.
                    from .routers.peer import run_peer_share_experiment, \
                        render_report, render_figures
                    filename = (msg_extra.get("filename") or "").strip()
                    try:
                        import pandas as pd
                        from pathlib import Path as _P
                        if filename:
                            path = _P(filename)
                            if not path.is_absolute():
                                path = rt.dir / filename
                        else:
                            cands = [p for p in rt.dir.iterdir()
                                     if p.is_file() and p.suffix.lower() == ".csv"
                                     and not p.name.lower().startswith("synthetic_")]
                            upi = [p for p in cands if "upi" in p.name.lower()]
                            cands = upi or cands
                            if not cands:
                                raise FileNotFoundError("no CSV dataset in this project")
                            path = cands[0]
                            for p in cands:
                                try:
                                    head = pd.read_csv(p, nrows=1)
                                    if "sender_bank" in head.columns:
                                        path = p
                                        break
                                except Exception:  # noqa: BLE001
                                    continue
                            filename = path.name
                        df = pd.read_csv(path, low_memory=False)
                        if "sender_bank" not in df.columns:
                            raise ValueError("dataset has no 'sender_bank' column")
                        res = run_peer_share_experiment(df)
                        report_md = render_report(res)
                        figures = render_figures(res)
                        # Register figures + report as artifacts.
                        artifact_ids = []
                        env = {}
                        try:
                            env = await rt.kernels.get_env()
                        except Exception:  # noqa: BLE001
                            pass
                        from .artifacts.store import Artifact
                        for name_, data in figures.items():
                            art = Artifact(kind="figure", name=name_,
                                           description=f"Peer experiment figure: {name_}",
                                           code=f"peer({filename})", env=env,
                                           message_id="", run_id="", data_type="png")
                            rt.artifacts.add_artifact(art, data=data, data_type="png")
                            artifact_ids.append(art.id)
                            await emit("artifact", {"artifact": art.to_dict()})
                        rep_art = Artifact(kind="report",
                                           name=f"peer-report-{_P(filename).stem}",
                                           description=f"Peer identification report for {filename}",
                                           code=f"peer({filename})", env=env,
                                           message_id="", run_id="", data_type="text")
                        rt.artifacts.add_artifact(rep_art, data=report_md.encode(),
                                                  data_type="text")
                        artifact_ids.append(rep_art.id)
                        await emit("artifact", {"artifact": rep_art.to_dict()})
                        rt.store.add_run(
                            prompt=f"Peer identification & market-share experiment on {filename}",
                            reply=report_md[:3000], status="done",
                            started_at=time.time(), finished_at=time.time(),
                            artifact_ids=artifact_ids,
                            metrics={
                                "identification_accuracy": res["identification"]["overall_accuracy"],
                                "segment_mae": res["segments_error"]["mae"],
                                "type_mae": res["types_error"]["mae"],
                            },
                            kind="peer_experiment", label=f"peer:{filename}",
                            model=None, dataset=filename)
                        content = (
                            f"**🏦 Peer identification & market-share experiment — `{filename}`**\n\n"
                            f"- Transactions: {res['n']:,} · Banks: {', '.join(res['banks'])}\n"
                            f"- **Identification accuracy:** {res['identification']['overall_accuracy']:.1%}\n"
                            f"- **Share-estimation MAE:** {res['segments_error']['mae']:.3f} per segment · "
                            f"{res['types_error']['mae']:.3f} per payment type\n"
                            f"- Confusion matrix, segment/type error, and bank volumes "
                            f"registered as artifacts.\n\n"
                            + report_md)
                        amid = rt.store.add_message(
                            "assistant", content,
                            {"tags": ["peer", "experiment", "report"]})
                        await emit("assistant_message", {"id": amid, "content": content,
                                                         "tags": ["peer", "experiment", "report"],
                                                         "created_at": _msg_created_at(rt, amid)})
                    except Exception as e:  # noqa: BLE001
                        await emit("error", {"message":
                            f"Peer experiment failed: {type(e).__name__}: {e}"})
                    await emit("done", {})
                    return
                if intent == "experiment_plan":
                    # Robust deterministic experiment planner: build a concrete
                    # plan, propose it to the user in the chat (plan card),
                    # wait for confirmation, then execute + present the result.
                    from .experiment_planner import PlanStore, list_experiments
                    from .routers.experiment_planner import \
                        plan_proposal_payload, present_result
                    pstore = PlanStore(rt.dir)

                    experiment_id = (msg_extra.get("experiment_id") or "").strip()
                    dataset = (msg_extra.get("dataset") or "").strip()
                    request = text or msg_extra.get("request") or ""

                    # If no explicit experiment id, try to match one from the
                    # request text (e.g. "peer", "eda"), then by aliases
                    # (e.g. "reidentification" -> reid_risk).
                    if not experiment_id:
                        low = request.lower()
                        avail = list_experiments()
                        for e in avail:
                            if e["id"] in low or e["name"].lower() in low:
                                experiment_id = e["id"]
                                break
                    if not experiment_id:
                        experiment_id = _experiment_from_text(request)
                    if not experiment_id:
                        # Default to the peer experiment when it's about banks.
                        if any(w in low for w in ("bank", "peer", "upi")):
                            experiment_id = "peer"
                    if not experiment_id:
                        await emit("error", {"message":
                            "No experiment matched. Available: "
                            + ", ".join(e["id"] for e in list_experiments())})
                        await emit("done", {})
                        return

                    # Resolve the dataset: explicit, or auto-pick a UPI/banking
                    # CSV, or (when the project has none) generate a
                    # deterministic synthetic one so the plan can always run.
                    from .experiment_planner import ensure_runnable_dataset
                    if not dataset:
                        dataset, synthetic = ensure_runnable_dataset(rt.dir)
                        if synthetic:
                            try:
                                await emit("notice", {"message":
                                    f"No dataset found in the project — generated "
                                    f"a deterministic synthetic UPI dataset "
                                    f"(`{dataset}`) so the experiment can run. "
                                    "Upload a real CSV to analyze your own data."})
                            except Exception:  # noqa: BLE001
                                pass
                    if not dataset:
                        await emit("error", {"message":
                            "No dataset in the project — upload a CSV first."})
                        await emit("done", {})
                        return

                    try:
                        plan = pstore.create(
                            experiment_id=experiment_id,
                            request=request, dataset=dataset,
                            seed=msg_extra.get("seed"))
                        pstore.propose(plan["id"])
                    except ValueError as e:
                        await emit("error", {"message": str(e)})
                        await emit("done", {})
                        return

                    payload = plan_proposal_payload(pstore.get(plan["id"]))
                    # Persist + show the plan card in chat, then wait for the
                    # user's approval via experiment_plan_decision (plan_id key).
                    await emit("experiment_plan_proposal", payload)
                    amid = rt.store.add_message(
                        "assistant",
                        "**🧪 Experiment plan proposed — confirm to execute**\n\n"
                        + _render_plan_md(payload),
                        {"tags": ["experiment_plan", "plan", "proposal"]})
                    await emit("assistant_message", {"id": amid,
                                                     "content": "**🧪 Experiment plan proposed — confirm to execute**\n\n" + _render_plan_md(payload),
                                                     "tags": ["experiment_plan", "plan", "proposal"],
                                                     "created_at": _msg_created_at(rt, amid)})
                    await emit("status", {"message":
                        "⏸ Awaiting your approval to run the experiment plan…"})
                    fut = asyncio.get_event_loop().create_future()
                    try:
                        rt._plan_approvals[plan["id"]] = fut
                    except Exception:  # noqa: BLE001
                        pass
                    ok = False
                    try:
                        ok = await asyncio.wait_for(fut, timeout=300)
                    except asyncio.TimeoutError:
                        ok = False
                    try:
                        rt._plan_approvals.pop(plan["id"], None)
                    except Exception:  # noqa: BLE001
                        pass
                    await emit("status", {"message": ""})
                    if not ok:
                        try:
                            pstore.decide(payload["plan_id"], False, by="user")
                        except Exception:  # noqa: BLE001
                            pass
                        await emit("notice", {"message":
                            f"Experiment plan {payload['plan_id']} rejected — nothing ran."})
                        await emit("done", {})
                        return
                    # Approved: run in a detached background task so the turn
                    # doesn't block and the result posts even if the tab closes.
                    try:
                        pstore.decide(payload["plan_id"], True, by="user")
                    except Exception:  # noqa: BLE001
                        pass
                    run_plan = pstore.get(payload["plan_id"])

                    async def _execute_plan(run_plan=run_plan):
                        try:
                            from .routers.experiment_planner import present_result
                            await present_result(rt, run_plan, emit=emit)
                        except Exception as e:  # noqa: BLE001
                            try:
                                pstore.update(run_plan["id"], status="FAILED",
                                              error=f"{type(e).__name__}: {e}")
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                await emit("error", {"message":
                                    f"Experiment failed: {type(e).__name__}: {e}"})
                                await emit("done", {})
                            except Exception:  # noqa: BLE001
                                pass

                    asyncio.create_task(_execute_plan())
                    await emit("status", {"message":
                        "✅ Plan approved — executing in the background…"})
                    await emit("done", {})
                    return
                if intent == "plan_step":
                    # Round-30: run one experiment plan step. Resolve the
                    # experiment + step, bind the coordinator, mark the step
                    # running, then fall through to the normal agent turn so the
                    # pipeline captures it. After the turn the step is resolved
                    # to done with the produced run.
                    sid = (msg_extra.get("step_id") if msg_extra else None)
                    if not str(sid).isdigit():
                        await emit("error", {"message": "No plan step specified."})
                        await emit("done", {})
                        return
                    step = rt.store.get_experiment_step(int(sid))
                    if step is None:
                        await emit("error", {"message": "Plan step not found."})
                        await emit("done", {})
                        return
                    eid = step.get("experiment_id")
                    coordinator.ctx.experiment_id = str(eid)
                    coordinator.ctx.parent_run_id = _best_run_id(rt.store, eid)
                    rt.store.update_experiment_step(int(sid), status="running")
                    plan_step_id = int(sid)
                if intent == "retry_stage":
                    await emit("status", {"message": "Retrying workflow stage…"})
                    snap = rt.workflow.snapshot()
                    inv = snap.get("invoke") or {}
                    stage = (msg_extra.get("stage") if msg_extra else "") or ""
                    if inv.get("kind") == "improve" and str(stage).startswith("iter"):
                        n = str(stage).replace("iter", "")
                        eid = inv.get("experiment_id")
                        if str(n).isdigit() and eid is not None:
                            result = await run_improve_loop(
                                rt.store, coordinator, rt.build_llm_messages,
                                lambda extra="": Reviewer(rt.llm, rt.store).review(extra),
                                int(eid), inv.get("prompt") or text, emit=emit,
                                workflow=rt.workflow,
                                iterations=int(inv.get("iterations") or 3),
                                start_at=int(n))
                            await emit("status", {"message": ""})
                            await emit("done", {})
                            return
                    if inv.get("kind") == "campaign" and str(stage).startswith("step"):
                        from .campaign import run_campaign
                        n = str(stage).replace("step", "")
                        cid = inv.get("campaign_id")
                        if str(n).isdigit() and cid is not None:
                            result = await run_campaign(
                                rt, coordinator, rt.build_llm_messages, int(cid),
                                emit=emit, workflow=rt.workflow, resume_step=int(n))
                            await emit("status", {"message": ""})
                            await emit("done", {})
                            return
                    await emit("error", {"message":
                        "Cannot retry this stage — no resumable workflow is active."})
                    await emit("done", {})
                    return
                # A free-form turn with no experiment context inherits the
                # focused experiment, so runs/timelines attach automatically
                # instead of drifting to a stale experiment id.
                exp_meta = {}
                if plan_step_id:
                    exp_meta["experiment_id"] = int(coordinator.ctx.experiment_id)
                elif not str(coordinator.ctx.experiment_id).isdigit():
                    fid = rt.store.get_setting("focus_experiment_id", "")
                    if str(fid).isdigit() and rt.store.get_experiment(int(fid)) is not None:
                        coordinator.ctx.experiment_id = fid
                        exp_meta["experiment_id"] = int(fid)
                mid = rt.store.add_message("user", text,
                                           {"tags": user_tags, **exp_meta})
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
                    # run_privacy_workflow persists + emits the result from
                    # inside its shielded task, so it survives client disconnect
                    # (no duplicate here).
                    if emit:
                        try:
                            await emit("done", {})
                        except Exception:  # noqa: BLE001
                            pass
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
                # Loop guard: a Continue click after the model has produced the
                # same near-identical reply twice means it's re-planning instead
                # of finishing — break the loop instead of burning another turn.
                if _is_continue_request(text) and _agent_looping(rt):
                    await emit("notice", {"message": (
                        "⚠️ The agent has repeated the same reply without making "
                        "progress — this looks like a loop, so I stopped instead "
                        "of running another turn. Try one of these:\n\n"
                        "1. Ask for **one concrete step** and name the exact "
                        "output, e.g. \"run the regeneration attack on "
                        "transaction_type and show the result now\".\n"
                        "2. Run it **deterministically**: `@eda <file>`, "
                        "`@mcp <server>__<tool>`, `/chart`, or the Experiments "
                        "→ Plans planner (propose → confirm → execute).\n"
                        "3. **/session** to start fresh so the model isn't "
                        "drowning in old tool output.")})
                    await emit("done", {})
                    return
                await rt.maybe_compact()
                llm_msgs = rt.build_llm_messages()
                if _is_continue_request(text):
                    # Help the model actually continue: it has the full tool
                    # history, so tell it not to re-plan or repeat.
                    llm_msgs.insert(0, {"role": "system", "content": (
                        "The user asked you to continue from where you stopped. "
                        "You already have the full tool history in this "
                        "conversation. Do NOT re-plan, re-describe, or repeat "
                        "earlier steps. Execute the immediate next concrete step "
                        "with a real tool call, then finish with the result.")})
                # Branching lineage: a turn that applies a reviewer suggestion
                # ("Apply & rerun") derives from the run it improves; anything
                # after a "fresh rerun"/autoresearch rerun derives from the last
                # run of the same kind. Used by the branch-history graph.
                applied_suggestion_id = None
                try:
                    if intent == "rerun_suggestion":
                        # First-class suggestion apply: parent = the run whose
                        # review produced the suggestion; mark it applied so its
                        # outcome is resolved (regression check) after the turn.
                        sid = (msg_extra.get("suggestion_id") if msg_extra else None)
                        if str(sid).isdigit():
                            sug = rt.store.get_suggestion(int(sid))
                            if sug is not None:
                                applied_suggestion_id = sug["id"]
                                rt.store.mark_suggestion_applied(sug["id"])
                                if sug["source_run_id"]:
                                    coordinator.ctx.parent_run_id = sug["source_run_id"]
                                else:
                                    coordinator.ctx.parent_run_id = None
                        if coordinator.ctx.parent_run_id is None:
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
                        from .agents.reviewer import build_review_context
                        review = await Reviewer(rt.llm, rt.store).review(
                            build_review_context(rt.store, runs_now[-1]))
                        if runs_now:
                            rid = runs_now[-1]["id"]
                            rt.store.update_run_review(rid, review)
                            _attach_suggestion_ids(rt.store, rid, review)
                        await emit("review", review)
                    except Exception:  # noqa: BLE001
                        await emit("review", {"findings": [], "suggestions": []})
                # Regression check for an applied suggestion: did the goal metric
                # actually improve vs. the run it was applied to?
                if applied_suggestion_id is not None:
                    try:
                        # Bind the applied suggestion to the run it produced
                        # before resolving (regression check + learning).
                        if runs_now:
                            rt.store.mark_suggestion_applied(
                                applied_suggestion_id, runs_now[-1]["id"])
                        outcome = rt.store.resolve_suggestion_outcome(applied_suggestion_id)
                        if outcome and outcome.get("status") in ("accepted", "rejected"):
                            label = "✓ improved the goal" if outcome.get("improved") else "✗ did not improve the goal"
                            delta = outcome.get("delta")
                            dstr = f"{delta:+.4g}" if delta is not None else "n/a"
                            await emit("notice", {"message": (
                                f"Suggestion \"{outcome.get('title') or 'applied'}\" "
                                f"{label} ({dstr} on {outcome.get('baseline_value')} "
                                f"→ {outcome.get('outcome_value')}).")})
                            # Round-7: remember this measured outcome.
                            try:
                                rt.store.record_suggestion_learning(outcome)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        pass
                await emit("status", {"message": ""})
                # Round-30: resolve a plan step that was run this turn — mark it
                # done and link the run it produced.
                if plan_step_id:
                    try:
                        rid = runs_now[-1]["id"] if runs_now else None
                        rt.store.update_experiment_step(
                            plan_step_id, status="done", run_id=rid,
                            note="Step completed by the agent.")
                    except Exception:  # noqa: BLE001
                        pass
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
                if plan_step_id:
                    try:
                        rt.store.update_experiment_step(plan_step_id, status="planned")
                    except Exception:  # noqa: BLE001
                        pass
                await emit("done", {})
            except LLMError as e:
                await emit("status", {"message": ""})
                await emit("error", {"message": str(e)})
                if plan_step_id:
                    try:
                        rt.store.update_experiment_step(plan_step_id, status="planned")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                await emit("status", {"message": ""})
                await emit("error", {"message": f"{type(e).__name__}: {e}"})
                if plan_step_id:
                    try:
                        rt.store.update_experiment_step(plan_step_id, status="planned")
                    except Exception:  # noqa: BLE001
                        pass

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
                elif mtype == "experiment_plan_decision":
                    # Resolve a pending experiment-plan approval (plan_id as key).
                    try:
                        fut = rt._plan_approvals.get(msg.get("plan_id", ""))
                        if fut and not fut.done():
                            fut.set_result(bool(msg.get("decision", False)))
                    except Exception:  # noqa: BLE001
                        pass
                elif mtype == "ping":
                    await emit("pong", {})
                elif mtype == "stop":
                    abort_event.set()
                elif mtype == "finetune_stage":
                    # Trigger a quai-lora pipeline stage from the chat window.
                    try:
                        from . import finetune_status as fs
                        stage = int((msg.get("stage") or 0))
                        req = fs.submit_stage(
                            stage, job_id=msg.get("job_id") or "",
                            options=msg.get("options") or {},
                            label=msg.get("label") or "")
                        await emit("notice", {"message": (
                            f"Queued finetune stage {stage} ({req['label']}) — "
                            "the host worker will run it; progress streams here.")})
                        await emit("finetune_pipeline", {
                            "pipeline": fs.pipeline_snapshot()})
                    except Exception as e:  # noqa: BLE001
                        await emit("error", {"message": f"Stage trigger failed: {e}"})
                elif mtype == "privacy_workflow":
                    # Run the privacy workflow as a detached background task so
                    # it survives client disconnects (like campaigns): the task
                    # posts its own report/message when done.
                    async def _pw():
                        try:
                            await run_privacy_workflow(
                                rt, emit, fresh=bool(msg.get("fresh")),
                                compare=bool(msg.get("compare")),
                                prompt=msg.get("content") or "privacy workflow")
                        except Exception as e:  # noqa: BLE001
                            await emit("error", {"message": f"Privacy workflow failed: {e}"})
                    asyncio.create_task(_pw())
                    await emit("status", {"message":
                        "Privacy workflow started in the background — peer "
                        "exploitation · red team · DP robustness…"})
                    await emit("done", {})
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
        # Reject any pending experiment-plan approvals so they don't hang.
        try:
            for fut in getattr(rt, "_plan_approvals", {}).values():
                if not fut.done():
                    fut.set_result(False)
        except Exception:  # noqa: BLE001
            pass
        rt.workflow.unsubscribe(emit)
        rt.unsubscribe_events(emit)
# ------------------------------------------------------------ static files ---

class NoCacheStaticFiles(StaticFiles):
    """Serve frontend assets with no-store headers so UI changes always apply
    immediately (defeats stale browser caches, incl. the HTML entrypoint)."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-store"
        return response


# Hosted GitBook docs (the gitbook/ folder) at /gitbook/ — open in a browser
# for the local documentation site (docsify bootstrap). Mounted BEFORE the root
# static mount so it isn't shadowed by the frontend.
from .paths import ROOT as _ROOT

_GITBOOK_DIR = _ROOT / "gitbook"
if _GITBOOK_DIR.is_dir():
    app.mount("/gitbook", StaticFiles(directory=str(_GITBOOK_DIR), html=True),
              name="gitbook")

app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
