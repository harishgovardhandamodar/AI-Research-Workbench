"""Artifact + project-file routes: listing, download, metadata, delete, and
upload."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..artifacts.store import Artifact
from ..paths import PROJECTS_DIR
from ..state import get_runtime, runtimes

router = APIRouter()

_MEDIA = {"png": "image/png", "svg": "image/svg+xml",
          "html": "text/html", "text": "text/plain"}


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


@router.get("/api/projects/{name}/artifacts")
async def list_artifacts(name: str):
    return {"artifacts": get_runtime(name).artifacts.list()}


@router.get("/artifacts/{artifact_id}")
async def artifact_file(artifact_id: str):
    for rt in runtimes.values():
        data = rt.artifacts.data(artifact_id)
        if data is not None:
            art = rt.artifacts.get(artifact_id)
            media = _MEDIA.get(art.data_type if art else "text",
                               "application/octet-stream")
            return FileResponse(rt.artifacts.artifacts_dir / Path(art.data_path).name,
                                media_type=media)
    # Runtime not loaded (e.g. after restart): fall back to scanning the
    # projects' artifacts directories so existing files keep working.
    path = _find_artifact_file_on_disk(artifact_id)
    if path is not None:
        media = _MEDIA.get(path.suffix.lstrip("."), "application/octet-stream")
        return FileResponse(path, media_type=media)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/api/artifacts/{artifact_id}/meta")
async def artifact_meta(artifact_id: str):
    for rt in runtimes.values():
        art = rt.artifacts.get(artifact_id)
        if art is not None:
            return {"artifact": art.to_dict()}
    meta = _find_artifact_meta_on_disk(artifact_id)
    if meta is not None:
        return {"artifact": meta}
    return JSONResponse({"error": "not found"}, status_code=404)


@router.delete("/api/projects/{name}/artifacts/{artifact_id}")
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


@router.get("/api/projects/{name}/files")
async def project_files(name: str):
    return {"files": _list_project_files(name)}


@router.post("/api/projects/{name}/files")
async def project_files_upload(name: str, upload: UploadFile = File(...)):
    rt = get_runtime(name)
    filename = _safe_filename(upload.filename or "")
    dest = rt.dir / filename
    data = await upload.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (limit 50 MB)")
    dest.write_bytes(data)
    return {"files": _list_project_files(name)}


@router.get("/api/projects/{name}/files/{filename}")
async def project_file_download(name: str, filename: str):
    rt = get_runtime(name)
    dest = rt.dir / _safe_filename(filename)
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media = "application/octet-stream"
    return FileResponse(dest, media_type=media, filename=dest.name)


@router.delete("/api/projects/{name}/files/{filename}")
async def project_file_delete(name: str, filename: str):
    rt = get_runtime(name)
    dest = rt.dir / _safe_filename(filename)
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    dest.unlink()
    return {"files": _list_project_files(name)}
