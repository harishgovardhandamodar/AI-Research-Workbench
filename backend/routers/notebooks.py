"""Kernel, notebook, grant, approval and knowledge-graph routes."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..artifacts.store import Artifact
from ..notebooks import NotebookError, new_notebook
from ..state import get_runtime

router = APIRouter()


@router.post("/api/projects/{name}/kernel/reset")
async def reset_kernel(name: str):
    rt = get_runtime(name)
    await rt.kernels.reset()
    return {"ok": True}


# ---------------------------------------------------------- notebooks --------

@router.get("/api/projects/{name}/notebooks")
async def list_notebooks(name: str):
    return {"notebooks": get_runtime(name).notebooks.list()}


@router.post("/api/projects/{name}/notebooks")
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


@router.get("/api/projects/{name}/notebooks/{nbname}")
async def get_notebook(name: str, nbname: str):
    try:
        nb = get_runtime(name).notebooks.load(nbname)
    except NotebookError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"notebook": nb}


@router.put("/api/projects/{name}/notebooks/{nbname}")
async def save_notebook(name: str, nbname: str, body: dict):
    rt = get_runtime(name)
    cells = body.get("cells")
    if not isinstance(cells, list):
        return JSONResponse({"error": "cells required"}, status_code=400)
    nb = rt.notebooks.load(nbname)
    nb["cells"] = cells
    rt.notebooks.save(nbname, nb)
    return {"notebook": nb}


@router.delete("/api/projects/{name}/notebooks/{nbname}")
async def delete_notebook(name: str, nbname: str):
    rt = get_runtime(name)
    if not rt.notebooks.delete(nbname):
        raise HTTPException(status_code=404, detail="notebook not found")
    return {"deleted": nbname}


@router.post("/api/projects/{name}/notebooks/{nbname}/execute")
async def execute_notebook(name: str, nbname: str, body: dict):
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


# ------------------------------------------------------------- grants --------

@router.get("/api/projects/{name}/grants")
async def list_grants(name: str):
    return {"grants": get_runtime(name).store.list_grants()}


@router.delete("/api/projects/{name}/grants/{grant_id}")
async def delete_grant(name: str, grant_id: str):
    rt = get_runtime(name)
    with rt.store._conn:
        cur = rt.store._conn.execute("DELETE FROM grants WHERE id=?", (int(grant_id),))
    return {"deleted": cur.rowcount > 0}


@router.get("/api/projects/{name}/approvals")
async def list_approvals(name: str, limit: int = 50):
    """Audit trail of approval decisions (allow / deny / temporary / timeout)."""
    return {"approvals": get_runtime(name).store.list_approvals(limit)}


# -------------------------------------------------- knowledge graphs ----------

@router.get("/api/projects/{name}/graphs")
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


@router.get("/api/projects/{name}/graphs/{filename}")
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
