"""Shared finetune (dk-lora) status helpers: workspace/job parsing + the
quai-lora pipeline snapshot. Used by both the REST router and the chat monitor
so the Experiments panel and the chat window agree on what "status" means.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .state import CONFIG

WORKSPACE_ENV = "FOX_DK_LORA_WORKSPACE"
DEFAULT_WORKSPACE = "~/.fox/dk-lora"

_METRIC_RE = re.compile(r"\[dk-metric\]\s*(\w+)=([^\s]+)")
_TQDM_RE = re.compile(r"(\d+)/(\d+)\s+\[([0-9:]+)<([0-9:]+)")
_TRAINER_METRIC_RE = re.compile(r"'(\w+)':\s*'?([^',}]+)'?")

# tqdm "NNN/TTT [elapsed<remaining, X.XXs/it]" — remaining/rate feed the ETA.
_TQDM_ETA_RE = re.compile(r"(\d+)/(\d+)\s+\[([0-9:]+)<([0-9:]+),\s*([0-9.]+)s/it")
_PENDING_VERIFY_ETA = 15 * 60  # rough ft-validate budget when nothing known yet


def _time_to_secs(txt: str) -> int | None:
    """Parse tqdm's MM:SS / H:MM:SS / HH:MM:SS elapsed-or-remaining text."""
    try:
        parts = [int(x) for x in txt.split(":")]
    except (ValueError, AttributeError):
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def fmt_eta(secs: int | None) -> str:
    """Human ETA, e.g. 90 -> '1m 30s', 5326 -> '1h 28m'."""
    if secs is None:
        return ""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"

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


def requests_dir() -> Path:
    return workspace_path() / "requests"


def results_dir() -> Path:
    return workspace_path() / "results"


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt file treated as missing
        return None


def parse_log(path: Path, n_chars: int = 3000) -> dict:
    """Extract tail + last metrics/progress from a training log."""
    out: dict = {"log_tail": "", "metrics": [], "series": [],
                 "step": None, "total": None,
                 "last_loss": None, "last_epoch": None,
                 "eta_secs": None, "rate": None, "finished": False}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return out
    out["log_tail"] = text[-n_chars:]
    # dk-lora scripts print TRAINING_DONE when training finished; the job JSON
    # status can lag (launcher watcher died) so the log is the source of truth.
    out["finished"] = "TRAINING_DONE" in text
    metric_lines = []
    cur_step = None
    series: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TQDM_RE.search(line)
        if m:
            cur_step = int(m.group(1))
            out["step"] = cur_step
            out["total"] = int(m.group(2))
        e = _TQDM_ETA_RE.search(line)
        if e:
            # remaining = m.group(4); rate = m.group(5) seconds/it.
            out["eta_secs"] = _time_to_secs(e.group(4))
            try:
                out["rate"] = float(e.group(5))
            except (TypeError, ValueError):
                out["rate"] = None
        if line.startswith("[dk-metric]"):
            metric_lines.append(dict(_METRIC_RE.findall(line)))
        elif line.startswith("{"):
            d = dict(_TRAINER_METRIC_RE.findall(line))
            metric_lines.append(d)
            # Numeric snapshot with the current step for charting.
            snap = {}
            if cur_step is not None:
                snap["step"] = cur_step
            for k, v in d.items():
                try:
                    snap[k] = float(v)
                except (TypeError, ValueError):
                    snap[k] = v
            if snap and (not series or series[-1] != snap):
                series.append(snap)
    seen: list[dict] = []
    for d in metric_lines:
        if d and (not seen or seen[-1] != d):
            seen.append(d)
    out["metrics"] = seen[-20:]
    # Keep the chart series bounded but complete enough to render curves.
    out["series"] = series[-4000:]
    if seen:
        last = seen[-1]
        out["last_loss"] = last.get("loss") or last.get("train_loss")
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
        "eta_secs": parsed.get("eta_secs"),
        "rate": parsed.get("rate"),
        "eta": fmt_eta(parsed.get("eta_secs")),
        "finished": bool(parsed.get("finished")),
        "series": parsed.get("series", []),
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


def reconcile_job_status(job: dict) -> None:
    """Persist a 'done' status onto the job JSON when the log says TRAINING_DONE
    but the record still says running (the dk-lora watcher thread can die with
    its launcher). Idempotent; only writes when it changes something."""
    if job.get("status") == "done" or not job.get("finished"):
        return
    path = jobs_dir() / f"{job['id']}.json"
    raw = read_json(path)
    if raw is None or raw.get("status") == "done":
        return
    raw["status"] = "done"
    raw.setdefault("result", {})
    raw["result"]["returncode"] = 0
    raw["result"]["completed"] = True
    raw["updated_at"] = time.time()
    try:
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_job(job_id: str) -> dict | None:
    raw = read_json(jobs_dir() / f"{job_id}.json")
    if raw is None:
        return None
    return job_summary(raw)


# ------------------------------------------------------- stage triggers -----
# Pipeline stages run on the *host* (torch/unsloth live in the host venv, not
# the workbench container). The GUI writes a request file into the shared
# workspace; a host-side worker (quai-lora/scripts/stage_worker.py) picks it
# up, executes the stage, and writes a result file. This keeps the trigger
# logic tiny and observable: requests/<id>.json -> results/<id>.json.

STAGE_NAMES = {1: "ingest + chunk", 2: "dataset", 3: "train", 4: "verify"}


def submit_stage(stage: int, job_id: str = "", options: dict | None = None,
                 label: str = "") -> dict:
    """Queue a pipeline stage for the host worker to run."""
    stage = int(stage)
    if stage not in STAGE_NAMES:
        raise ValueError(f"unknown stage {stage}; expected 1-4")
    rid = f"{stage}-{int(time.time() * 1000)}"
    req = {
        "request_id": rid,
        "stage": stage,
        "job_id": job_id or "",
        "options": options or {},
        "label": label or f"stage {stage} ({STAGE_NAMES[stage]})",
        "status": "queued",
        "created_at": time.time(),
    }
    rdir = requests_dir()
    rdir.mkdir(parents=True, exist_ok=True)
    try:
        rdir.chmod(0o777)
    except OSError:  # noqa: BLE001
        pass
    path = rdir / f"{rid}.json"
    path.write_text(json.dumps(req, indent=2), encoding="utf-8")
    try:
        # The host worker runs as a different user (the container is root), so
        # the request must be readable/writable by whoever picks it up.
        path.chmod(0o666)
    except OSError:  # noqa: BLE001
        pass
    return req


def list_stage_requests() -> list[dict]:
    """Queued/running stage requests (newest last) with any attached result."""
    out = []
    rdir, resdir = requests_dir(), results_dir()
    reqs: dict[str, dict] = {}
    if rdir.exists():
        for p in sorted(rdir.glob("*.json")):
            raw = read_json(p)
            if raw:
                reqs[raw["request_id"]] = raw
    res: dict[str, dict] = {}
    if resdir.exists():
        for p in sorted(resdir.glob("*.json")):
            raw = read_json(p)
            if raw:
                res[raw["request_id"]] = raw
    for rid, req in reqs.items():
        out.append({**req, "result": res.get(rid)})
    return sorted(out, key=lambda r: r.get("created_at") or 0)


def get_stage_request(request_id: str) -> dict | None:
    for r in list_stage_requests():
        if r["request_id"] == request_id:
            return r
    return None


# ------------------------------------------------------- validation (RAG) ----
# The ft-validate store lives beside the workspace (data/validate in the
# project) and is written by the host worker; the container reads it read-only
# through the bind mount. We parse it directly (no ft_validate import needed)
# so the Experiments/chat views can show RAG verification progress + reports.

def validate_store_dir() -> Path | None:
    return _validate_store_dir()


def validate_runs() -> list[dict]:
    """All ft-validate verification runs with progress + report text."""
    store_dir = _validate_store_dir()
    if store_dir is None:
        return []
    out = []
    for p in sorted((store_dir / "runs").glob("*.json"), reverse=True):
        raw = read_json(p)
        if not raw:
            continue
        run = {
            "id": raw.get("id"),
            "status": raw.get("status"),
            "eval_set_id": raw.get("eval_set_id"),
            "base_model": raw.get("base_model"),
            "adapter_path": raw.get("adapter_path"),
            "model_ids": raw.get("model_ids") or [],
            "created_at": raw.get("created_at", 0),
            "updated_at": raw.get("updated_at", 0),
            "error": raw.get("error", ""),
            "aggregate": raw.get("aggregate") or {},
            "failures": raw.get("failures") or [],
            "n_questions": len(raw.get("per_question") or []),
            "report": raw.get("report_md") or "",
            "report_path": raw.get("report_path") or "",
        }
        if not run["report"]:
            # The report lives on disk (reports/<id>.md) — read it so the chat
            # and panel can show the full report without ft_validate.
            md = store_dir / "reports" / f"{run['id']}.md"
            if md.exists():
                try:
                    run["report"] = md.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
        run["progress"] = _validate_progress(run, store_dir / "runs")
        run["deltas"] = _validate_deltas(run["aggregate"])
        out.append(run)
    return out


_VALIDATE_PROG_RE = re.compile(r"\[(\d+)/(\d+)\]")


def _validate_progress(run: dict, runs_dir: Path) -> dict:
    """Questions answered so far, parsed from the run's log ([i/N] lines)."""
    log = runs_dir / f"{run['id']}.log"
    answered, total = 0, 0
    if log.exists():
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        for m in _VALIDATE_PROG_RE.finditer(text):
            answered = max(answered, int(m.group(1)))
            total = max(total, int(m.group(2)))
    if not total and run.get("n_questions"):
        total = run["n_questions"]
    return {"answered": answered, "total": total,
            "pct": round(answered / total * 100) if total else 0}


def _validate_deltas(aggregate: dict) -> dict:
    """base vs adapter deltas for the 4 RAG metrics."""
    base = (aggregate or {}).get("base", {})
    adapter = (aggregate or {}).get("adapter", {})
    deltas = {}
    for k in ("faithfulness", "accuracy", "hallucination", "retention"):
        b = (base.get(k) or {}).get("mean")
        a = (adapter.get(k) or {}).get("mean")
        if b is not None and a is not None:
            deltas[k] = round(a - b, 4)
    return deltas


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
    for job in jobs:
        reconcile_job_status(job)
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
        if train["total"] and train["step"] is not None:
            # Carry the real step progress so the headline % tracks the log bar.
            stages[-1]["pct"] = min(100, round(train["step"] / train["total"] * 100))
            # Per-stage ETA straight from tqdm's remaining timer.
            if train.get("eta_secs") is not None:
                stages[-1]["eta_secs"] = train["eta_secs"]
                stages[-1]["eta"] = f"~{fmt_eta(train['eta_secs'])} left"
            if train.get("rate") is not None:
                stages[-1]["rate"] = f"{train['rate']:.1f}s/it"
    elif train["status"] == "done":
        d = f"{train['id']}"
        if train["last_loss"] is not None:
            d += f" · final loss {train['last_loss']}"
        stages.append(_stage_state(True, detail=d))
        stages[-1]["eta"] = "done"
        stages[-1]["eta_secs"] = 0
    else:
        stages.append(_stage_state(True, failed=True, detail=f"{train['id']} · {train['status']}"))
    # 4. verify
    stage_reqs = list_stage_requests()
    active_req = None
    for r in reversed(stage_reqs):
        if r.get("stage") == 4 and r.get("status") in ("queued", "running"):
            active_req = r
            break
    vrun = validate_runs()
    vruns = vrun or vruns
    if vruns:
        last = vruns[0]
        st = last.get("status", "pending")
        prog = last.get("progress") or {}
        if st in ("done", "completed"):
            d = f"{len(vruns)} verification run(s)"
            deltas = last.get("deltas") or {}
            if deltas:
                d += " · " + " · ".join(
                    f"{k} {v:+.2f}" for k, v in deltas.items())
            stages.append(_stage_state(True, detail=d))
            stages[-1]["eta"] = "done"
            stages[-1]["eta_secs"] = 0
            stages[-1]["report"] = last.get("report") or ""
        elif st == "failed":
            stages.append(_stage_state(True, failed=True, detail="verification failed"))
        else:
            d = "verification running"
            if prog.get("total"):
                d += f" · {prog.get('answered')}/{prog.get('total')} questions"
            stages.append(_stage_state(True, running=True, detail=d))
            # Live progress drives the stage % + headline bar.
            stages[-1]["pct"] = prog.get("pct") or 50
            stages[-1]["eta_secs"] = _PENDING_VERIFY_ETA
            stages[-1]["eta"] = f"~{fmt_eta(_PENDING_VERIFY_ETA)}"
    elif active_req is not None:
        # A stage-4 (verify) request is queued/running on the host worker.
        req_detail = f"verification {active_req.get('status')} ({active_req.get('request_id')})"
        stages.append(_stage_state(True, running=True, detail=req_detail))
        stages[-1]["eta_secs"] = _PENDING_VERIFY_ETA
        stages[-1]["eta"] = f"~{fmt_eta(_PENDING_VERIFY_ETA)}"
    else:
        stages.append(_stage_state(False, detail="not run yet"))
        # ft-validate hasn't run: only show an estimate once training is done.
        stages[-1]["eta_secs"] = _PENDING_VERIFY_ETA if train and train["status"] == "done" else None
        if stages[-1]["eta_secs"] is not None:
            stages[-1]["eta"] = f"~{fmt_eta(_PENDING_VERIFY_ETA)}"

    # Total ETA: running stage's tqdm remaining + any queued/estimated stages.
    known = [s.get("eta_secs") for s in stages if s.get("eta_secs") is not None]
    total_eta = sum(known) if known else None

    done = sum(1 for s in stages if s["state"] == "done")
    active = any(s["state"] == "running" for s in stages)
    failed = any(s["state"] == "failed" for s in stages)
    # Overall progress: while a stage is running, the headline % tracks that
    # stage's real progress (e.g. training step/total), so the finetune card
    # matches the live log bar instead of just "2 of 4 stages done". When the
    # pipeline is idle/complete, fall back to the fraction of finished stages.
    if active:
        pct = max((s["pct"] for s in stages if s["state"] == "running"), default=0)
    else:
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
                    "label": PIPELINE_STAGES[i]["label"],
                    "eta": stages[i].get("eta", ""),
                    "eta_secs": stages[i].get("eta_secs"),
                    "rate": stages[i].get("rate"),
                    **stages[i]}
                   for i in range(len(PIPELINE_STAGES))],
        "status": status,
        "message": message,
        "pct": pct,
        "eta": fmt_eta(total_eta),
        "eta_secs": total_eta,
        "job_id": train["id"] if train else None,
        "job_status": train["status"] if train else None,
    }
