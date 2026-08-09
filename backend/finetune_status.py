"""Shared finetune (dk-lora) status helpers: workspace/job parsing + the
quai-lora pipeline snapshot. Used by both the REST router and the chat monitor
so the Experiments panel and the chat window agree on what "status" means.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .state import CONFIG

WORKSPACE_ENV = "FOX_DK_LORA_WORKSPACE"
DEFAULT_WORKSPACE = "~/.fox/dk-lora"

_METRIC_RE = re.compile(r"\[dk-metric\]\s*(\w+)=([^\s]+)")
_TQDM_RE = re.compile(r"(\d+)/(\d+)\s+\[(\d+):(\d+)<([0-9:]+)")
_TRAINER_METRIC_RE = re.compile(r"'(\w+)':\s*'?([^',}]+)'?")

# quai-lora pipeline stages, in order, as shown in the chat pipeline card.
PIPELINE_STAGES = [
    {"id": "ingest", "label": "Ingest + chunk corpus"},
    {"id": "dataset", "label": "Build training dataset"},
    {"id": "train", "label": "Train LoRA adapter"},
    {"id": "verify", "label": "Verify base vs adapter"},
]


def workspace_path() -> Path:
    cfg_ws = (CONFIG.get("finetune") or {}).get("workspace", "").strip()
    env_ws = os.environ.get(WORKSPACE_ENV, "").strip()
    return Path(cfg_ws or env_ws or DEFAULT_WORKSPACE).expanduser().resolve()


def jobs_dir() -> Path:
    return workspace_path() / "jobs"


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt file treated as missing
        return None


def parse_log(path: Path, n_chars: int = 3000) -> dict:
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


def job_log_path(raw: dict) -> Path:
    """The log file for a job record, with a workspace-relative fallback (the
    record's log_path is an absolute host path that differs inside the
    container's bind mount)."""
    p = Path(str(raw.get("log_path") or ""))
    if p.exists():
        return p
    return jobs_dir() / f"{raw.get('id')}.log"


def job_summary(raw: dict, parsed: dict | None = None) -> dict:
    parsed = parsed or parse_log(job_log_path(raw))
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
        "metrics": parsed.get("metrics", []),
    }


def list_jobs() -> list[dict]:
    jobs = []
    jdir = jobs_dir()
    if jdir.exists():
        for path in sorted(jdir.glob("*.json"), reverse=True):
            raw = read_json(path)
            if not raw or raw.get("kind") != "training":
                continue
            jobs.append(job_summary(raw))
    return jobs


def get_job(job_id: str) -> dict | None:
    raw = read_json(jobs_dir() / f"{job_id}.json")
    if raw is None:
        return None
    return job_summary(raw)


def _stage_state(ok: bool, running: bool = False, failed: bool = False,
                 detail: str = "") -> dict:
    if failed:
        return {"state": "failed", "detail": detail or "failed", "pct": 100}
    if running:
        return {"state": "running", "detail": detail or "running", "pct": 50}
    if ok:
        return {"state": "done", "detail": detail or "done", "pct": 100}
    return {"state": "pending", "detail": detail or "queued", "pct": 0}


def _validate_store_dir() -> Path | None:
    """The ft-validate store for the current workspace's project (sibling of
    data/workspace), so stage 4 can reflect verification runs."""
    ws = workspace_path()
    for cand in (ws / ".." / "validate", ws.parent.parent / "validate"):
        cand = cand.resolve()
        if cand.is_dir():
            return cand
    return None


def _validate_runs(store_dir: Path | None) -> list[dict]:
    if store_dir is None:
        return []
    out = []
    for f in sorted(store_dir.glob("runs/*.json")):
        raw = read_json(f)
        if raw:
            out.append(raw)
    return out


def pipeline_snapshot() -> dict:
    """Derive the quai-lora pipeline stage states from the workspace on disk."""
    ws = workspace_path()
    raw_artifacts = read_json(ws / "index.json") or {}
    n_artifacts = len(raw_artifacts.get("artifacts", {}))
    n_chunks = len(raw_artifacts.get("chunks", {}) or [])
    datasets = []
    if (ws / "datasets").exists():
        for p in (ws / "datasets").glob("*.meta.json"):
            meta = read_json(p)
            if meta:
                datasets.append({"id": p.name[:-len(".meta.json")],
                                 "count": meta.get("count", 0)})
    jobs = list_jobs()
    train = next((j for j in jobs if j["kind"] == "training"), None)
    validate_dir = _validate_store_dir()
    vruns = _validate_runs(validate_dir)

    stages: list[dict] = []
    # 1. ingest + chunk
    if n_artifacts and n_chunks:
        stages.append(_stage_state(True, detail=f"{n_artifacts} artifacts · {n_chunks} chunks"))
    else:
        stages.append(_stage_state(False, detail="no corpus ingested"))
    # 2. dataset
    if datasets:
        best = max(datasets, key=lambda d: d["count"])
        stages.append(_stage_state(True, detail=f"{best['id']} · {best['count']} examples"))
    else:
        stages.append(_stage_state(False, detail="no dataset"))
    # 3. train
    if train is None:
        stages.append(_stage_state(False, detail="no training job"))
    elif train["status"] == "running":
        d = f"{train['id']}"
        if train["total"] and train["step"] is not None:
            d += f" · {train['step']}/{train['total']}"
        if train["last_loss"] is not None:
            d += f" · loss {train['last_loss']}"
        stages.append(_stage_state(True, running=True, detail=d))
    elif train["status"] == "done":
        d = f"{train['id']}"
        if train["last_loss"] is not None:
            d += f" · final loss {train['last_loss']}"
        stages.append(_stage_state(True, detail=d))
    else:
        stages.append(_stage_state(True, failed=True, detail=f"{train['id']} · {train['status']}"))
    # 4. verify
    if vruns:
        last = vruns[-1]
        st = last.get("status", "pending")
        if st in ("done", "completed"):
            stages.append(_stage_state(True, detail=f"{len(vruns)} verification run(s)"))
        elif st == "failed":
            stages.append(_stage_state(True, failed=True, detail="verification failed"))
        else:
            stages.append(_stage_state(True, running=True, detail="verification running"))
    else:
        stages.append(_stage_state(False, detail="not run yet"))

    done = sum(1 for s in stages if s["state"] == "done")
    active = any(s["state"] == "running" for s in stages)
    failed = any(s["state"] == "failed" for s in stages)
    pct = round((done / len(stages)) * 100)
    if active:
        status = "running"
        message = f"{active and 'Training/verification in progress' or ''}".strip() or "pipeline active"
    elif failed:
        status = "failed"
        message = "pipeline stage failed"
    elif done == len(stages):
        status = "done"
        message = "pipeline complete"
    else:
        status = "idle"
        message = "pipeline not started"
    return {
        "workspace": str(ws),
        "stages": [{"id": PIPELINE_STAGES[i]["id"],
                    "label": PIPELINE_STAGES[i]["label"], **stages[i]}
                   for i in range(len(PIPELINE_STAGES))],
        "status": status,
        "message": message,
        "pct": pct,
        "job_id": train["id"] if train else None,
        "job_status": train["status"] if train else None,
    }
