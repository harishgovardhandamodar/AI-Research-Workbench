"""Fox - Experiment workbench: FastAPI backend.

Serves the web UI + JSON REST API + WebSocket chat. Each project gets its own
folder under <workbench>/projects with SQLite persistence, artifact storage and a
persistent Python kernel.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agents.approval import ApprovalBroker
from .agents.coordinator import Coordinator
from .agents.reviewer import Reviewer
from .agents.tools import ToolContext
from .artifacts.store import Artifact, ArtifactStore
from .experiments import build_graph, load_experiments
from .kernels.manager import KernelManager
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TOOL_BASE_URL, LLMClient, LLMError
from .mcp import DEFAULT_SERVERS, MCPRegistry
from .notebooks import NotebookError, NotebookService, new_notebook
from .paths import CONFIG_PATH, FRONTEND_DIR, PROJECTS_DIR, ROOT
from .permissions import PermissionManager
from .store import ProjectStore
from .workflows import WorkflowTracker

DEFAULT_CONFIG = {
    "llm": {
        "base_url": DEFAULT_BASE_URL,
        "tool_base_url": DEFAULT_TOOL_BASE_URL,
        "model": DEFAULT_MODEL,
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "agent": {"max_iters": 20, "reviewer_enabled": True},
    "mcp": {"servers": DEFAULT_SERVERS},
}

DEFAULT_SYSTEM_PROMPT = (
    "You are Fox, an open-source experiment workbench running fully on the user's "
    "machine with local models. You help computational biologists, chemists, "
    "physicists and data scientists run real analyses hands-on.\n\n"
    "Working style:\n"
    "- Solve problems by writing and running code in a persistent, sandboxed Python "
    "kernel (numpy, pandas, scipy, matplotlib). Variables persist across calls.\n"
    "- Use run_python for computation, analysis and figures. Make clear, well-labelled "
    "publication-style matplotlib figures.\n"
    "- Save important results (tables, summaries, data) with save_artifact so they "
    "become auditable artifacts.\n"
    "- Use run_shell only when necessary; it asks the user for permission. Prefer the "
    "Python kernel.\n"
    "- Every figure records its exact code and environment so it can be reproduced.\n"
    "- Be rigorous: cite numbers you actually computed. If you don't know, say so.\n\n"
    "Privacy: everything stays on the user's machine unless they explicitly approve a "
    "network-touching shell command."
)


# ---------------------------------------------------------------- config -----

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["llm"].update(saved.get("llm", {}))
            cfg["agent"].update(saved.get("agent", {}))
            if "servers" in saved.get("mcp", {}):
                # Keep user's servers but always surface the bundled default
                # servers (e.g. newly-added "privacy") unless overridden by name.
                by_name = {s.get("name"): s for s in saved["mcp"]["servers"]}
                merged = list(saved["mcp"]["servers"])
                for s in DEFAULT_SERVERS:
                    if s["name"] not in by_name:
                        merged.append(s)
                cfg["mcp"]["servers"] = merged
            return cfg
        except json.JSONDecodeError:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


CONFIG = load_config()


def make_llm() -> LLMClient:
    llm_cfg = CONFIG["llm"]
    return LLMClient(
        base_url=llm_cfg.get("base_url", DEFAULT_BASE_URL),
        tool_base_url=llm_cfg.get("tool_base_url", DEFAULT_TOOL_BASE_URL),
        model=llm_cfg.get("model", DEFAULT_MODEL),
        temperature=llm_cfg.get("temperature", 0.2),
        max_tokens=llm_cfg.get("max_tokens", 4096),
    )


# ------------------------------------------------------------ project runtime --

class ProjectRuntime:
    def __init__(self, name: str):
        self.name = name
        self.dir = PROJECTS_DIR / name
        self.store = ProjectStore(self.dir)
        self.artifacts = ArtifactStore(self.dir)
        self.kernels = KernelManager(self.dir)
        self.notebooks = NotebookService(self.dir, self.kernels.python)
        self.permissions = PermissionManager(self.store)
        self.lock = asyncio.Lock()
        self.llm = make_llm()
        self.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        self.max_iters = CONFIG["agent"].get("max_iters", 8)
        self.workflow = WorkflowTracker(
            persist=lambda snap: self.store.set_setting(
                "workflow_latest", json.dumps(snap)),
            record=self.store.add_workflow_run,
        )
        try:
            latest = self.store.get_setting("workflow_latest", "")
            self.workflow.restore(json.loads(latest) if latest else None)
        except Exception:  # noqa: BLE001
            pass

    def ctx(self, emit, approval) -> ToolContext:
        return ToolContext(kernels=self.kernels, artifacts=self.artifacts,
                           store=self.store, permissions=self.permissions,
                           approval=approval, emit=emit, notebooks=self.notebooks,
                           workflow=self.workflow)

    def build_llm_messages(self) -> list[dict]:
        from .agents.coordinator import SYSTEM_PROMPT
        from .skills import skills_context

        rows = self.store.list_messages()
        msgs: list[dict] = []
        for r in rows:
            role = r["role"]
            meta = r.get("meta") or {}
            if role == "system":
                continue
            if role == "user":
                msgs.append({"role": "user", "content": r["content"]})
            elif role == "assistant":
                d = {"role": "assistant", "content": r["content"]}
                tcs = meta.get("tool_calls")
                if tcs:
                    d["tool_calls"] = wire_tool_calls(tcs)
                msgs.append(d)
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""),
                             "content": r["content"]})
        sk = skills_context()
        system = SYSTEM_PROMPT + ("\n\n" + sk if sk else "")
        msgs.insert(0, {"role": "system", "content": system})
        return sanitize_messages(msgs)

    async def stop(self):
        await self.kernels.stop()


def wire_tool_calls(tcs: list) -> list:
    """Normalize stored tool_calls to the OpenAI wire format (arguments as JSON string)."""
    out = []
    for tc in tcs or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append({
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": json.dumps(args)},
        })
    return out


def sanitize_messages(msgs: list[dict]) -> list[dict]:
    """Ensure OpenAI tool-call history is well-formed (tool results follow calls)."""
    clean: list[dict] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            remaining = msgs[i + 1:]
            call_ids = {tc.get("id") for tc in m["tool_calls"]}
            if not any(r.get("role") == "tool" and r.get("tool_call_id") in call_ids
                       for r in remaining):
                m = {"role": "assistant", "content": m.get("content", "")}
        clean.append(m)
    return clean


# ------------------------------------------------------------------ app -----

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for rt in list(runtimes.values()):
        await rt.stop()


app = FastAPI(title="Fox - Experiment workbench", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

runtimes: dict[str, ProjectRuntime] = {}
_llm_cache: LLMClient | None = None
mcp_registry: MCPRegistry = MCPRegistry(CONFIG.get("mcp", {}).get("servers", []))


def get_runtime(name: str) -> ProjectRuntime:
    if name not in runtimes:
        runtimes[name] = ProjectRuntime(name)
    return runtimes[name]


def get_llm() -> LLMClient:
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = make_llm()
    return _llm_cache


# ------------------------------------------------------------- REST: config --

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return {"config": CONFIG}


@app.post("/api/config")
async def set_config(body: dict):
    global _llm_cache
    cfg = body.get("config", {})
    if "llm" in cfg:
        CONFIG["llm"].update(cfg["llm"])
    if "agent" in cfg:
        CONFIG["agent"].update(cfg["agent"])
    if "mcp" in cfg and "servers" in cfg["mcp"]:
        CONFIG["mcp"]["servers"] = cfg["mcp"]["servers"]
        await rebuild_mcp()
    save_config(CONFIG)
    _llm_cache = None
    for rt in runtimes.values():
        rt.llm = make_llm()
        rt.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        rt.max_iters = CONFIG["agent"].get("max_iters", 8)
    return {"config": CONFIG}


# ---------------------------------------------------------------- MCP --------

async def rebuild_mcp():
    global mcp_registry
    if mcp_registry is not None:
        await mcp_registry.close()
    mcp_registry = MCPRegistry(CONFIG.get("mcp", {}).get("servers", []))


@app.get("/api/mcp")
async def mcp_status():
    statuses = await mcp_registry.statuses()
    return {"servers": statuses, "installed": mcp_registry._available}


@app.get("/api/models")
async def list_models():
    try:
        return {"models": await get_llm().list_models()}
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)


# --------------------------------------------------- experiments / run history

@app.get("/api/experiments")
async def get_experiments():
    """List all privacy-workflow runs (timestamps, settings, metrics, artifacts)."""
    return {"experiments": load_experiments()}


@app.get("/api/experiments/graph")
async def get_experiments_graph():
    """Graph view: one node per run + similarity/overlap edges between runs."""
    return build_graph()


# --------------------------------------------------------- agent dashboard ----

@app.get("/api/agent")
async def agent_dashboard():
    """Agent dashboard: tools, MCP servers, skills/add-ons, and status."""
    from .agents.tools import get_tool_schemas
    from .skills import load_skills

    # Built-in agent tools (the agent's "subagents" / capabilities).
    tools = [
        {"name": t["function"]["name"],
         "description": (t["function"].get("description") or "")[:160]}
        for t in get_tool_schemas()
    ]

    # MCP servers + namespaced tools.
    try:
        mcp_status = await mcp_registry.statuses()
        for s in mcp_status:
            s["tools"] = [f"{s['name']}__{t}" for t in s.get("tools", [])]
    except Exception:  # noqa: BLE001
        mcp_status = [{"name": "?  ", "ok": False, "error": "registry error",
                       "tools": []}]

    # Skills: custom registry + bundled example notebooks/scripts as add-ons.
    skills = load_skills()
    bundled = []
    examples_nb = ROOT / "examples" / "notebooks"
    if examples_nb.exists():
        bundled = sorted(f.stem for f in examples_nb.glob("*.ipynb"))
    bundled_scripts = sorted(
        p.name for p in (ROOT / "examples" / "experiments").glob("*.py")
    ) if (ROOT / "examples" / "experiments").exists() else []

    # Status / add-on data.
    total_artifacts = 0
    for rt in runtimes.values():
        try:
            total_artifacts += len(rt.artifacts.list())
        except Exception:  # noqa: BLE001
            pass
    addons = {
        "projects": len(runtimes) or (len(list(PROJECTS_DIR.iterdir())) if PROJECTS_DIR.exists() else 0),
        "experiments": len(load_experiments()),
        "artifacts": total_artifacts,
        "notebooks": len(bundled),
        "scripts": len(bundled_scripts),
    }

    llm = {
        "model": CONFIG["llm"].get("model"),
        "base_url": CONFIG["llm"].get("base_url"),
        "tool_base_url": CONFIG["llm"].get("tool_base_url"),
    }

    return {
        "tools": tools,
        "mcp": mcp_status,
        "skills": skills,
        "bundled": {"notebooks": bundled, "scripts": bundled_scripts},
        "addons": addons,
        "llm": llm,
    }


@app.post("/api/agent/skills")
async def add_agent_skill(body: dict):
    from .skills import add_skill
    try:
        skill = add_skill(body.get("name", ""), body.get("description", ""),
                          body.get("instruction", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"skill": skill}


@app.delete("/api/agent/skills/{skill_id}")
async def delete_agent_skill(skill_id: str):
    from .skills import delete_skill
    return {"deleted": delete_skill(skill_id)}


@app.post("/api/mcp/servers")
async def add_mcp_server(body: dict):
    """Add an MCP server to the config and rebuild the registry."""
    cfg = json.loads(json.dumps(CONFIG))
    servers = cfg.setdefault("mcp", {}).setdefault("servers", [])
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if any(s.get("name") == name for s in servers):
        return JSONResponse({"error": f"server '{name}' already exists"}, status_code=400)
    server = {
        "name": name,
        "transport": body.get("transport", "stdio"),
        "trusted": bool(body.get("trusted", False)),
    }
    if server["transport"] == "stdio":
        server["command"] = body.get("command") or "{python}"
        server["args"] = [a for a in (body.get("args") or "").split(",") if a.strip()]
        server["env"] = {"PYTHONPATH": str(ROOT)}
    else:
        server["url"] = body.get("url") or ""
        server["headers"] = body.get("headers") or {}
    servers.append(server)
    save_config(cfg)
    CONFIG["mcp"]["servers"] = servers
    await rebuild_mcp()
    return {"server": server}


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    cfg = json.loads(json.dumps(CONFIG))
    servers = cfg.setdefault("mcp", {}).setdefault("servers", [])
    out = [s for s in servers if s.get("name") != name]
    if len(out) == len(servers):
        return JSONResponse({"error": "server not found"}, status_code=404)
    cfg["mcp"]["servers"] = out
    save_config(cfg)
    CONFIG["mcp"]["servers"] = out
    await rebuild_mcp()
    return {"deleted": True}


# --------------------------------------------------------- REST: projects ---

@app.get("/api/projects")
async def list_projects():
    out = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                rt = get_runtime(d.name)
                msgs = rt.store.list_messages()
                arts = rt.artifacts.list()
                out.append({
                    "name": d.name,
                    "messages": len(msgs),
                    "artifacts": len(arts),
                    "updated": d.stat().st_mtime if hasattr(d, "stat") else 0,
                })
    return {"projects": out}


@app.post("/api/projects")
async def create_project(body: dict):
    name = (body.get("name") or "").strip().replace("/", "_")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    d = PROJECTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    get_runtime(name)
    return {"name": name}


# -------------------------------------------------- REST: project state ------

@app.get("/api/projects/{name}/state")
async def project_state(name: str):
    rt = get_runtime(name)
    msgs = rt.store.list_messages()
    arts = rt.artifacts.list()
    grants = rt.store.list_grants()
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        env = {}
    try:
        vars_ = await rt.kernels.python.list_variables()
    except Exception:  # noqa: BLE001
        vars_ = {}
    return {"name": name, "messages": msgs, "artifacts": arts, "grants": grants,
            "env": env, "variables": vars_}


@app.get("/api/projects/{name}/workflow")
async def project_workflow(name: str):
    """Latest workflow-progress snapshot (arXiv replication, …).

    The WebSocket pushes `workflow` events live; this endpoint lets any page or
    section load fetch the current state on demand (event-driven self-heal).
    """
    return {"workflow": get_runtime(name).workflow.snapshot()}


@app.get("/api/projects/{name}/workflow/history")
async def project_workflow_history(name: str):
    """Archived workflow runs (persisted in SQLite across restarts)."""
    return {"workflow_runs": get_runtime(name).store.list_workflow_runs()}


@app.get("/api/projects/{name}/artifacts")
async def list_artifacts(name: str):
    return {"artifacts": get_runtime(name).artifacts.list()}


@app.get("/artifacts/{artifact_id}")
async def artifact_file(artifact_id: str):
    for rt in runtimes.values():
        data = rt.artifacts.data(artifact_id)
        if data is not None:
            art = rt.artifacts.get(artifact_id)
            media = {"png": "image/png", "svg": "image/svg+xml",
                     "html": "text/html", "text": "text/plain"}.get(
                art.data_type if art else "text", "application/octet-stream")
            return FileResponse(rt.artifacts.artifacts_dir / Path(art.data_path).name,
                                media_type=media)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/artifacts/{artifact_id}/meta")
async def artifact_meta(artifact_id: str):
    for rt in runtimes.values():
        art = rt.artifacts.get(artifact_id)
        if art is not None:
            return {"artifact": art.to_dict()}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/projects/{name}/artifacts/{artifact_id}")
async def delete_artifact(name: str, artifact_id: str):
    rt = get_runtime(name)
    return {"deleted": rt.artifacts.delete(artifact_id)}


@app.post("/api/projects/{name}/kernel/reset")
async def reset_kernel(name: str):
    rt = get_runtime(name)
    await rt.kernels.reset()
    return {"ok": True}


# ---------------------------------------------------------- notebooks --------

@app.get("/api/projects/{name}/notebooks")
async def list_notebooks(name: str):
    return {"notebooks": get_runtime(name).notebooks.list()}


@app.post("/api/projects/{name}/notebooks")
async def create_notebook(name: str, body: dict):
    rt = get_runtime(name)
    nbname = (body.get("name") or "").strip()
    if not nbname:
        return JSONResponse({"error": "name required"}, status_code=400)
    cells = body.get("cells")
    nb = new_notebook(cells, nbname)
    safe = rt.notebooks._safe(nbname)
    rt.notebooks.save(safe, nb)
    return {"name": safe, "notebook": nb}


@app.get("/api/projects/{name}/notebooks/{nbname}")
async def get_notebook(name: str, nbname: str):
    try:
        nb = get_runtime(name).notebooks.load(nbname)
    except NotebookError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"notebook": nb}


@app.put("/api/projects/{name}/notebooks/{nbname}")
async def save_notebook(name: str, nbname: str, body: dict):
    rt = get_runtime(name)
    cells = body.get("cells")
    if not isinstance(cells, list):
        return JSONResponse({"error": "cells required"}, status_code=400)
    nb = rt.notebooks.load(nbname)
    nb["cells"] = cells
    rt.notebooks.save(nbname, nb)
    return {"notebook": nb}


@app.post("/api/projects/{name}/notebooks/{nbname}/execute")
async def execute_notebook(name: str, nbname: str, body: dict):
    import base64

    rt = get_runtime(name)
    cells = body.get("cells", "all")
    indices = None
    if cells != "all":
        try:
            indices = [int(x) for x in str(cells).split(",") if x.strip()]
        except ValueError:
            return JSONResponse({"error": "cells must be 'all' or comma-separated indices"},
                                status_code=400)

    async def on_artifact(fig_b64: str, source: str):
        env = await rt.kernels.get_env()
        art = Artifact(kind="figure", name="notebook-figure",
                       description="Figure produced by a notebook cell",
                       code=source, env=env, message_id="")
        rt.artifacts.add_artifact(art, data=base64.b64decode(fig_b64), data_type="png")
        return art

    try:
        res = await rt.notebooks.execute(nbname, indices, on_artifact=on_artifact)
    except NotebookError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return res


@app.get("/api/projects/{name}/grants")
async def list_grants(name: str):
    return {"grants": get_runtime(name).store.list_grants()}


@app.delete("/api/projects/{name}/grants/{grant_id}")
async def delete_grant(name: str, grant_id: str):
    rt = get_runtime(name)
    with rt.store._conn:
        cur = rt.store._conn.execute("DELETE FROM grants WHERE id=?", (int(grant_id),))
    return {"deleted": cur.rowcount > 0}


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
                              fresh: bool) -> str:
    """Execute a notebook (fresh seed when requested) and summarize the results."""
    from .experiments import record_experiment
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
                       code=source, env=env, message_id="")
        rt.artifacts.add_artifact(art, data=base64.b64decode(fig_b64), data_type="png")
        collected.append({"name": art.name, "id": art.id})
        if emit:
            try:
                await emit("artifact", {"artifact": art.to_dict()})
            except Exception:  # noqa: BLE001
                pass
        return art

    res = await svc.execute(name, on_artifact=on_artifact, prelude=prelude)
    nb = res["notebook"]

    # Record the run in the Experiments history (kind = notebook), reading any
    # metrics the notebook helper exposed (e.g. clean/robust accuracy).
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
        record_experiment({
            "id": f"nb-{int(time.time())}",
            "kind": "notebook",
            "label": name,
            "seed": seed_used,
            "fresh": bool(fresh),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "artifacts": collected,
        })
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
    return "\n".join(lines)


# Meaningful tags shown on chat messages so experiments are recognisable at a
# glance (rendered as small badges next to the message text).
def message_tags(role: str, text: str) -> list[str]:
    tags: list[str] = []
    low = (text or "").lower()
    if role == "user":
        if match_workflow(text):
            tags.append("privacy workflow")
            if fresh_requested(text):
                tags.append("fresh rerun")
            if compare_requested(text):
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


async def run_privacy_workflow(rt: ProjectRuntime, emit,
                               fresh: bool = False, compare: bool = False) -> str:
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
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args, cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        out, err = b"", b"[timeout] workflow exceeded 600s"
    summary = out.decode(errors="replace")
    if err:
        summary += "\n[stderr]\n" + err.decode(errors="replace")[-2000:]

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
            "The workflow ran **3 stages** on synthetic SWIFT data "
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
    return message[:60_000] if message else "(workflow produced no output)"


# ---------------------------------------------------------- regenerate -------

REGEN_PROMPT = """\
You are modifying Python code in a scientific workbench. Here is the original code
that produced a figure:

```python
{code}
```

The user wants this change: "{instruction}"

Respond with ONLY the complete, modified Python code in a single fenced code block.
Do not explain. Preserve any existing variable names so kernel state stays consistent.
"""


@app.post("/api/projects/{name}/regenerate")
async def regenerate(name: str, body: dict):
    rt = get_runtime(name)
    artifact_id = body.get("artifact_id", "")
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return JSONResponse({"error": "instruction required"}, status_code=400)
    art = rt.artifacts.get(artifact_id)
    if not art:
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    code = art.code
    try:
        resp = await rt.llm.complete(
            [{"role": "system",
              "content": REGEN_PROMPT.format(code=code, instruction=instruction)},
             {"role": "user", "content": "Output the complete modified code now."}],
            temperature=0.1,
        )
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    text = resp.get("content", "")
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    new_code = m.group(1).strip() if m else text.strip()
    if not new_code:
        return JSONResponse({"error": "model returned no code"}, status_code=502)
    env = await rt.kernels.get_env()
    kernel_resp = await rt.kernels.python.run_code(new_code)
    new_art = None
    figs = kernel_resp.get("figures") or []
    if figs:
        import base64

        data = base64.b64decode(figs[0])
        new_art = Artifact(kind="figure", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=data, data_type="png")
    else:
        new_art = Artifact(kind="text", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=(kernel_resp.get("output") or "").encode(),
                                  data_type="text")
    return {"artifact": new_art.to_dict(),
            "output": kernel_resp.get("output", ""),
            "error": kernel_resp.get("error", "")}


# ---------------------------------------------------------- WebSocket ---------

@app.websocket("/ws/projects/{name}")
async def ws_chat(ws: WebSocket, name: str):
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

    broker = ApprovalBroker(emit)
    coordinator = Coordinator(rt.llm, rt.ctx(emit, broker), emit=emit,
                              persist=lambda r, c, m: rt.store.add_message(r, c, m),
                              max_iters=rt.max_iters, mcp=mcp_registry)

    async def handle_turn(text: str):
        async with rt.lock:
            try:
                user_tags = message_tags("user", text)
                mid = rt.store.add_message("user", text, {"tags": user_tags})
                await emit("user_message", {"id": mid, "content": text, "tags": user_tags})
                if match_workflow(text) or compare_requested(text):
                    compare = compare_requested(text)
                    await emit("status", {"message":
                        ("Comparing previous workflow runs…" if compare else
                         "Running the privacy workflow — peer exploitation · "
                         "red team · DP robustness…")})
                    result = await run_privacy_workflow(
                        rt, emit, fresh=fresh_requested(text), compare=compare)
                    amid = rt.store.add_message(
                        "assistant", result,
                        {"tags": message_tags("assistant", result)})
                    await emit("assistant_message", {"id": amid, "content": result,
                                                     "tags": message_tags("assistant", result)})
                    await emit("done", {})
                    return
                nb = match_notebook_run(text)
                if nb:
                    name, fresh = nb
                    await emit("status", {"message": f"Executing notebook {name}"
                                        + (" with a fresh seed…" if fresh else "…")})
                    result = await run_notebook_intent(rt, emit, name, fresh)
                    tags = ["notebook", "fresh rerun" if fresh else "run"]
                    amid = rt.store.add_message("assistant", result, {"tags": tags})
                    await emit("assistant_message", {"id": amid, "content": result,
                                                     "tags": tags})
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
                llm_msgs = rt.build_llm_messages()
                result = await coordinator.run_turn(llm_msgs)
                amid = rt.store.add_message(
                    "assistant", result.get("text", ""),
                    {"tags": message_tags("assistant", result.get("text", ""))})
                await emit("assistant_message", {"id": amid,
                                                 "content": result.get("text", ""),
                                                 "tags": message_tags("assistant",
                                                                      result.get("text", ""))})
                if rt.reviewer_enabled:
                    await emit("status", {"message": "Reviewing the turn…"})
                    await emit("review_start", {})
                    try:
                        findings = await Reviewer(rt.llm, rt.store).review()
                        await emit("review", {"findings": findings})
                    except Exception:  # noqa: BLE001
                        await emit("review", {"findings": []})
                await emit("status", {"message": ""})
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
                else:
                    await incoming.put(msg)
        except WebSocketDisconnect:
            pass

    recv_task = asyncio.create_task(receive_loop())
    try:
        while True:
            msg = await incoming.get()
            if msg.get("type") == "chat":
                text = (msg.get("content") or "").strip()
                if text:
                    await handle_turn(text)
    except WebSocketDisconnect:
        pass
    finally:
        recv_task.cancel()
        rt.workflow.unsubscribe(emit)


# ------------------------------------------------------------ static files ---

class NoCacheStaticFiles(StaticFiles):
    """Serve frontend assets with no-cache headers so UI changes always apply."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
