"""Project CRUD + lifecycle routes: create/delete/fork projects, project state,
workflow progress, and workflow history."""

from __future__ import annotations

import json
import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..paths import PROJECTS_DIR
from ..state import get_runtime, runtimes
from ..store import close_project_db

router = APIRouter()


def _valid_project_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


@router.get("/api/projects")
async def list_projects():
    out = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                # Only reuse an already-loaded runtime; don't spawn kernels for
                # every project on the disk just to count messages/artifacts.
                rt = runtimes.get(d.name)
                if rt is not None:
                    msgs = len(rt.store.list_messages())
                    arts = len(rt.artifacts.list())
                    try:
                        busy = rt.is_busy()
                    except Exception:  # noqa: BLE001
                        busy = False
                else:
                    try:
                        from ..artifacts.store import ArtifactStore
                        from ..store import ProjectStore
                        store = ProjectStore(d)
                        msgs = len(store.list_messages(limit=2000))
                        arts = len(ArtifactStore(d).list(limit=2000))
                        busy = False
                    except Exception:  # noqa: BLE001
                        msgs = arts = 0
                        busy = False
                out.append({
                    "name": d.name,
                    "messages": msgs,
                    "artifacts": arts,
                    "busy": busy,
                    "updated": d.stat().st_mtime if hasattr(d, "stat") else 0,
                })
    return {"projects": out}


@router.post("/api/projects")
async def create_project(body: dict):
    name = (body.get("name") or "").strip().replace("/", "_")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    d = PROJECTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    get_runtime(name)
    return {"name": name}


@router.delete("/api/projects/{name}")
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


@router.post("/api/projects/{name}/fork")
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


@router.get("/api/projects/{name}/messages")
async def project_messages(name: str, limit: int = 200, offset: int = 0):
    """Paginated chat messages (newest-first, so offset 0 = the most recent)."""
    rt = get_runtime(name)
    rows = rt.store.list_messages(limit=min(max(int(limit), 1), 2000))
    total = len(rows)
    start = min(int(offset), total)
    return {"messages": rows[start:], "total": total,
            "has_more": start + len(rows) < total}


@router.get("/api/projects/{name}/state")
async def project_state(name: str, light: bool = False):
    rt = get_runtime(name)
    if light:
        # Cheap variant: counts + kernel status, no full message/artifact payloads.
        return {
            "name": name,
            "light": True,
            "message_count": len(rt.store.list_messages(limit=2000)),
            "artifact_count": len(rt.artifacts.list(limit=2000)),
            "grant_count": len(rt.store.list_grants()),
            "status": rt.status(),
        }
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
    try:
        act = rt.store.get_setting("management_last_activity", "")
        mgmt_activity = json.loads(act) if act else None
    except Exception:  # noqa: BLE001
        mgmt_activity = None
    return {"name": name, "messages": msgs, "artifacts": arts, "grants": grants,
            "env": env, "variables": vars_, "management_activity": mgmt_activity}


@router.get("/api/projects/{name}/status")
async def project_status(name: str):
    """Unified live view of a project: in-flight campaigns/evals/plans, kernel
    health (incl. restarts), workflow snapshot, and audit stats."""
    return {"status": get_runtime(name).status()}


@router.get("/api/projects/{name}/workflow")
async def project_workflow(name: str):
    """Latest workflow-progress snapshot (arXiv replication, …).

    The WebSocket pushes `workflow` events live; this endpoint lets any page or
    section load fetch the current state on demand (event-driven self-heal).
    """
    return {"workflow": get_runtime(name).workflow.snapshot()}


@router.get("/api/projects/{name}/workflow/history")
async def project_workflow_history(name: str):
    """Archived workflow runs (persisted in SQLite across restarts)."""
    return {"workflow_runs": get_runtime(name).store.list_workflow_runs()}
