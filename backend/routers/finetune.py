"""Finetune (dk-lora) status routes: surface dk-lora training jobs in the UI.

The dk-lora MCP server owns LoRA/QLoRA training and keeps its jobs on disk in a
workspace (jobs/<id>.json + jobs/<id>.log). The workbench acts as a read-only
viewer here so the Experiments UI can show live training status without
round-tripping through the MCP host.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..state import CONFIG, save_config

router = APIRouter()

WORKSPACE_ENV = "FOX_DK_LORA_WORKSPACE"
DEFAULT_WORKSPACE = "~/.fox/dk-lora"

_METRIC_RE = re.compile(r"\[dk-metric\]\s*(\w+)=([^\s]+)")
_TQDM_RE = re.compile(r"(\d+)/(\d+)\s+\[(\d+):(\d+)<([0-9:]+)")
_TRAINER_METRIC_RE = re.compile(r"'(\w+)':\s*'?([^',}]+)'?")


def _workspace_path() -> Path:
    cfg_ws = (CONFIG.get("finetune") or {}).get("workspace", "").strip()
    env_ws = os.environ.get(WORKSPACE_ENV, "").strip()
    return Path(cfg_ws or env_ws or DEFAULT_WORKSPACE).expanduser().resolve()


def _jobs_dir() -> Path:
    return _workspace_path() / "jobs"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt file treated as missing
        return None


def _parse_log(path: Path, n_chars: int = 3000) -> dict:
    """Extract tail + last metrics/progress from a training log."""
    out: dict = {"log_tail": "", "metrics": [], "step": None, "total": None,
                 "last_loss": None, "last_epoch": None}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return out
    out["log_tail"] = text[-n_chars:]
    metric_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TQDM_RE.search(line)
        if m:
            out["step"] = int(m.group(1))
            out["total"] = int(m.group(2))
        if line.startswith("[dk-metric]"):
            metric_lines.append(dict(_METRIC_RE.findall(line)))
        elif line.startswith("{"):
            metric_lines.append(dict(_TRAINER_METRIC_RE.findall(line)))
    # Deduplicate consecutive identical dicts (tqdm rerenders the same line).
    seen: list[dict] = []
    for d in metric_lines:
        if d and (not seen or seen[-1] != d):
            seen.append(d)
    out["metrics"] = seen[-20:]
    if seen:
        last = seen[-1]
        out["last_loss"] = last.get("loss")
        out["last_epoch"] = last.get("epoch")
    return out


def _job_summary(raw: dict, parsed: dict) -> dict:
    cfg = raw.get("config") or {}
    return {
        "id": raw.get("id"),
        "kind": raw.get("kind"),
        "status": raw.get("status"),
        "created_at": raw.get("created_at", 0),
        "updated_at": raw.get("updated_at", 0),
        "error": raw.get("error", ""),
        "result": raw.get("result") or {},
        "output_dir": raw.get("output_dir", ""),
        "config": cfg,
        "step": parsed.get("step"),
        "total": parsed.get("total"),
        "last_loss": parsed.get("last_loss"),
        "last_epoch": parsed.get("last_epoch"),
    }


@router.get("/api/finetune/status")
async def finetune_status():
    """All dk-lora training jobs with live progress + last metrics."""
    ws = _workspace_path()
    jobs = []
    jdir = _jobs_dir()
    if jdir.exists():
        for path in sorted(jdir.glob("*.json"), reverse=True):
            raw = _read_json(path)
            if not raw or raw.get("kind") != "training":
                continue
            log_path = Path(str(raw.get("log_path") or ""))
            if not log_path.exists():
                log_path = jdir / f"{raw.get('id')}.log"
            parsed = _parse_log(log_path)
            jobs.append(_job_summary(raw, parsed))
    return {"workspace": str(ws), "workspace_ok": ws.is_dir(), "jobs": jobs}


@router.get("/api/finetune/jobs/{job_id}")
async def finetune_job(job_id: str):
    """One job: full record + log tail + metric history."""
    raw = _read_json(_jobs_dir() / f"{job_id}.json")
    if raw is None:
        return JSONResponse({"error": f"job not found: {job_id}"},
                            status_code=404)
    log_path = Path(str(raw.get("log_path") or ""))
    if not log_path.exists():
        log_path = _jobs_dir() / f"{job_id}.log"
    parsed = _parse_log(log_path, n_chars=8000)
    return {**_job_summary(raw, parsed), "metrics": parsed["metrics"],
            "log_tail": parsed["log_tail"]}


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
