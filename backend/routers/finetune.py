"""Finetune (dk-lora) status routes: surface dk-lora training jobs in the UI.

The dk-lora MCP server owns LoRA/QLoRA training and keeps its jobs on disk in a
workspace (jobs/<id>.json + jobs/<id>.log). The workbench acts as a read-only
viewer here so the Experiments UI can show live training status without
round-tripping through the MCP host. Parsing logic lives in finetune_status.py
so the chat monitor and this router agree on what "status" means.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import finetune_status as fs
from ..state import CONFIG, save_config

router = APIRouter()


@router.get("/api/finetune/status")
async def finetune_status():
    """All dk-lora training jobs with live progress + last metrics."""
    ws = fs.workspace_path()
    return {"workspace": str(ws), "workspace_ok": ws.is_dir(),
            "jobs": fs.list_jobs()}


@router.get("/api/finetune/jobs/{job_id}")
async def finetune_job(job_id: str):
    """One job: full record + log tail + metric history."""
    raw = fs.read_json(fs.jobs_dir() / f"{job_id}.json")
    if raw is None:
        return JSONResponse({"error": f"job not found: {job_id}"},
                            status_code=404)
    parsed = fs.parse_log(fs.job_log_path(raw), n_chars=8000)
    return {**fs.job_summary(raw, parsed), "metrics": parsed["metrics"],
            "log_tail": parsed["log_tail"]}


@router.get("/api/finetune/pipeline")
async def finetune_pipeline():
    """The quai-lora pipeline snapshot (stages 1-4) for the chat pipeline card."""
    return fs.pipeline_snapshot()


@router.post("/api/finetune/workspace")
async def finetune_set_workspace(body: dict):
    """Point the finetune status view at a dk-lora workspace directory."""
    ws = (body.get("workspace") or "").strip()
    if not ws:
        return JSONResponse({"error": "workspace path required"}, status_code=400)
    cfg = CONFIG.setdefault("finetune", {})
    cfg["workspace"] = ws
    save_config(CONFIG)
    return {"workspace": str(Path(ws).expanduser().resolve())}


@router.post("/api/finetune/stage")
async def finetune_stage_trigger(body: dict):
    """Queue a pipeline stage (1-4) for the host worker to run."""
    try:
        stage = int(body.get("stage"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "stage (int 1-4) required"}, status_code=400)
    try:
        req = fs.submit_stage(stage, job_id=body.get("job_id") or "",
                              options=body.get("options") or {},
                              label=body.get("label") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"request_id": req["request_id"], "stage": stage, "status": "queued"}


@router.get("/api/finetune/stage")
async def finetune_stage_list():
    """Stage requests + their results (for the chat/panel to reflect them)."""
    return {"requests": fs.list_stage_requests()}


@router.get("/api/finetune/validate")
async def finetune_validate_list():
    """RAG verification runs with progress + deltas + report text."""
    return {"runs": fs.validate_runs()}
