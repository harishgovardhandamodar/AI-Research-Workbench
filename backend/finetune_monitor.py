"""Finetune chat monitor: tail dk-lora job logs and push live debug lines +
pipeline snapshots into the chat window, persisting a compact session history.

Design:
- Started per-project (one task per ProjectRuntime) on the first chat connect,
  so a job started from the CLI (pipeline.py) still streams into the GUI chat.
- Reads the shared dk-lora workspace (see finetune_status) on a short tick.
- Broadcasts two events to every subscribed chat window:
    * finetune_log      -> {job, lines:[...]}  (new debug lines since last tick)
    * finetune_pipeline -> {pipeline:{...}}     (stage snapshot, on change)
- Persists assistant messages (meta.tags=["finetune","pipeline"]) at meaningful
  transitions only (start / every ~100 steps / done / failed) so the chat's
  session history stays compact while still telling the finetune story.
"""

from __future__ import annotations

import asyncio
import re
import time

from . import finetune_status as fs

_TICK = 2.0                 # seconds between log-tail polls
_IDLE_TICK = 8.0            # poll interval when no job is active
_PROGRESS_EVERY = 100       # persist a progress message every N steps
_BURST_MAX = 400            # max log lines pushed per socket message
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BASE_MODEL_RE = re.compile(r'"base_model"\s*:\s*"([^"]+)"')


def _find_base_model(script_text: str) -> str:
    """Best-effort: pull base_model out of the generated training script."""
    m = _BASE_MODEL_RE.search(script_text or "")
    return m.group(1).strip() if m else ""


def _clean_line(line: str) -> str:
    """Strip ANSI escapes and collapse \r-redrawn progress segments to the most
    recent frame (tqdm rewrites one physical line many times)."""
    line = _ANSI_RE.sub("", line)
    if "\r" in line:
        line = line.rsplit("\r", 1)[-1]
    return line.strip()


def _split_lines(block: str) -> list[str]:
    out = []
    for line in block.split("\n"):
        line = _clean_line(line)
        if line:
            out.append(line)
    return out


class FinetuneMonitor:
    def __init__(self, runtime):
        self.rt = runtime
        self._task: asyncio.Task | None = None
        self._offsets: dict[str, int] = {}      # job_id -> bytes already read
        self._last_snapshot: dict | None = None
        self._persisted_step: dict[str, int] = {}   # job_id -> last persisted step
        self._persisted_terminal: set[str] = set()  # job_id whose end was persisted
        self._last_broadcast: dict[str, float] = {}  # per-job flood control

    # ------------------------------------------------------------------ life --
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ core --
    async def _run(self) -> None:
        while True:
            try:
                jobs = fs.list_jobs()
                active = [j for j in jobs if j["status"] == "running"]
                await self._tail_logs(active)
                await self._maybe_broadcast_pipeline(jobs)
                await self._persist_transitions(jobs)
                await asyncio.sleep(_TICK if active else _IDLE_TICK)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the monitor die
                await asyncio.sleep(_TICK)

    async def _tail_logs(self, running: list[dict]) -> None:
        for job in running:
            raw = fs.read_json(fs.jobs_dir() / f"{job['id']}.json")
            if raw is None:
                continue
            path = fs.job_log_path(raw)
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
                offset = self._offsets.get(job["id"], 0)
                if size < offset:          # log rotated/truncated
                    offset = 0
                if size <= offset:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    block = f.read(size - offset)
                self._offsets[job["id"]] = size
            except OSError:
                continue
            lines = _split_lines(block)
            if not lines:
                continue
            # Never dump a huge backlog in one socket message: on first connect
            # the whole log would flood the chat. Keep the offset correct so no
            # bytes are lost, but only push the most recent lines.
            if len(lines) > _BURST_MAX:
                lines = lines[-_BURST_MAX:]
            # Flood control: never push more than one burst per ~0.5s.
            now = time.time()
            last = self._last_broadcast.get(job["id"], 0.0)
            if now - last < 0.5 and len(lines) <= 3:
                continue
            self._last_broadcast[job["id"]] = now
            await self.rt.broadcast("finetune_log", {"job": job, "lines": lines})

    async def _maybe_broadcast_pipeline(self, jobs: list[dict]) -> None:
        snap = fs.pipeline_snapshot()
        if snap == self._last_snapshot:
            return
        self._last_snapshot = snap
        await self.rt.broadcast("finetune_pipeline", {"pipeline": snap})

    # -------------------------------------------------------------- history --
    async def _persist_transitions(self, jobs: list[dict]) -> None:
        for job in jobs:
            st = job["status"]
            # The job JSON can lag the log (launcher watcher died): a running
            # job whose log says TRAINING_DONE is actually done.
            if st == "running" and job.get("finished"):
                fs.reconcile_job_status(job)
                job["status"] = "done"
                st = "done"
            if st == "running":
                await self._persist_start_or_progress(job)
            elif st in ("done", "failed"):
                if job["id"] not in self._persisted_terminal:
                    await self._persist_terminal(job)
                    self._persisted_terminal.add(job["id"])
                    self._offsets.pop(job["id"], None)
            # Keep the Experiments-tab record in sync with the training log.
            self._record_run(job)

    def _record_run(self, job: dict) -> None:
        """Create/update a kind=finetune run in the project store so the
        training metrics (loss / grad_norm / learning_rate / epoch) show up in
        the Experiments tab and render as charts from the metric series.

        The run is attached to a project experiment derived from the training
        config (created on demand); its `config.metric_series` carries the full
        numeric history for charting while `metrics` holds the summary scalars
        used by leaderboards.
        """
        try:
            store = self.rt.store
            rid = store.find_finetune_run(job["id"])
            raw = fs.read_json(fs.jobs_dir() / f"{job['id']}.json") or {}
            cfg = raw.get("config") or {}
            try:
                script_path = fs.jobs_dir() / f"{job['id']}.py"
                script_text = script_path.read_text(encoding="utf-8", errors="replace")
                base_model = _find_base_model(script_text)
            except Exception:  # noqa: BLE001
                base_model = ""
            exp_name = cfg.get("config_id") or "LoRA finetune"
            if base_model:
                exp_name = f"{exp_name} · {base_model}"
            # Find or create the finetune experiment (goal: minimize loss).
            exp_id = None
            for e in store.list_experiments():
                if (e.get("name") or "").strip().lower() == exp_name.lower():
                    exp_id = e["id"]
                    break
            if exp_id is None:
                exp_id = store.create_experiment(
                    name=exp_name, goal_metric="loss", higher_better=False)

            series = job.get("series") or []
            last = series[-1] if series else {}
            # HF's final summary line uses train_loss instead of loss.
            if "loss" not in last and "train_loss" in last:
                last = dict(last); last["loss"] = last["train_loss"]
            # Metrics are summary scalars (numeric) for leaderboards.
            metrics = {}
            for k in ("loss", "grad_norm", "learning_rate", "epoch"):
                v = last.get(k)
                if v is not None:
                    metrics[k] = float(v)
            if job.get("step") is not None:
                metrics["step"] = int(job["step"])
            metrics["dataset"] = cfg.get("dataset_id") or ""
            config = {
                "job_id": job["id"],
                "config_id": cfg.get("config_id") or "",
                "dataset_id": cfg.get("dataset_id") or "",
                "base_model": base_model,
                "backend": cfg.get("backend") or "",
                "metric_series": series,
            }
            now = job.get("updated_at") or job.get("created_at") or time.time()
            if rid is None:
                # First time: create the run with the current snapshot.
                store.add_run(
                    prompt=("LoRA/QLoRA fine-tuning "
                            + (f"of {base_model} " if base_model else "")
                            + f"on dataset {cfg.get('dataset_id') or '?'}"),
                    reply=(f"Finetune job `{job['id']}` — "
                           + f"loss {metrics.get('loss', '—')} · "
                           + f"epoch {metrics.get('epoch', '—')}"),
                    status=job["status"], started_at=now, finished_at=now,
                    metrics=metrics, experiment_id=exp_id, config=config,
                    label=f"dk-lora:{job['id']}", kind="finetune",
                    model=base_model or None,
                    dataset=cfg.get("dataset_id") or None)
            else:
                store.set_run_metrics(
                    rid, metrics, config=config, status=job["status"],
                    finished_at=(now if job["status"] in ("done", "failed") else None))
        except Exception:  # noqa: BLE001
            pass

    def _persist(self, content: str, finetune_meta: dict) -> None:
        try:
            self.rt.store.add_message(
                "assistant", content,
                {"tags": ["finetune", "pipeline"], "finetune": finetune_meta})
        except Exception:  # noqa: BLE001
            pass

    async def _persist_start_or_progress(self, job: dict) -> None:
        step = job.get("step") or 0
        total = job.get("total") or 0
        prev = self._persisted_step.get(job["id"], -1)
        # Persist on job start (prev == -1) and then every N steps.
        if prev == -1 or step - prev >= _PROGRESS_EVERY:
            self._persisted_step[job["id"]] = step
            snap = fs.pipeline_snapshot()
            kind = "start" if prev == -1 else "progress"
            head = ("Training job started" if kind == "start"
                    else f"Training progress — step {step}/{total}")
            detail = []
            if job.get("last_loss") is not None:
                detail.append(f"loss={job['last_loss']}")
            if job.get("last_epoch") is not None:
                detail.append(f"epoch={job['last_epoch']}")
            if job.get("eta"):
                detail.append(f"eta={job['eta']}")
            suffix = f" · {' · '.join(detail)}" if detail else ""
            self._persist(
                f"**Finetune · {head}**{suffix}\n\n"
                f"Pipeline: {snap.get('pct')}% ({snap.get('message')}).",
                {"kind": kind, "job_id": job["id"], "pipeline": snap})

    async def _persist_terminal(self, job: dict) -> None:
        snap = fs.pipeline_snapshot()
        head = "Training job done" if job["status"] == "done" else "Training job failed"
        lines = [f"**Finetune · {head}** — `{job['id']}`"]
        if job.get("last_loss") is not None:
            lines.append(f"- Final loss: {job['last_loss']}")
        if job.get("last_epoch") is not None:
            lines.append(f"- Final epoch: {job['last_epoch']}")
        if job.get("error"):
            lines.append(f"- Error: {job['error']}")
        out_dir = (job.get("output_dir") or "").strip()
        if out_dir:
            lines.append(f"- Adapter: `{out_dir}`")
        lines.append(f"- Pipeline: {snap.get('pct')}% ({snap.get('message')}).")
        # Fold in the last debug lines so the terminal message doubles as a log
        # tail without flooding history.
        raw = fs.read_json(fs.jobs_dir() / f"{job['id']}.json")
        if raw is not None:
            parsed = fs.parse_log(fs.job_log_path(raw), n_chars=1200)
            tail = [l for l in parsed["log_tail"].splitlines() if l.strip()][-8:]
            if tail:
                lines.append("\n```\n" + "\n".join(tail) + "\n```")
        self._persist("\n".join(lines),
                      {"kind": job["status"], "job_id": job["id"],
                       "pipeline": snap})
