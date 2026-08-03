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
import shutil
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, File, UploadFile
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
from .store import ProjectStore, close_project_db
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

        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        summary = self.store.get_setting("context_summary", "")
        rows = self.store.list_messages()
        msgs: list[dict] = []
        for r in rows:
            if r["id"] <= cutoff:
                continue
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
        if summary:
            system += ("\n\n## Summary of earlier conversation (compacted)\n"
                       "The following is a persistent summary of turns that were "
                       "compacted out of the live context:\n" + summary)
        msgs.insert(0, {"role": "system", "content": system})
        return sanitize_messages(msgs)

    # Number of fresh messages kept before older turns get compacted away.
    COMPACTION_LIMIT = 60
    # Always keep this many of the most recent messages fresh in the context.
    COMPACTION_KEEP = 24

    async def maybe_compact(self):
        """Summarize older turns into a persistent summary once the conversation
        grows past COMPACTION_LIMIT fresh messages.

        The summary + the message-id cutoff are stored in settings, so the
        compaction survives restarts and is only performed once per block.
        """
        rows = self.store.list_messages()
        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        fresh = [r for r in rows if r["id"] > cutoff]
        if len(fresh) <= self.COMPACTION_LIMIT:
            return
        block = fresh[:-self.COMPACTION_KEEP]
        if not block:
            return
        prev = self.store.get_setting("context_summary", "")
        summary = await _summarize_conversation(self.llm, prev, block)
        new_cutoff = block[-1]["id"]
        self.store.set_setting("context_summary", summary)
        self.store.set_setting("context_cutoff", str(new_cutoff))

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


def _valid_project_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    """Delete a project (session, artifacts, notebook files) and drop its runtime."""
    if not _valid_project_name(name):
        raise HTTPException(status_code=400, detail="invalid project name")
    d = PROJECTS_DIR / name
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="project not found")
    rt = runtimes.pop(name, None)
    if rt is not None:
        try:
            await rt.stop()
        except Exception:  # noqa: BLE001
            pass
    close_project_db(d)
    shutil.rmtree(d, ignore_errors=True)
    return {"deleted": name}


@app.post("/api/projects/{name}/fork")
async def fork_project(name: str, body: dict):
    """Fork a project as a new session: snapshot of messages, runs, artifacts,
    notebooks and files."""
    src = PROJECTS_DIR / name
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="project not found")
    new_name = (body.get("name") or "").strip().replace("/", "_")
    if not new_name:
        new_name = f"{name}-fork"
    if not _valid_project_name(new_name):
        raise HTTPException(status_code=400, detail="invalid project name")
    dst = PROJECTS_DIR / new_name
    if dst.exists():
        raise HTTPException(status_code=409, detail="project already exists")
    shutil.copytree(src, dst)
    get_runtime(new_name)
    return {"name": new_name}


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


@app.get("/api/projects/{name}/runs")
async def project_runs(name: str, limit: int = 50):
    """Every agent turn recorded as a run (traceability)."""
    return {"runs": get_runtime(name).store.list_runs(limit)}


@app.get("/api/projects/{name}/runs/{rid}")
async def project_run(name: str, rid: int):
    run = get_runtime(name).store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


async def build_run_report(rt: ProjectRuntime, run: dict) -> str:
    """Assemble a lab-notebook markdown report for an agent run.

    Deterministic sections (prompt, metrics, tool trace, artifacts, review) are
    always present; an LLM executive summary is prepended when available.
    """
    lines = [
        f"# Run #{run['id']} — report",
        "",
        f"- **Prompt**: {run.get('prompt') or '—'}",
        f"- **Status**: {run.get('status')}",
        f"- **Started**: {_fmt_ts(run.get('started_at'))}",
        f"- **Finished**: {_fmt_ts(run.get('finished_at'))}",
    ]
    metrics = run.get("metrics") or {}
    if metrics:
        lines += ["", "## Metrics", "",
                  "| metric | value |", "|---|---|"]
        for k in sorted(metrics):
            lines.append(f"| {k} | {metrics[k]:.6g} |")
    seq = run.get("tool_sequence") or []
    if seq:
        lines += ["", "## Tool trace", ""]
        for t in seq:
            mark = "ok" if t.get("ok") else "FAILED"
            lines.append(f"- `{t.get('name')}` ({mark}) — args: `{t.get('args') or ''}`")
            lines.append(f"  - result: `{(t.get('result') or '').strip() or '(empty)'}`")
    arts = run.get("artifact_ids") or []
    if arts:
        lines += ["", "## Artifacts", ""]
        for aid in arts:
            art = rt.artifacts.get(aid)
            if art:
                lines.append(f"- [{aid}]({art.url or f'/artifacts/{aid}'}) — {art.name} ({art.kind})")
            else:
                lines.append(f"- `{aid}` (not found)")
    review = run.get("review") or {}
    findings = review.get("findings") or []
    suggestions = review.get("suggestions") or []
    if findings or suggestions:
        lines += ["", "## Review", ""]
        for f in findings:
            lines.append(f"- **{f.get('severity')}**: {f.get('message')}")
        if suggestions:
            lines += ["", "### Suggested next steps", ""]
            for s in suggestions:
                lines.append(f"- {s}")
    base = "\n".join(lines)

    # LLM-assisted executive summary (best effort).
    try:
        summary = await _summarize_run(rt, run, base)
    except Exception:  # noqa: BLE001
        summary = ""
    if summary:
        base = f"## Executive summary\n\n{summary}\n\n" + base
    return base


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return str(ts)


async def _summarize_run(rt: ProjectRuntime, run: dict, report: str) -> str:
    prompt = (
        "You are writing the executive summary of a lab-notebook report. Given the "
        "run facts below, write 3-5 concise sentences: what was tried, the key "
        "metrics, and whether the result is good or needs improvement. No markdown "
        "headings, just plain sentences.\n\n"
        f"Prompt: {run.get('prompt', '')}\n\nReport:\n{report[:4000]}")
    resp = await rt.llm.complete([{"role": "user", "content": prompt}],
                                 temperature=0.2, tools=None)
    text = (resp.get("content") or "").strip()
    return text[:2000] if text else ""


def _conversation_digest(rows: list[dict], limit: int = 120) -> str:
    """Deterministic fallback summary: one compacted line per message."""
    out: list[str] = []
    for r in rows:
        role = r["role"]
        content = " ".join((r["content"] or "").split())
        if role == "user":
            out.append(f"user: {content[:limit]}")
        elif role == "assistant":
            out.append(f"assistant: {content[:limit]}")
        elif role == "tool":
            meta = r.get("meta") or {}
            out.append(f"tool({meta.get('name', 'tool')}): {content[:100]}")
    return "\n".join(out[:300])


async def _summarize_conversation(llm, prev: str, rows: list[dict]) -> str:
    """Produce (or extend) a persistent summary of compacted conversation turns.

    Best-effort: an LLM summary when available, otherwise a deterministic
    digest of the message contents.
    """
    transcript = _conversation_digest(rows)
    if prev:
        transcript = f"Existing summary:\n{prev}\n\nNew turns to fold in:\n{transcript}"
    prompt = (
        "You maintain a persistent summary of an agentic research conversation. "
        "Read the turns below and produce a compact summary capturing: the user's "
        "research goal and constraints, what experiments/analyses were run, key "
        "results and metric values, and any open questions or next steps. Plain "
        "sentences or short bullets, no markdown headings, keep it under 400 words.\n\n"
        + transcript[:8000])
    try:
        resp = await llm.complete([{"role": "user", "content": prompt}],
                                  temperature=0.2, tools=None)
        text = (resp.get("content") or "").strip()
        if text:
            return text[:4000]
    except Exception:  # noqa: BLE001
        pass
    return _conversation_digest(rows, limit=160)


@app.post("/api/projects/{name}/runs/{rid}/report")
async def project_run_report(name: str, rid: int):
    """Generate a lab-notebook markdown report for a run and save it as an artifact."""
    from .artifacts.store import Artifact
    rt = get_runtime(name)
    run = rt.store.get_run(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    report = await build_run_report(rt, run)
    env = {}
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        pass
    art = Artifact(kind="text", name=f"run-{rid}-report",
                   description=f"Auto-generated lab-notebook report for run #{rid}",
                   code="# auto-generated report", env=env)
    rt.artifacts.add_artifact(art, data=report.encode(), data_type="text")
    mid = rt.store.add_message("assistant", report,
                               {"tags": ["report", f"run #{rid}"]})
    return {"report": report, "artifact_id": art.id, "message_id": mid}


@app.get("/api/projects/{name}/goals")
async def project_goals(name: str):
    return {"goals": get_runtime(name).store.list_goals()}


@app.post("/api/projects/{name}/goals")
async def project_goals_add(name: str, body: dict):
    metric = str(body.get("metric", "")).strip()
    if not metric:
        raise HTTPException(status_code=400, detail="metric is required")
    try:
        target = float(body.get("target", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target must be numeric")
    rt = get_runtime(name)
    rt.store.add_goal(metric, target, bool(body.get("higher_better", True)),
                      str(body.get("label", "")))
    return {"goals": rt.store.list_goals()}


@app.delete("/api/projects/{name}/goals/{metric}")
async def project_goals_delete(name: str, metric: str):
    rt = get_runtime(name)
    deleted = rt.store.delete_goal(metric)
    if not deleted:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"goals": rt.store.list_goals()}


def goal_notices(rt: ProjectRuntime, run: dict) -> list[str]:
    """Human-readable goal-progress / new-best notices for a freshly recorded run."""
    goals = rt.store.list_goals()
    if not goals:
        return []
    metrics = run.get("metrics") or {}
    if not metrics:
        return []
    runs = rt.store.list_runs()
    notices = []
    for g in goals:
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


@app.get("/api/projects/{name}/compare")
async def project_compare(name: str, run_a: str = "", run_b: str = ""):
    """Metric delta between two runs (agent runs or experiment-history records)."""
    from .experiments import compare_runs, load_experiments
    if not run_a or not run_b:
        raise HTTPException(status_code=400, detail="run_a and run_b are required")
    rt = get_runtime(name)

    def resolve(ref: str):
        # Agent run ids are integers; experiment-history ids are strings.
        if ref.isdigit():
            rec = rt.store.get_run(int(ref))
            if rec is not None:
                return rec
        for exp in load_experiments():
            if str(exp.get("id")) == ref:
                return exp
        return None

    ra, rb = resolve(run_a), resolve(run_b)
    if ra is None or rb is None:
        raise HTTPException(status_code=404,
                            detail=f"could not resolve run ids: {run_a!r}, {run_b!r}")
    return {"comparison": compare_runs(ra, rb)}


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
    # Runtime not loaded (e.g. after restart): fall back to scanning the
    # projects' artifacts directories so existing files keep working.
    path = _find_artifact_file_on_disk(artifact_id)
    if path is not None:
        media = {"png": "image/png", "svg": "image/svg+xml",
                 "html": "text/html", "text": "text/plain"}.get(
            path.suffix.lstrip("."), "application/octet-stream")
        return FileResponse(path, media_type=media)
    return JSONResponse({"error": "not found"}, status_code=404)


def _find_artifact_file_on_disk(artifact_id: str) -> Path | None:
    if not PROJECTS_DIR.exists():
        return None
    for proj in PROJECTS_DIR.iterdir():
        art_dir = proj / "artifacts"
        if not art_dir.is_dir():
            continue
        for ext in (".png", ".svg", ".html", ".txt", ".bin"):
            p = art_dir / f"{artifact_id}{ext}"
            if p.exists():
                return p
    return None


def _find_artifact_meta_on_disk(artifact_id: str) -> dict | None:
    """Read an artifact row from the project DB without loading the runtime."""
    if not PROJECTS_DIR.exists():
        return None
    for proj in PROJECTS_DIR.iterdir():
        db = proj / "workbench.db"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            conn.close()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        return Artifact(
            id=row["id"], kind=row["kind"], name=row["name"],
            description=row["description"], code=row["code"],
            env=json.loads(row["env"] or "{}"), message_id=row["message_id"],
            run_id=row["run_id"], created_at=row["created_at"],
            data_path=row["data_path"], data_type=row["data_type"],
            size=row["size"]).to_dict()
    return None


@app.get("/api/artifacts/{artifact_id}/meta")
async def artifact_meta(artifact_id: str):
    for rt in runtimes.values():
        art = rt.artifacts.get(artifact_id)
        if art is not None:
            return {"artifact": art.to_dict()}
    meta = _find_artifact_meta_on_disk(artifact_id)
    if meta is not None:
        return {"artifact": meta}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/projects/{name}/artifacts/{artifact_id}")
async def delete_artifact(name: str, artifact_id: str):
    rt = get_runtime(name)
    return {"deleted": rt.artifacts.delete(artifact_id)}


# ------------------------------------------------------------ project files --

_IGNORED_FILES = {"workbench.db", "workbench.db-wal", "workbench.db-shm",
                  "config.json"}


def _safe_filename(name: str) -> str:
    base = Path(name).name
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    return base


def _list_project_files(name: str) -> list[dict]:
    rt = get_runtime(name)
    out = []
    for p in sorted(rt.dir.iterdir()):
        if not p.is_file() or p.name in _IGNORED_FILES:
            continue
        out.append({
            "name": p.name,
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
            "url": f"/api/projects/{name}/files/{p.name}",
        })
    return out


@app.get("/api/projects/{name}/files")
async def project_files(name: str):
    return {"files": _list_project_files(name)}


@app.post("/api/projects/{name}/files")
async def project_files_upload(name: str, upload: UploadFile = File(...)):
    rt = get_runtime(name)
    filename = _safe_filename(upload.filename or "")
    dest = rt.dir / filename
    data = await upload.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (limit 50 MB)")
    dest.write_bytes(data)
    return {"files": _list_project_files(name)}


@app.get("/api/projects/{name}/files/{filename}")
async def project_file_download(name: str, filename: str):
    rt = get_runtime(name)
    dest = rt.dir / _safe_filename(filename)
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media = "application/octet-stream"
    return FileResponse(dest, media_type=media, filename=dest.name)


@app.delete("/api/projects/{name}/files/{filename}")
async def project_file_delete(name: str, filename: str):
    rt = get_runtime(name)
    dest = rt.dir / _safe_filename(filename)
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    dest.unlink()
    return {"files": _list_project_files(name)}


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


@app.get("/api/projects/{name}/approvals")
async def list_approvals(name: str, limit: int = 50):
    """Audit trail of approval decisions (allow / deny / temporary / timeout)."""
    return {"approvals": get_runtime(name).store.list_approvals(limit)}


# -------------------------------------------------- knowledge graphs ----------

@app.get("/api/projects/{name}/graphs")
async def list_knowledge_graphs(name: str):
    """Auto-exported per-paper arXiv knowledge graphs persisted for this project."""
    rt = get_runtime(name)
    gdir = rt.dir / "knowledge_graphs"
    out = []
    if gdir.is_dir():
        for p in sorted(gdir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({
                "name": p.name,
                "size": p.stat().st_size,
                "paper_id": data.get("paper_id"),
                "stats": data.get("stats", {}),
                "modified": p.stat().st_mtime,
                "url": f"/api/projects/{name}/graphs/{p.name}",
            })
    return {"graphs": out}


@app.get("/api/projects/{name}/graphs/{filename}")
async def get_knowledge_graph(name: str, filename: str):
    rt = get_runtime(name)
    safe = Path(filename).name
    if safe.endswith(".json"):
        safe = safe[:-5]
    p = (rt.dir / "knowledge_graphs" / f"{safe}.json")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="graph not found")
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        raise HTTPException(status_code=500, detail="graph unreadable")
    return {"name": p.name, "graph": data}


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
                              fresh: bool, message_id: str = "") -> str:
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
                       code=source, env=env, message_id=message_id)
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

    broker = ApprovalBroker(emit, store=rt.store)
    coordinator = Coordinator(rt.llm, rt.ctx(emit, broker), emit=emit,
                              persist=lambda r, c, m: rt.store.add_message(r, c, m),
                              record=lambda r: rt.store.add_run(
                                  prompt=r.get("prompt", ""),
                                  reply=r.get("reply", ""),
                                  status=r.get("status", "done"),
                                  started_at=r.get("started_at", 0.0),
                                  finished_at=r.get("finished_at", time.time()),
                                  tool_sequence=r.get("tool_sequence"),
                                  artifact_ids=r.get("artifact_ids"),
                                  metrics=r.get("metrics"),
                                  review=r.get("review")),
                              max_iters=rt.max_iters, mcp=mcp_registry)

    async def handle_turn(text: str, intent: str = ""):
        async with rt.lock:
            try:
                user_tags = message_tags("user", text)
                # Explicit intents (from the UI quick-action buttons) route
                # deterministically instead of relying on keyword matching.
                workflow_mode = compare_mode = fresh_mode = False
                if intent == "privacy_workflow":
                    workflow_mode = True
                    user_tags = ["privacy workflow"]
                elif intent == "privacy_workflow_fresh":
                    workflow_mode = fresh_mode = True
                    user_tags = ["privacy workflow", "fresh rerun"]
                elif intent == "privacy_compare":
                    workflow_mode = compare_mode = True
                    user_tags = ["privacy workflow", "compare runs"]
                else:
                    workflow_mode = bool(match_workflow(text) or compare_requested(text))
                    compare_mode = compare_requested(text)
                    fresh_mode = fresh_requested(text)
                mid = rt.store.add_message("user", text, {"tags": user_tags})
                coordinator.ctx.message_id = str(mid)
                await emit("user_message", {"id": mid, "content": text, "tags": user_tags})
                if workflow_mode:
                    await emit("status", {"message":
                        ("Comparing previous workflow runs…" if compare_mode else
                         "Running the privacy workflow — peer exploitation · "
                         "red team · DP robustness…")})
                    result = await run_privacy_workflow(
                        rt, emit, fresh=fresh_mode, compare=compare_mode)
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
                    result = await run_notebook_intent(rt, emit, name, fresh,
                                                        message_id=str(mid))
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
                await rt.maybe_compact()
                llm_msgs = rt.build_llm_messages()
                result = await coordinator.run_turn(llm_msgs)
                amid = rt.store.add_message(
                    "assistant", result.get("text", ""),
                    {"tags": message_tags("assistant", result.get("text", ""))})
                await emit("assistant_message", {"id": amid,
                                                 "content": result.get("text", ""),
                                                 "tags": message_tags("assistant",
                                                                      result.get("text", ""))})
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
            broker.reject_all()  # resolve pending approvals so the agent can't hang
            pass

    recv_task = asyncio.create_task(receive_loop())
    try:
        while True:
            msg = await incoming.get()
            if msg.get("type") == "chat":
                text = (msg.get("content") or "").strip()
                if text:
                    await handle_turn(text, intent=msg.get("intent") or "")
    except WebSocketDisconnect:
        pass
    finally:
        recv_task.cancel()
        broker.reject_all()  # don't let the agent hang on a vanished client
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
