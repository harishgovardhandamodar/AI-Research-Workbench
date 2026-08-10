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
from ..state import (CONFIG, get_llm, get_runtime, make_llm, mcp_registry,
                     rebuild_mcp, reset_llm_cache, runtimes, save_config)
from .. import editor as editor_cfg

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True}


_MCP_MASK = "***REDACTED***"
_SENSITIVE_HINTS = ("token", "key", "secret", "password", "passwd", "auth",
                    "bearer", "credential", "cookie")


def _is_sensitive_key(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SENSITIVE_HINTS)


def _redact_config(cfg: dict) -> dict:
    """Deep-copy config with any key whose name hints at a secret masked."""
    def walk(node):
        if isinstance(node, dict):
            return {k: (_MCP_MASK if _is_sensitive_key(k) else walk(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(cfg)


def _merge_mcp_server(orig: dict, new: dict) -> dict:
    """Preserve real secrets (env/headers values) that came back as the mask so
    saving a redacted config never clobbers them."""
    for key in ("env", "headers"):
        old, cur = orig.get(key), new.get(key)
        if isinstance(old, dict) and isinstance(cur, dict):
            merged = dict(old)
            for k, v in cur.items():
                if isinstance(v, str) and v == _MCP_MASK and k in merged:
                    continue  # keep the live secret
                merged[k] = v
            new[key] = merged
    return new


@router.get("/api/config")
async def get_config():
    """Return the config with secrets (MCP headers/env tokens, kaggle keys)
    masked so they never reach the browser."""
    return {"config": _redact_config(CONFIG)}


@router.post("/api/config")
async def set_config(body: dict):
    cfg = body.get("config", {})
    if "llm" in cfg:
        CONFIG["llm"].update(cfg["llm"])
    if "agent" in cfg:
        CONFIG["agent"].update(cfg["agent"])
    if "kaggle" in cfg:
        new_k = {k: v for k, v in cfg["kaggle"].items()
                 if k in ("username", "key")}
        # a masked key value means the frontend never had the real secret.
        if new_k.get("key") == _MCP_MASK:
            new_k.pop("key", None)
        CONFIG["kaggle"].update(new_k)
    if "management" in cfg:
        CONFIG["management"].update({k: v for k, v in cfg["management"].items()
                                     if k in ("repo_dir", "github_repo",
                                              "auto_commit", "auto_push")})
    if "mcp" in cfg and "servers" in cfg["mcp"]:
        # Merge so redacted env/header values don't clobber live secrets.
        old_by_name = {s.get("name"): s for s in CONFIG["mcp"]["servers"]}
        merged_servers = [
            _merge_mcp_server(old_by_name.get(s.get("name"), {}), s)
            for s in cfg["mcp"]["servers"]
        ]
        CONFIG["mcp"]["servers"] = merged_servers
        await rebuild_mcp()
    save_config(CONFIG)
    reset_llm_cache()
    for rt in runtimes.values():
        rt.llm = make_llm()
        rt.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        rt.max_iters = CONFIG["agent"].get("max_iters", 20)
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


def _save_management_activity(rt, action: str, result: dict):
    """Persist the last commit/push result so the chat window can show it again
    after a page refresh."""
    try:
        rt.store.set_setting("management_last_activity", json.dumps({
            "action": action,
            "commit": result.get("commit"),
            "commit_full": result.get("commit_full"),
            "committed_at": result.get("committed_at"),
            "pushed_at": result.get("pushed_at"),
            "commit_url": result.get("commit_url"),
            "message": result.get("message"),
        }))
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/projects/{name}/management/commit")
async def project_management_commit(name: str, body: dict):
    """Commit this project's experiment artifacts to the management repo."""
    from .. import experiment_repo

    rt = get_runtime(name)
    result = await experiment_repo.commit_project_async(
        rt, (body.get("message") or "").strip() or None)
    if result.get("ok"):
        _save_management_activity(rt, "commit", result)
    return result


@router.post("/api/projects/{name}/management/push")
async def project_management_push(name: str):
    """Push the management repo's current branch to its GitHub remote."""
    from .. import experiment_repo

    result = await asyncio.to_thread(experiment_repo.push)
    if result.get("ok"):
        rt = get_runtime(name)
        _save_management_activity(rt, "push", result)
    return result


@router.post("/api/projects/{name}/management/commit-and-push")
async def project_management_commit_and_push(name: str, body: dict):
    """Commit this project's experiment artifacts and push them to GitHub."""
    from .. import experiment_repo

    rt = get_runtime(name)
    result = await experiment_repo.commit_project_async(
        rt, (body.get("message") or "").strip() or None)
    if not result.get("ok"):
        return result
    pushed = await asyncio.to_thread(experiment_repo.push)
    _save_management_activity(rt, "push", {**result, **pushed})
    return {**result, "pushed": pushed}


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


@router.post("/api/mcp/refresh")
async def mcp_refresh():
    """Clear the status cache so the next GET /api/mcp re-probes the servers."""
    mcp_registry.clear_status_cache()
    return {"ok": True}


@router.post("/api/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(name: str, body: dict):
    """Enable/disable an MCP server (persisted). Disabled servers are neither
    probed for status nor offered to the agent."""
    cfg = json.loads(json.dumps(CONFIG))
    servers = cfg.setdefault("mcp", {}).setdefault("servers", [])
    server = next((s for s in servers if s.get("name") == name), None)
    if server is None:
        return JSONResponse({"error": "server not found"}, status_code=404)
    enabled = bool(body.get("enabled", True))
    server["enabled"] = enabled
    save_config(cfg)
    CONFIG["mcp"]["servers"] = servers
    await rebuild_mcp()
    return {"ok": True, "name": name, "enabled": enabled}


@router.patch("/api/mcp/servers/{name}")
async def edit_mcp_server(name: str, body: dict):
    """Edit an MCP server's config (transport/command/args/url/headers/trusted/
    enabled) without removing it — keeps its name (and any grants keyed on it)."""
    cfg = json.loads(json.dumps(CONFIG))
    servers = cfg.setdefault("mcp", {}).setdefault("servers", [])
    server = next((s for s in servers if s.get("name") == name), None)
    if server is None:
        return JSONResponse({"error": "server not found"}, status_code=404)
    patchable = ("transport", "command", "args", "env", "trusted", "enabled",
                 "url", "headers")
    for key in patchable:
        if key in body:
            server[key] = body[key]
    save_config(cfg)
    CONFIG["mcp"]["servers"] = servers
    await rebuild_mcp()
    return {"ok": True, "server": server}


def _tool_experiment(rt, server: str, tool: str) -> int | None:
    """Find-or-create an Experiments-tab experiment for a tool call."""
    try:
        name = f"🧪 {server}__{tool}"
        for e in rt.store.list_experiments():
            if (e.get("name") or "").startswith(name):
                return e["id"]
        return rt.store.create_experiment(
            name=name,
            hypothesis=f"Output of MCP tool {server}__{tool} on {rt.name}",
            goal_metric="", higher_better=True)
    except Exception:  # noqa: BLE001
        return None


def _parse_flat_metrics(text: str) -> dict | None:
    """Best-effort flat numeric metrics from a JSON tool reply."""
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    out = {}
    for k, v in parsed.items():
        if isinstance(v, bool):
            out[k] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            out[k] = round(float(v), 6)
    return out or None


@router.post("/api/mcp/tools/{server}/{tool}")
async def call_tool(server: str, tool: str, body: dict):
    """Invoke an MCP tool directly from the UI (read-only / granted tools only;
    writable tools without a grant return 403 with the permission key). A real
    invocation is recorded as a run (kind=mcp_tool) for timeline traceability.
    When ``experiment`` is truthy the call is also attached to (or creates) an
    Experiments-tab experiment with any flat numeric metrics parsed from the
    reply."""
    import time as _time
    from ..mcp import call_mcp_tool
    args = body.get("args") or {}
    project = (body.get("project") or "").strip()
    track = bool(body.get("experiment"))
    rt = get_runtime(project) if project else None
    permissions = rt.permissions if rt else None
    started = _time.time()
    text, is_err = await call_mcp_tool(
        mcp_registry, server, tool, args, permissions=permissions)
    if is_err:
        denied = text.startswith("[denied]")
        if rt and not denied:
            try:
                rt.store.add_run(
                    prompt=json.dumps(args)[:500] or f"{server}__{tool}",
                    reply=text[:4000], status="error",
                    started_at=started, finished_at=_time.time(),
                    kind="mcp_tool", label=f"{server}__{tool}", model="MCP")
            except Exception:  # noqa: BLE001
                pass
        status_code = 403 if denied else 502
        return JSONResponse({"ok": False, "text": text,
                             "permission_key": f"{server}__{tool}"},
                            status_code=status_code)
    run_id = None
    experiment_id = None
    if rt:
        metrics = _parse_flat_metrics(text) if track else None
        if track:
            experiment_id = _tool_experiment(rt, server, tool)
        try:
            run_id = rt.store.add_run(
                prompt=json.dumps(args)[:500] or f"{server}__{tool}",
                reply=text[:4000], status="done",
                started_at=started, finished_at=_time.time(),
                metrics=metrics or None, kind="mcp_tool",
                label=f"{server}__{tool}", model="MCP",
                experiment_id=experiment_id)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "text": text, "tool": f"{server}__{tool}",
            "run_id": run_id, "experiment_id": experiment_id}


@router.post("/api/projects/{name}/mcp/artifacts")
async def save_mcp_result_artifact(name: str, body: dict):
    """Persist a direct MCP tool call's result as a project artifact."""
    from ..artifacts.store import Artifact
    rt = get_runtime(name)
    text = body.get("text") or ""
    title = (body.get("name") or "mcp-tool-result").strip() or "mcp-tool-result"
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    art = Artifact(kind="data", name=title,
                   description="Saved MCP tool call result",
                   code="", env={}, message_id="", run_id="", data_type="text")
    try:
        rt.artifacts.add_artifact(art, data=text.encode(), data_type="text")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True, "artifact_id": art.id, "name": title}


@router.get("/api/projects/{name}/mcp/grants")
async def mcp_tool_grants(name: str):
    """Per-project MCP tool permission grants (kind=mcp_tool)."""
    rt = get_runtime(name)
    grants = {g["pattern"]: g["decision"]
              for g in rt.store.list_grants() if g["kind"] == "mcp_tool"}
    return {"grants": grants}


@router.post("/api/projects/{name}/mcp/grants")
async def set_mcp_tool_grant(name: str, body: dict):
    """Grant/revoke an MCP tool permission: decision in {allow, deny, ask}."""
    rt = get_runtime(name)
    key = (body.get("key") or "").strip()
    decision = (body.get("decision") or "").strip().lower()
    if not key or decision not in ("allow", "deny", "ask"):
        return JSONResponse({"error": "key + decision (allow|deny|ask) required"},
                            status_code=400)
    rt.permissions.record("mcp_tool", key, decision)
    grants = {g["pattern"]: g["decision"]
              for g in rt.store.list_grants() if g["kind"] == "mcp_tool"}
    return {"ok": True, "grants": grants}


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


@router.get("/api/system/stats")
async def system_stats():
    """Live server resource usage (CPU / memory / GPU / processes) for the
    faded dgxtop-style HUD. Cached server-side for a few seconds."""
    from .. import system_stats as stats

    return stats.get_stats()
