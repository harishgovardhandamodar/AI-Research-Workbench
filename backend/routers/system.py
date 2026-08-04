"""System-level REST routes: config, health, editor, MCP, models, global
experiments history, and the agent dashboard / skills."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..experiments import build_graph, load_experiments
from ..llm import LLMError
from ..paths import PROJECTS_DIR, ROOT
from ..state import (CONFIG, get_llm, make_llm, mcp_registry, rebuild_mcp,
                     reset_llm_cache, runtimes, save_config)
from .. import editor as editor_cfg

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def get_config():
    return {"config": CONFIG}


@router.post("/api/config")
async def set_config(body: dict):
    cfg = body.get("config", {})
    if "llm" in cfg:
        CONFIG["llm"].update(cfg["llm"])
    if "agent" in cfg:
        CONFIG["agent"].update(cfg["agent"])
    if "kaggle" in cfg:
        CONFIG["kaggle"].update({k: v for k, v in cfg["kaggle"].items()
                                 if k in ("username", "key")})
    if "management" in cfg:
        CONFIG["management"].update({k: v for k, v in cfg["management"].items()
                                     if k in ("repo_dir", "github_repo",
                                              "auto_commit", "auto_push")})
    if "mcp" in cfg and "servers" in cfg["mcp"]:
        CONFIG["mcp"]["servers"] = cfg["mcp"]["servers"]
        await rebuild_mcp()
    save_config(CONFIG)
    reset_llm_cache()
    for rt in runtimes.values():
        rt.llm = make_llm()
        rt.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        rt.max_iters = CONFIG["agent"].get("max_iters", 8)
    return {"config": CONFIG}


@router.get("/api/management/repos")
async def management_repos():
    """Candidate experiment management repos: sibling git worktrees next to the
    workbench repo (e.g. the personal-experiments repo)."""
    from ..experiment_repo import sibling_git_repos

    return {"repos": sibling_git_repos()}


@router.get("/api/management/status")
async def management_status():
    """Current experiment-management wiring: local repo, GitHub repo, origin."""
    from ..experiment_repo import current_remote, management_repo_dir

    repo = management_repo_dir()
    return {
        "repo_dir": str(repo) if repo else "",
        "github_repo": (CONFIG.get("management") or {}).get("github_repo", ""),
        "remote": current_remote(repo) if repo is not None else None,
    }


@router.post("/api/management/link")
async def management_link(body: dict):
    """Link the experiment management repo to a GitHub repo (owner/repo or URL):
    saves it and points the local repo's `origin` at it."""
    from ..experiment_repo import (ensure_remote, github_remote_url,
                                   management_repo_dir)

    gh = (body.get("github_repo") or "").strip()
    CONFIG["management"]["github_repo"] = gh
    save_config(CONFIG)
    repo = management_repo_dir()
    if repo is None:
        return {"ok": False, "remote": None,
                "message": "set the management repo path (Settings) first"}
    ok, msg = ensure_remote(repo)
    return {"ok": ok, "message": msg, "remote": github_remote_url()}


@router.get("/api/editor")
async def editor_status(request: Request):
    """In-browser editor (code-server) info + a lightweight reachability probe."""
    import urllib.request

    info = editor_cfg.editor_config()
    info["reachable"] = False
    # When running on a remote host, "http://127.0.0.1:8787" is unreachable from
    # the user's browser. If the configured URL is the default loopback one,
    # derive it from the host the user is browsing from (same host, port 8787).
    if info.get("url") == "http://127.0.0.1:8787":
        host = request.url.hostname or "127.0.0.1"
        info["url"] = f"http://{host}:8787" if host not in ("127.0.0.1", "localhost") \
            else "http://127.0.0.1:8787"
    if info.get("enabled"):
        try:
            await asyncio.to_thread(
                urllib.request.urlopen, editor_cfg.editor_probe_url(), timeout=3
            )
            info["reachable"] = True
        except Exception:  # noqa: BLE001
            info["reachable"] = False
    return {"editor": info}


@router.get("/api/mcp")
async def mcp_status():
    statuses = await mcp_registry.statuses()
    return {"servers": statuses, "installed": mcp_registry._available}


@router.get("/api/models")
async def list_models():
    try:
        return {"models": await get_llm().list_models()}
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@router.get("/api/experiments")
async def get_experiments():
    """Legacy global experiment history (privacy_runs.json).

    Deprecated: the Experiments UI reads per-project history from
    /api/projects/{name}/experiments/history instead.
    """
    return {"experiments": load_experiments()}


@router.get("/api/experiments/graph")
async def get_experiments_graph():
    """Legacy global graph (privacy_runs.json).

    Deprecated: use /api/projects/{name}/experiments/graph for project-scoped,
    SQLite-backed runs.
    """
    return build_graph(load_experiments())


@router.get("/api/agent")
async def agent_dashboard():
    """Agent dashboard: tools, MCP servers, skills/add-ons, and status."""
    from ..agents.tools import get_tool_schemas
    from ..skills import load_skills

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
    total_runs = 0
    for rt in runtimes.values():
        try:
            total_artifacts += len(rt.artifacts.list())
            total_runs += rt.store.count_runs()
        except Exception:  # noqa: BLE001
            pass
    addons = {
        "projects": len(runtimes) or (len(list(PROJECTS_DIR.iterdir())) if PROJECTS_DIR.exists() else 0),
        "experiments": total_runs,
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


@router.post("/api/agent/skills")
async def add_agent_skill(body: dict):
    from ..skills import add_skill
    try:
        skill = add_skill(body.get("name", ""), body.get("description", ""),
                          body.get("instruction", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"skill": skill}


@router.delete("/api/agent/skills/{skill_id}")
async def delete_agent_skill(skill_id: str):
    from ..skills import delete_skill
    return {"deleted": delete_skill(skill_id)}


@router.post("/api/mcp/servers")
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


@router.delete("/api/mcp/servers/{name}")
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
