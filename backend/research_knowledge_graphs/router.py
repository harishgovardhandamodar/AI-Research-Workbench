"""FastAPI router for the vendored Research Knowledge Graphs app.

Ports the hive-research-gpu REST API (previously a stdlib http.server) into the
workbench's FastAPI backend, namespaced under ``/api/rkg``. The dashboard and
landscape views are served at ``/rkg/dashboard`` and ``/rkg/landscape`` (their
JS API calls were rewritten to the ``/api/rkg`` prefix).

The Organizer is created lazily on first request so the workbench startup never
blocks on Ollama / GPU / pool probing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import Config
from .gpu import GPUManager
from .logs import get_capture
from .organizer import Organizer

router = APIRouter()

_org: Organizer | None = None
_gpu_mgr: GPUManager | None = None
_VIEWS = Path(__file__).parent / "views"


def get_org() -> Organizer:
    """Lazily build the shared Organizer (Ollama + GPU + pool wiring)."""
    global _org, _gpu_mgr
    if _org is None:
        config = Config()
        _gpu_mgr = GPUManager(config)
        _org = Organizer(config, _gpu_mgr)
    return _org


def _sanitize_id(label: str) -> str:
    from .pipeline import _sanitize_id as _sid

    return _sid(label)


# ------------------------------------------------------------ views ----------

@router.get("/rkg/dashboard", response_class=HTMLResponse)
async def rkg_dashboard() -> str:
    return (_VIEWS / "dashboard.html").read_text(encoding="utf-8")


@router.get("/rkg/landscape", response_class=HTMLResponse)
async def rkg_landscape() -> str:
    return (_VIEWS / "landscape.html").read_text(encoding="utf-8")


# ------------------------------------------------------------ GET API --------

@router.get("/api/rkg/graph")
async def rkg_graph():
    return get_org().graph_data()


@router.get("/api/rkg/stats")
async def rkg_stats():
    return get_org().stats()


@router.get("/api/rkg/similarity")
async def rkg_similarity(paper_ids: str | None = Query(default=None),
                         algorithm: str = "combined"):
    org = get_org()
    pids = [x.strip() for x in paper_ids.split(",") if x.strip()] if paper_ids else None
    return org.similarity(paper_ids=pids, algorithm=algorithm)


@router.get("/api/rkg/papers")
async def rkg_papers():
    org = get_org()
    has_lineage = {e.source for e in org.kg._hive.edges if e.relation == "cites"}
    vault_dir = org.config.vault_dir
    papers = []
    for n in org.kg.papers:
        safe = _sanitize_id(n.label) or n.id
        notes_file = Path(vault_dir) / safe / "00_notes.md"
        if notes_file.exists():
            note_path = str(notes_file)
            note_dir = str(Path(vault_dir) / safe)
        else:
            legacy = Path(vault_dir) / f"{safe}.md"
            note_path = str(legacy) if legacy.exists() else ""
            note_dir = note_path
        papers.append({
            "id": n.id, "title": n.label,
            "authors": (', '.join(a.name for a in n.authors)
                        if isinstance(n.authors, list) else n.authors) or "",
            "published": n.published, "affiliations": n.affiliations,
            "note_path": note_path,
            "note_dir": note_dir if Path(note_dir).exists() else "",
            "has_lineage": n.id in has_lineage,
            "has_extra": bool(n.definition and n.definition.startswith("{")),
        })
    return papers


@router.get("/api/rkg/papers/search")
async def rkg_papers_search(q: str = Query(default="")):
    def _auth_str(n):
        a = n.authors
        return ', '.join(x.name for x in a) if isinstance(a, list) else (a or "")

    ql = q.lower()
    return [
        {"id": n.id, "title": n.label, "authors": _auth_str(n),
         "published": n.published, "affiliations": n.affiliations}
        for n in get_org().kg.papers
        if not ql or ql in n.label.lower() or ql in _auth_str(n).lower()
        or ql in (n.affiliations or "").lower()
    ]


@router.get("/api/rkg/concepts")
async def rkg_concepts():
    return [
        {"id": n.id, "label": n.label, "definition": n.definition}
        for n in get_org().kg.concepts
    ]


@router.get("/api/rkg/browse")
async def rkg_browse():
    org = get_org()
    papers_dir = str(org.config.papers_dir)
    vault_dir = str(org.config.vault_dir)
    tree = []

    def _scan(dirpath):
        entries = []
        try:
            for entry in sorted(os.listdir(dirpath)):
                full = os.path.join(dirpath, entry)
                if os.path.isdir(full):
                    files = []
                    for root, _dirs, filenames in os.walk(full):
                        for fn in sorted(filenames):
                            rel = os.path.relpath(os.path.join(root, fn), full)
                            if rel.startswith("."):
                                continue
                            ext = os.path.splitext(fn)[1].lower()
                            files.append({"name": rel, "ext": ext})
                    entries.append({"name": entry, "files": files})
                else:
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in (".pdf", ".md", ".txt", ".py", ".yaml", ".json", ".html", ".csv"):
                        entries.append({"name": entry, "files": [{"name": entry, "ext": ext}]})
        except Exception:
            pass
        return entries

    try:
        tree = _scan(papers_dir)
        vault_entries = []
        for entry in sorted(os.listdir(vault_dir)):
            full = os.path.join(vault_dir, entry)
            if os.path.isdir(full):
                files = []
                for root, _dirs, filenames in os.walk(full):
                    for fn in sorted(filenames):
                        rel = os.path.relpath(os.path.join(root, fn), full)
                        if rel.startswith("."):
                            continue
                        ext = os.path.splitext(fn)[1].lower()
                        files.append({"name": rel, "ext": ext})
                if files:
                    vault_entries.append({"name": entry, "files": files})
            elif entry.endswith(".md"):
                vault_entries.append({"name": entry, "files": [{"name": entry, "ext": ".md"}]})
        if vault_entries:
            vault_entries.sort(key=lambda e: e["name"])
            tree.append({"name": "Notes", "files": vault_entries})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"tree": tree}


@router.get("/api/rkg/read")
async def rkg_read(path: str = Query(default="")):
    if not path:
        return JSONResponse({"error": "missing path"}, status_code=400)
    org = get_org()
    basedirs = [str(org.config.papers_dir), str(org.config.vault_dir)]
    for basedir in basedirs:
        abspath = os.path.normpath(os.path.join(basedir, path))
        if abspath.startswith(os.path.normpath(basedir)) and os.path.isfile(abspath):
            try:
                return {"path": path, "content": open(abspath, encoding="utf-8").read()}
            except Exception:
                continue
    if path.startswith("Notes/"):
        stripped = path[len("Notes/"):]
        for basedir in basedirs:
            abspath = os.path.normpath(os.path.join(basedir, stripped))
            if abspath.startswith(os.path.normpath(basedir)) and os.path.isfile(abspath):
                try:
                    return {"path": path, "content": open(abspath, encoding="utf-8").read()}
                except Exception:
                    continue
    return JSONResponse({"error": "file not found"}, status_code=404)


@router.get("/api/rkg/raw")
async def rkg_raw(path: str = Query(default="")):
    if not path:
        return JSONResponse({"error": "missing path"}, status_code=400)
    org = get_org()
    basedirs = [str(org.config.papers_dir), str(org.config.vault_dir), "."]
    abspath = os.path.normpath(path)
    found = os.path.isfile(abspath)
    if not found:
        for basedir in basedirs:
            cand = os.path.normpath(os.path.join(basedir, path))
            if os.path.isfile(cand):
                abspath = cand
                found = True
                break
    if not found and path.startswith("Notes/"):
        stripped = path[len("Notes/"):]
        for basedir in [str(org.config.vault_dir), str(org.config.papers_dir)]:
            cand = os.path.normpath(os.path.join(basedir, stripped))
            if os.path.isfile(cand):
                abspath = cand
                found = True
                break
    if not found:
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = os.path.splitext(abspath)[1].lstrip(".").lower()
    cts = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "pdf": "application/pdf",
        "md": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8",
    }
    try:
        if ext in ("png", "jpg", "jpeg", "gif", "svg", "pdf"):
            data = Path(abspath).read_bytes()
            return Response(content=data, media_type=cts.get(ext, "application/octet-stream"),
                            headers={"Cache-Control": "max-age=3600"})
        content = Path(abspath).read_text(encoding="utf-8")
        return HTMLResponse(
            f"<pre style='background:#0a0e17;color:#e2e8f0;padding:20px;font-size:13px;"
            f"line-height:1.7;white-space:pre-wrap'>{content}</pre>")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/rkg/web/list")
async def rkg_web_list():
    web_nodes = [
        {"id": n.id, "title": n.label,
         "url": next((l.split("URL:")[1].strip() for l in (n.definition or "").split("\n")
                      if l.startswith("URL:")), ""),
         "summary": n.abstract[:200]}
        for n in get_org().kg._hive.nodes
        if n.type == "web"
    ]
    return web_nodes


@router.get("/api/rkg/ollama")
async def rkg_ollama():
    import platform

    import requests

    org = get_org()
    base = org.config.ollama_base_url
    model = org.config.ollama_model
    fast = org.config.ollama_fast_model
    embed = org.config.ollama_embed_model
    connected = False
    models = []
    try:
        r = requests.get(f"{base}/api/tags", timeout=5)
        if r.status_code == 200:
            connected = True
            models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return {
        "connected": connected, "base_url": base, "model": model,
        "fast_model": fast, "embed_model": embed,
        "model_available": model in models,
        "fast_available": fast in models,
        "embed_available": embed in models,
        "platform": platform.platform(), "processor": platform.processor(),
        "python": platform.python_version(),
    }


@router.get("/api/rkg/gpu")
async def rkg_gpu():
    global _gpu_mgr
    if _gpu_mgr is not None and _gpu_mgr._nvidia_available:
        status = _gpu_mgr.get_status()
        return {"backend": "cuda", "nvidia": True, "device_count": status["count"],
                "devices": status["devices"], "platform": status["platform"],
                "processor": status["processor"], "python": status["python"]}
    import subprocess

    info: dict[str, Any] = {"backend": "cpu", "nvidia": False, "details": ""}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            info["backend"] = "cuda"
            info["nvidia"] = True
            devices = []
            for line in r.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(", ")]
                if len(parts) >= 6:
                    devices.append({
                        "index": parts[0], "name": parts[1],
                        "memory_total_mb": parts[2], "memory_used_mb": parts[3],
                        "utilization_percent": parts[4], "temperature_c": parts[5],
                    })
            info["devices"] = devices
            info["details"] = f"{len(devices)} NVIDIA GPU(s) detected"
    except Exception:
        info["details"] = "No GPU detected"
    return info


@router.get("/api/rkg/logs")
async def rkg_logs(n: int = 100):
    return get_capture().get_recent(n)


@router.get("/api/rkg/pool")
async def rkg_pool():
    return get_org().pool.get()


@router.get("/api/rkg/pool/papers")
async def rkg_pool_papers():
    return get_org().pool.get_observed_papers()


@router.get("/api/rkg/pool/graph")
async def rkg_pool_graph():
    return get_org().pool.get_pool_graph()


@router.get("/api/rkg/pool/topics")
async def rkg_pool_topics():
    return {"topics": get_org().pool.get_topics()}


# ----------------------------------------------------------- POST API --------

@router.post("/api/rkg/add")
async def rkg_add(data: dict = Body(default={})):
    arxiv_id = data.get("id", "")
    if not arxiv_id:
        return JSONResponse({"error": "missing id"}, status_code=400)
    org = get_org()
    model = org.config.resolve_model(data.get("model"))
    return org.add_by_id(arxiv_id, model=model)


@router.post("/api/rkg/search")
async def rkg_search(data: dict = Body(default={})):
    query = data.get("query", "")
    if not query:
        return JSONResponse({"error": "missing query"}, status_code=400)
    return get_org().search(query)


@router.post("/api/rkg/import")
async def rkg_import(data: dict = Body(default={})):
    query = data.get("query", "")
    if not query:
        return JSONResponse({"error": "missing query"}, status_code=400)
    org = get_org()
    model = org.config.resolve_model(data.get("model"))
    return org.add_by_search(query, model=model)


@router.post("/api/rkg/query")
async def rkg_query(data: dict = Body(default={})):
    question = data.get("question", "")
    if not question:
        return JSONResponse({"error": "missing question"}, status_code=400)
    return get_org().query_rag(question)


@router.post("/api/rkg/lineage")
async def rkg_lineage(data: dict = Body(default={})):
    arxiv_id = data.get("arxiv_id", "")
    if not arxiv_id:
        return JSONResponse({"error": "missing arxiv_id"}, status_code=400)
    return get_org().fetch_lineage(arxiv_id)


@router.post("/api/rkg/web/add")
async def rkg_web_add(data: dict = Body(default={})):
    url = data.get("url", "")
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    org = get_org()
    model = org.config.resolve_model(data.get("model"))
    return org.web.ingest(url, model=model)


@router.post("/api/rkg/similarity")
async def rkg_similarity_post(data: dict = Body(default={})):
    paper_ids = data.get("paper_ids", None)
    algorithm = data.get("algorithm", "combined")
    if isinstance(paper_ids, str):
        paper_ids = [x.strip() for x in paper_ids.split(",") if x.strip()]
    return get_org().similarity(paper_ids=paper_ids, algorithm=algorithm)


@router.post("/api/rkg/refresh")
async def rkg_refresh(data: dict = Body(default={})):
    org = get_org()
    model = org.config.resolve_model(data.get("model"))
    return org.refresh_papers(model=model)


@router.post("/api/rkg/papers/refresh")
async def rkg_paper_refresh(data: dict = Body(default={})):
    paper_id = data.get("paper_id", "")
    if not paper_id:
        return JSONResponse({"error": "missing paper_id"}, status_code=400)
    org = get_org()
    model = org.config.resolve_model(data.get("model"))
    return org.refresh_paper(paper_id, model=model)


@router.post("/api/rkg/graph/detail")
async def rkg_graph_detail():
    return get_org().detail_graph()


@router.post("/api/rkg/definitions")
async def rkg_definitions():
    return get_org().generate_definitions()


@router.post("/api/rkg/pool/topics/add")
async def rkg_pool_topics_add(data: dict = Body(default={})):
    name = data.get("name", "")
    query = data.get("query", "")
    if not name or not query:
        return JSONResponse({"error": "missing name or query"}, status_code=400)
    get_org().pool.add_topic(name, query)
    return {"status": "ok"}


@router.post("/api/rkg/pool/topics/remove")
async def rkg_pool_topics_remove(data: dict = Body(default={})):
    name = data.get("name", "")
    if not name:
        return JSONResponse({"error": "missing name"}, status_code=400)
    get_org().pool.remove_topic(name)
    return {"status": "ok"}


@router.post("/api/rkg/pool/import")
async def rkg_pool_import(data: dict = Body(default={})):
    arxiv_id = data.get("arxiv_id", "")
    if not arxiv_id:
        return JSONResponse({"error": "missing arxiv_id"}, status_code=400)
    org = get_org()
    result = org.add_by_id(arxiv_id)
    if result.get("status") in ("added", "exists"):
        org.pool.mark_imported(arxiv_id)
    return result


@router.post("/api/rkg/pool/import_batch")
async def rkg_pool_import_batch(data: dict = Body(default={})):
    arxiv_ids = data.get("arxiv_ids", [])
    if not arxiv_ids:
        return JSONResponse({"error": "missing arxiv_ids"}, status_code=400)
    org = get_org()
    results = []
    for aid in arxiv_ids:
        r = org.add_by_id(aid)
        if r.get("status") in ("added", "exists"):
            org.pool.mark_imported(aid)
        results.append({"arxiv_id": aid, "status": r.get("status")})
    return {"results": results}
