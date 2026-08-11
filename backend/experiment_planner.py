"""Robust deterministic experiment planner, creator, generator & manager.

Lifecycle of a plan (per project, persisted as JSON in the project dir):

    DRAFT -> WAITING_APPROVAL -> APPROVED -> RUNNING -> DONE / FAILED / REJECTED

Flow
----
1. The user (or the experiment-planner MCP) asks for an experiment. A
   deterministic ``plan_experiment(...)`` builds a concrete plan: experiment id,
   dataset, seed, steps (what will run + what outputs are expected), and an
   estimated cost/size. Nothing is executed yet.
2. The plan is **proposed to the user in the chat** (an ``experiment_plan_proposal``
   event / plan card). The user must confirm before anything runs.
3. On approval, the plan is executed deterministically (registered experiments
   expose ``run_experiment(df) -> dict``, ``render_report(res) -> md`` and
   ``render_figures(res) -> {name: png_bytes}``). Results become workbench
   artifacts and a run is recorded.
4. The result is presented back to the chat (KPI summary + figures + report).

The whole thing is deterministic and inspectable: the same plan id always maps
to the same proposal and, given the same seed + dataset, the same result.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
import zlib
from pathlib import Path


# ---------------------------------------------------------------- registry ----
# Registered deterministic experiments: id -> definition. Each entry:
#   name, description, needs_dataset, plan_steps(request)->[str],
#   run(df, seed)->res, render_report(res)->str, render_figures(res)->dict
EXPERIMENT_REGISTRY: dict = {}

def register_experiment(defn: dict) -> None:
    eid = defn.get("id") or defn.get("name")
    if not eid:
        raise ValueError("experiment needs an id")
    EXPERIMENT_REGISTRY[eid] = defn


def list_experiments() -> list[dict]:
    return [
        {"id": eid, "name": d.get("name", eid),
         "description": d.get("description", ""),
         "needs_dataset": bool(d.get("needs_dataset")),
         "requires_columns": d.get("requires_columns") or [],
         "goal_metric": d.get("goal_metric") or "",
         "higher_better": bool(d.get("higher_better", True)),
         "seed_sensitive": bool(d.get("seed_sensitive"))}
        for eid, d in sorted(EXPERIMENT_REGISTRY.items())
    ]


# ------------------------------------------------------------ dataset io -----
DATASET_SUFFIXES = (".csv", ".parquet", ".xlsx", ".xls")


def is_dataset_file(name: str | Path) -> bool:
    return Path(name).suffix.lower() in DATASET_SUFFIXES


def load_dataset(path: Path):
    """Load a project dataset into a DataFrame by extension (csv/parquet/xlsx)."""
    import pandas as pd
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path, low_memory=False)


def peek_dataset(path: Path, n: int = 1):
    """Cheap first-N-rows read of a dataset (for column validation/preview)."""
    import pandas as pd
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path).head(n)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, nrows=n)
    return pd.read_csv(path, nrows=n, low_memory=False)


def generate_synthetic_upi(path: Path, seed: int = 42, n: int = 3000) -> Path:
    """Deterministic synthetic UPI transaction dataset, for projects with no
    real dataset yet. Mirrors the columns the banking / privacy / re-id
    experiments expect so every plan can run."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    banks = ["HDFC", "SBI", "ICICI", "Axis", "Kotak", "Yes"]
    segments = ["retail", "dining", "grocery", "fuel", "travel", "bills"]
    ptypes = ["UPI", "IMPS", "NEFT"]
    ages = ["18-25", "26-35", "36-45", "46-60", "60+"]
    states = ["KA", "MH", "DL", "TN", "UP", "GJ"]
    devices = ["android", "ios", "web"]
    networks = ["jio", "airtel", "vi", "wifi"]
    df = pd.DataFrame({
        "sender_bank": rng.choice(banks, n),
        "merchant_category": rng.choice(segments, n),
        "transaction type": rng.choice(ptypes, n, p=[0.6, 0.25, 0.15]),
        "amount (INR)": np.round(rng.lognormal(6.5, 0.8, n), 2),
        "sender_age_group": rng.choice(ages, n),
        "sender_state": rng.choice(states, n),
        "device_type": rng.choice(devices, n),
        "network_type": rng.choice(networks, n),
        "hour_of_day": rng.integers(0, 24, n),
    })
    df["email"] = [f"u{i}@mail.com" for i in range(n)]
    df["phone"] = [f"+91{i:010d}" for i in range(n)]
    df.to_csv(path, index=False)
    return path


def ensure_runnable_dataset(project_dir: Path, dataset: str = "") -> tuple[str, bool]:
    """Pick the requested (or first real) dataset in the project — preferring
    UPI/bank-named files; if none exists, generate a deterministic synthetic UPI
    dataset so a plan can run. Returns (filename, is_synthetic)."""
    if dataset:
        cand = project_dir / dataset
        if cand.exists():
            return dataset, False
    cands = sorted(p for p in project_dir.iterdir()
                   if p.is_file() and is_dataset_file(p.name)
                   and not p.name.lower().startswith("synthetic_"))
    if cands:
        upi = [p for p in cands
               if "upi" in p.name.lower() or "bank" in p.name.lower()]
        return (upi or cands)[0].name, False
    out = project_dir / "synthetic_upi_transactions.csv"
    if not out.exists():
        generate_synthetic_upi(out)
    return out.name, True


def dataset_hash(path: Path) -> str:
    """SHA-256 fingerprint of a dataset file (streaming, whole file).

    Pins a plan/result to the exact data it was created/run against so metrics
    deltas are never confounded by silent data edits. Returns '' if the file is
    unreadable.
    """
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def _with_file_lock(path: Path, fn):
    """Run ``fn()`` under an OS-level exclusive lock so separate processes
    (e.g. the backend REST host and the experiment-planner MCP) can't corrupt
    or lose each other's read-modify-write cycles on the same JSON store."""
    lock_path = path.with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ------------------------------------------------------------- plan store -----
PLAN_STATUSES = ("DRAFT", "WAITING_APPROVAL", "APPROVED", "RUNNING",
                 "DONE", "FAILED", "REJECTED")


class PlanStore:
    """Per-project plan store backed by <project>/experiment_plans.json.

    Reads are lock-free (writes are atomic via rename, so a reader always sees a
    complete file). Every read-modify-write cycle goes through ``_mutate`` which
    holds a process-wide lock so concurrent background runs / route calls can't
    lose each other's updates.
    """

    def __init__(self, project_dir: Path):
        self.path = project_dir / "experiment_plans.json"
        self._lock = threading.RLock()

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return {"plans": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"plans": {}}

    def _load(self) -> dict:
        return self._read_raw()

    def _atomic_write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(self.path)

    def _mutate(self, fn):
        """Apply ``fn(data)`` atomically under the store lock.

        ``fn`` may mutate ``data`` in place and/or return a new value; the
        written data is ``fn``'s return value when not None, else the (possibly
        mutated) input. The mutator's own return value is passed through so
        callers like ``delete`` / ``recover_interrupted`` can use it.

        Locking is two-tier: a process-local ``RLock`` (re-entrancy) plus an
        OS-level ``flock`` so the separate MCP process and the REST host can't
        lose each other's updates on the shared JSON file.
        """
        with self._lock:
            return _with_file_lock(self.path, lambda: self._read_and_write(fn))

    def _read_and_write(self, fn):
        data = self._read_raw()
        out = fn(data)
        if out is not None:
            data = out
        self._atomic_write(data)
        return out

    def create(self, experiment_id: str, request: str, dataset: str = "",
               seed: int | None = None, parent_id: str = "",
               lineage: list[str] | None = None) -> dict:
        defn = EXPERIMENT_REGISTRY.get(experiment_id)
        if defn is None:
            raise ValueError(f"unknown experiment '{experiment_id}' — "
                             f"available: {list(EXPERIMENT_REGISTRY)}")
        if defn.get("needs_dataset") and not dataset:
            raise ValueError(f"experiment '{experiment_id}' needs a dataset file")
        # Validate the dataset file exists + required columns, if we can.
        if dataset:
            ds_path = self.path.parent / dataset
            if not ds_path.exists():
                raise ValueError(f"dataset file not found: {dataset}")
            req_cols = defn.get("requires_columns") or []
            if req_cols:
                try:
                    head = peek_dataset(ds_path, n=1)
                    missing = [c for c in req_cols if c not in head.columns]
                    if missing:
                        raise ValueError(
                            f"dataset '{dataset}' is missing required column(s): "
                            f"{', '.join(missing)}")
                except ValueError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
        plan_id = uuid.uuid4().hex[:12]
        # Default seed is derived from the request (not wall-clock) so identical
        # requests + datasets reproduce identically; explicit seeds still win.
        seed_source = "explicit"
        if seed is None:
            seed = derive_seed(experiment_id, dataset, request or "")
            seed_source = "derived"
        steps_def = defn.get("plan_steps") or []
        steps = list(steps_def(request or "", dataset)) if callable(steps_def) else list(steps_def)
        expected_def = defn.get("expected_outputs") or []
        expected = (list(expected_def(request or "", dataset))
                    if callable(expected_def) else list(expected_def))
        plan = {
            "id": plan_id,
            "experiment_id": experiment_id,
            "name": defn.get("name", experiment_id),
            "description": defn.get("description", ""),
            "request": request or "",
            "dataset": dataset,
            "dataset_hash": dataset_hash(ds_path) if dataset else "",
            "parent_id": parent_id or "",
            "lineage": list(lineage or []),
            "seed": seed,
            "seed_source": seed_source,
            "steps": steps,
            "expected_outputs": expected,
            "status": "DRAFT",
            "created_at": time.time(),
            "updated_at": time.time(),
            "approval": None,
            "result": None,
            "error": None,
            "artifact_ids": [],
            "metrics": None,
            "_project_dir": str(self.path.parent),
        }
        self._mutate(lambda data: data["plans"].__setitem__(plan_id, plan))
        return plan

    def get(self, plan_id: str) -> dict | None:
        return self._load()["plans"].get(plan_id)

    def update(self, plan_id: str, **fields) -> dict:
        result: dict = {}

        def _upd(data):
            plan = data["plans"].get(plan_id)
            if plan is None:
                raise ValueError(f"plan not found: {plan_id}")
            plan.update(fields)
            plan["updated_at"] = time.time()
            result["plan"] = plan
            return data
        self._mutate(_upd)
        return result["plan"]

    def list(self, status: str | None = None) -> list[dict]:
        plans = list(self._load()["plans"].values())
        plans.sort(key=lambda p: p.get("created_at", 0))
        if status:
            plans = [p for p in plans if p.get("status") == status]
        return plans

    def delete(self, plan_id: str) -> bool:
        def _del(data):
            if plan_id not in data["plans"]:
                return None
            del data["plans"][plan_id]
            return data
        return self._mutate(_del) is not None

    # ---------------------------------------------- suggestion state -----
    def dismissed_suggestions(self) -> set[str]:
        """Stable suggestion ids the user has dismissed in this project."""
        return set(self._read_raw().get("dismissed_suggestions", []))

    def dismiss_suggestion(self, suggestion_id: str) -> bool:
        """Persist a dismissed suggestion id. Returns False if already present."""
        def _d(data):
            cur = set(data.get("dismissed_suggestions", []))
            if suggestion_id in cur:
                return None
            data["dismissed_suggestions"] = sorted(cur | {suggestion_id})
            return data
        return self._mutate(_d) is not None

    def recover_interrupted(self, grace: float = 30.0) -> int:
        """Mark plans a previous process left RUNNING as FAILED (interrupted).

        Run at startup: a persisted ``RUNNING`` plan whose ``started_at`` is
        older than ``grace`` seconds can only be a run killed by a restart.
        Returns how many plans were recovered.
        """
        now = time.time()

        counts = {"n": 0}

        def _recover(data):
            for p in data["plans"].values():
                started = p.get("started_at") or p.get("updated_at") or 0
                if p.get("status") == "RUNNING" and now - started > grace:
                    p["status"] = "FAILED"
                    p["error"] = "interrupted by server restart"
                    p["updated_at"] = now
                    counts["n"] += 1
            return data
        self._mutate(_recover)
        return counts["n"]

    def propose(self, plan_id: str) -> dict:
        """Move DRAFT -> WAITING_APPROVAL and return the proposal payload."""
        plan = self.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        if plan["status"] != "DRAFT":
            raise ValueError(f"plan is '{plan['status']}', not DRAFT")
        return self.update(plan_id, status="WAITING_APPROVAL")

    def decide(self, plan_id: str, approve: bool, by: str = "") -> dict:
        """Approve or reject a WAITING_APPROVAL plan."""
        plan = self.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        if plan["status"] != "WAITING_APPROVAL":
            raise ValueError(f"plan is '{plan['status']}', not WAITING_APPROVAL")
        return self.update(
            plan_id,
            status="APPROVED" if approve else "REJECTED",
            approval={"approved": bool(approve), "by": by or "user",
                      "at": time.time()})

    def repropose(self, plan_id: str, *, dataset: str | None = None,
                  seed: int | None = None, request: str | None = None) -> dict:
        """Edit + re-propose a rejected/cancelled plan (back to DRAFT ->
        WAITING_APPROVAL). Keeps the plan id so history is traceable."""
        plan = self.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        if plan["status"] not in ("REJECTED", "DRAFT", "FAILED"):
            raise ValueError(f"plan is '{plan['status']}' — only rejected/failed "
                             "plans can be re-proposed")
        fields = {"status": "DRAFT", "approval": None, "result": None,
                  "error": None, "metrics": None, "artifact_ids": [],
                  "started_at": None}
        new_ds = plan.get("dataset", "")
        if dataset is not None:
            fields["dataset"] = dataset
            new_ds = dataset
        if seed is not None:
            fields["seed"] = seed
            fields["seed_source"] = "explicit"
        elif plan.get("seed_source") == "derived" and (
                (request is not None and request != plan.get("request", ""))
                or new_ds != plan.get("dataset", "")):
            # Input changed and the seed was derived from it -> re-derive so the
            # re-proposed plan reproduces the NEW input deterministically.
            fields["seed"] = derive_seed(plan["experiment_id"], new_ds,
                                         request if request is not None
                                         else plan.get("request", ""))
            fields["seed_source"] = "derived"
        if request is not None:
            fields["request"] = request
            defn = EXPERIMENT_REGISTRY.get(plan["experiment_id"]) or {}
            sd = defn.get("plan_steps") or []
            fields["steps"] = (list(sd(request or "", new_ds))
                               if callable(sd) else list(sd))
        plan = self.update(plan_id, **fields)
        return self.propose(plan_id)

    def clone(self, plan_id: str, *, seed: int | None = None,
              dataset: str | None = None, request: str | None = None) -> dict:
        """Clone a plan into a fresh DRAFT (for re-run / variant with new seed).

        The clone records ``parent_id`` + ``lineage`` so run chains (e.g. the
        seed-verify and post-clean re-runs suggested by the planner) are
        traceable back to their source plan.
        """
        src = self.get(plan_id)
        if src is None:
            raise ValueError(f"plan not found: {plan_id}")
        lineage = (src.get("lineage") or []) + [plan_id]
        return self.create(
            experiment_id=src["experiment_id"],
            request=request if request is not None else src.get("request", ""),
            dataset=dataset if dataset is not None else src.get("dataset", ""),
            seed=seed if seed is not None else src.get("seed"),
            parent_id=plan_id,
            lineage=lineage)


# ------------------------------------------------------------ execution -------
def derive_seed(experiment_id: str, dataset: str, request: str = "") -> int:
    """Deterministic content-derived seed for a plan request.

    Identical (experiment, dataset, request) always produce the same seed, so a
    plan re-created for the same request reproduces the same result. Callers can
    still override with an explicit seed.
    """
    return zlib.crc32(f"{experiment_id}|{dataset}|{request}".encode("utf-8"))


def plan_result_dir(project_dir: Path, plan_id: str) -> Path:
    """Where a plan's persistent outputs (report + figures) are stored."""
    d = project_dir / "plans" / plan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def execute_plan(plan: dict, df, project_dir: Path | None = None,
                 progress=None, timeout: float = 300.0) -> dict:
    """Run an approved plan deterministically. Returns an updated plan dict
    (status DONE/FAILED, result, metrics). ``progress(step_index, message)``
    receives per-step updates during execution. When ``project_dir`` is given,
    figures + report are persisted under <project>/plans/<id>/ so a DONE plan
    survives restart and can be re-presented.

    The experiment body runs on a worker thread so a hung / pathological dataset
    can't wedge the caller: if it doesn't finish within ``timeout`` seconds the
    plan is marked FAILED("timed out …"). Note the worker thread is *not*
    killable once started — it is left to finish in the background, but nothing
    it returns is ever persisted or presented.
    """
    if plan.get("status") not in ("APPROVED", "RUNNING"):
        raise ValueError(f"plan is '{plan.get('status')}' — approve it first")
    defn = EXPERIMENT_REGISTRY.get(plan["experiment_id"])
    if defn is None:
        raise ValueError(f"unknown experiment '{plan.get('experiment_id')}'")
    plan = dict(plan)
    plan["status"] = "RUNNING"
    plan["started_at"] = time.time()
    # Pin the exact data this run is computed against (may differ from the hash
    # recorded at plan creation if the file changed in between).
    if project_dir is not None and plan.get("dataset"):
        plan["dataset_hash"] = dataset_hash(project_dir / plan["dataset"])
    steps = plan.get("steps") or []
    if progress:
        for i, s in enumerate(steps):
            await_progress(progress, i, f"{i + 1}/{len(steps)} {s}")

    def _work():
        res = defn["run"](df, seed=plan.get("seed"))
        report_md = defn["render_report"](res)
        figures = (defn["render_figures"](res)
                   if "render_figures" in defn else {})
        return res, report_md, figures

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(_work)
        res, report_md, figures = fut.result(timeout=timeout)
    except _Timeout:
        plan.update({"status": "FAILED", "result": None, "metrics": None,
                     "error": f"timed out after {timeout}s"})
        executor.shutdown(wait=False)
        return plan
    except Exception as e:  # noqa: BLE001
        plan.update({"status": "FAILED", "result": None, "metrics": None,
                     "error": f"{type(e).__name__}: {e}"})
        executor.shutdown(wait=False)
        return plan
    executor.shutdown(wait=True)
    try:
        metrics = res.get("metrics") or _default_metrics(res)

        # Persist figures + report when a project dir is provided.
        persisted = []
        if project_dir is not None:
            out_dir = plan_result_dir(project_dir, plan["id"])
            for name_, data in figures.items():
                (out_dir / name_).write_bytes(data)
                persisted.append(name_)
            if report_md:
                (out_dir / "report.md").write_text(report_md, encoding="utf-8")
                persisted.append("report.md")

        plan.update({
            "status": "DONE",
            "result": {"report": report_md, "figures": list(figures),
                       "persisted": persisted, "n": res.get("n")},
            "metrics": metrics,
            "error": None,
        })
        if project_dir is not None:
            plan["result_dir"] = str(plan_result_dir(project_dir, plan["id"]))
        plan["_figures_bytes"] = figures
        plan["_report_md"] = report_md
        if progress:
            await_progress(progress, len(steps), "done")
        return plan
    except Exception as e:  # noqa: BLE001
        plan.update({"status": "FAILED", "result": None, "metrics": None,
                     "error": f"{type(e).__name__}: {e}"})
        return plan


def await_progress(progress, i, message):
    """Call a progress callback if it's async or sync.

    Coroutine callbacks are only scheduled when a running loop exists in this
    thread (main-thread async callers). Cross-thread scheduling — e.g. a sync
    progress closure created in an async handler and invoked from an
    ``asyncio.to_thread`` worker — is the caller's job (see
    ``routers.experiment_planner.present_result``).
    """
    if progress is None:
        return
    try:
        import asyncio
        if asyncio.iscoroutinefunction(progress):
            try:
                asyncio.get_running_loop().create_task(progress(i, message))
            except RuntimeError:
                pass  # no running loop in this thread — skip rather than crash
        else:
            progress(i, message)
    except Exception:  # noqa: BLE001
        pass


def _default_metrics(res: dict) -> dict:
    out = {}
    for k, v in (res or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 6)
    return out


# ------------------------------------------------------- suggestions --------
# Incremental suggestions: given prior plans (DONE + FAILED), recommend the next
# experiment to run and concrete follow-up actions. Rule-based and deterministic
# so results are reproducible and explainable. Key behaviours:
#   - new dataset / no DONE runs  -> EDA onboarding first
#   - finding-driven follow-ups (PII -> reid/dp, re-id -> dp, corr -> anomaly,
#     anomalies -> clean, dp-error -> clean/anomaly)
#   - failure-aware: an experiment whose last attempt FAILED is not re-suggested
#     for coverage (a low-score notice points at the error instead)
#   - remediation-driven: after a DONE `clean`, re-run affected experiments to
#     confirm improvement; compare pre/post metrics when both exist
#   - seed-sensitivity: single-run stochastic experiments get a "verify with a
#     different seed" suggestion
#   - coverage across the WHOLE registry (eda/clean/peer included), derived from
#     each experiment's goal_metric rather than a hardcoded list

_EXPERIMENT_ORDER = ("eda", "pii_scan", "reid_risk", "dp_privacy",
                     "correlation", "anomaly", "clean", "peer")

# Experiments whose outputs depend on the RNG seed (worth verifying via a second
# seed rather than trusting a single run). Computed from each definition's
# ``seed_sensitive`` flag at call time so the registry can grow.
def _seed_sensitive_ids() -> set[str]:
    return {eid for eid, d in EXPERIMENT_REGISTRY.items()
            if d.get("seed_sensitive")}

# Goal metrics the remediation rules look at, per experiment.
_FINDING_METRICS = {
    "pii_scan": "pii_columns",
    "reid_risk": "k_anonymity_1",
    "dp_privacy": "min_mae",
    "anomaly": "outlier_cols",
    "correlation": "max_abs_corr",
    "clean": "affected_rows",
    "peer": "identification_accuracy",
}

_AFFECTED_BY_CLEAN = ("dp_privacy", "anomaly", "correlation", "reid_risk",
                      "peer")


def _plan_order(p: dict) -> tuple:
    """Deterministic ordering key for a plan: time then id, so same-second runs
    never tie-break on set/dict iteration order."""
    return (p.get("updated_at", 0), p.get("created_at", 0), p.get("id", ""))


def _run_latest(plans: list[dict], statuses=("DONE",)) -> dict:
    """Latest plan per (dataset, experiment_id) with a status in ``statuses``."""
    out = {}
    for p in (plans or []):
        if p.get("status") not in statuses:
            continue
        key = (p.get("dataset"), p.get("experiment_id"))
        prev = out.get(key)
        if prev is None or _plan_order(p) > _plan_order(prev):
            out[key] = p
    return out


def _plan_hint(experiment_id: str) -> dict | None:
    defn = EXPERIMENT_REGISTRY.get(experiment_id)
    if defn is None:
        return None
    return {"id": experiment_id, "name": defn.get("name", experiment_id),
            "description": defn.get("description", ""),
            "needs_dataset": defn.get("needs_dataset", True)}


def _suggestion(experiment_id, score, reason, based_on=None, action="plan",
                evidence=None, suggested_seed=None):
    hint = _plan_hint(experiment_id)
    if hint is None:
        return None
    out = {**hint, "score": score, "reason": reason,
           "based_on": based_on or [], "action": action,
           "evidence": evidence or []}
    # Stable, content-addressed id so the UI can dismiss a specific suggestion
    # and the same (rule, experiment, dataset, reason) maps to the same id.
    out["suggestion_id"] = suggestion_key(experiment_id,
                                          (based_on or [""])[0],
                                          reason)
    if suggested_seed is not None:
        out["suggested_seed"] = suggested_seed
    return out


def suggestion_key(experiment_id: str, dataset: str, reason: str) -> str:
    """Deterministic id for a suggestion (for dismissal state)."""
    import hashlib
    return hashlib.sha256(
        f"{experiment_id}|{dataset}|{reason}".encode("utf-8")).hexdigest()[:16]


def build_suggestions(plans: list[dict],
                      datasets: list[str] | None = None,
                      dismissed: set[str] | None = None) -> list[dict]:
    """Derive ranked, incremental next-step suggestions from prior runs.

    ``datasets`` optionally lists dataset files present in the project but never
    planned (cold-start): each gets an EDA-onboarding suggestion so a freshly
    uploaded CSV isn't invisible to the planner. ``dismissed`` is a set of
    stable ``suggestion_id`` values the user has dismissed — those are filtered
    out so a suggestion the user acted on / rejected doesn't nag forever.
    """
    plans = plans or []
    done = _run_latest(plans, ("DONE",))
    attempts = _run_latest(plans, ("DONE", "FAILED", "REJECTED"))
    suggestions: list[dict] = []
    seen: set = set()
    dismissed = dismissed or set()

    planned_ds = {p.get("dataset") for p in plans if p.get("dataset")}
    all_ds = sorted(planned_ds | set(datasets or []))
    for ds in all_ds:
        ds_plans = [p for p in plans if p.get("dataset") == ds]

        # 0) Cold-start: a dataset with zero plans (freshly uploaded) -> EDA.
        if not ds_plans:
            s = _suggestion("eda", 4,
                f"`{ds}` is present in the project but hasn't been analyzed "
                "yet — start with an EDA overview to profile shape, missing "
                "values and duplicates.",
                based_on=[ds])
            if s: suggestions.append(s)
            continue

        done_ids = {k[1] for k in done if k[0] == ds}
        m = {k[1]: (done.get((ds, k[1])) or {}).get("metrics") or {}
             for k in done if k[0] == ds}
        failed_ids = {k[1] for k in attempts if k[0] == ds
                      and attempts[(k[0], k[1])].get("status") == "FAILED"}

        # 0b) Onboarding: a dataset that has plans but no DONE run -> EDA.
        if not done_ids:
            s = _suggestion("eda", 4,
                f"`{ds}` has no completed runs yet — start with an EDA overview "
                "to profile shape, missing values and duplicates.",
                based_on=[ds])
            if s: suggestions.append(s); seen.add(("eda", ds))

        # 1) PII found -> run reid + dp on the same dataset.
        pii = m.get("pii_scan", {})
        if pii.get("pii_columns", 0) > 0:
            for eid in ("reid_risk", "dp_privacy"):
                if eid not in done_ids and (eid, ds) not in seen:
                    s = _suggestion(eid, 5,
                        f"PII scan on `{ds}` found {pii['pii_columns']} "
                        "identifier-like columns — "
                        + ("run re-identification risk to quantify uniqueness."
                           if eid == "reid_risk"
                           else "evaluate DP protection for numeric aggregates."),
                        based_on=[ds, "pii_scan"])
                    if s: suggestions.append(s); seen.add((eid, ds))

        # 2) Re-identification risk high -> DP protection.
        reid = m.get("reid_risk", {})
        k1 = reid.get("k_anonymity_1", 0) or 0
        if k1 > 0.02 and "dp_privacy" not in done_ids \
                and ("dp_privacy", ds) not in seen:
            s = _suggestion("dp_privacy", 4,
                f"Re-id risk on `{ds}` shows {k1:.1%} of rows uniquely "
                "identifiable — DP on numeric aggregates mitigates inference.",
                based_on=[ds, "reid_risk"])
            if s: suggestions.append(s); seen.add(("dp_privacy", ds))

        # 3) Strong correlations -> anomaly (outliers distort correlations).
        corr = m.get("correlation", {})
        mc = corr.get("max_abs_corr", 0) or 0
        if mc > 0.5 and "anomaly" not in done_ids \
                and ("anomaly", ds) not in seen:
            s = _suggestion("anomaly", 3,
                f"Correlation on `{ds}` peaks at |r|={mc:.2f} — outliers can "
                "inflate that; check anomalies first.",
                based_on=[ds, "correlation"])
            if s: suggestions.append(s); seen.add(("anomaly", ds))

        # 4) Anomalies found -> a concrete clean/remediation plan.
        anom = m.get("anomaly", {})
        if anom.get("outlier_cols", 0) > 0 and "clean" not in done_ids \
                and ("clean", ds) not in seen:
            s = _suggestion("clean", 4,
                f"Anomalies on `{ds}` (in {anom['outlier_cols']} column(s)) — "
                "run the cleaning plan to quantify the remediation "
                "(dedupe + nulls + outlier impact).",
                based_on=[ds, "anomaly"])
            if s: suggestions.append(s); seen.add(("clean", ds))

        # 5) DP error high -> anomaly / clean first (outliers raise sensitivity).
        dp = m.get("dp_privacy", {})
        if dp.get("min_mae") is not None and dp["min_mae"] > 1.0:
            if "anomaly" not in done_ids and ("anomaly", ds) not in seen:
                s = _suggestion("anomaly", 3,
                    f"DP mean error on `{ds}` is high (MAE {dp['min_mae']:.2f}) "
                    "— outliers inflate sensitivity; check them first.",
                    based_on=[ds, "dp_privacy"])
                if s: suggestions.append(s); seen.add(("anomaly", ds))
            elif "clean" not in done_ids and ("clean", ds) not in seen:
                s = _suggestion("clean", 3,
                    f"DP mean error on `{ds}` is high (MAE {dp['min_mae']:.2f}) "
                    "— run the cleaning plan and re-run DP after remediation.",
                    based_on=[ds, "dp_privacy"])
                if s: suggestions.append(s); seen.add(("clean", ds))

        # 6) After a DONE clean -> re-run affected experiments to confirm the
        #    improvement (delta analysis), or note the confirmed improvement.
        if "clean" in done_ids:
            clean_t = (done.get((ds, "clean")) or {}).get("updated_at", 0)
            for eid in _AFFECTED_BY_CLEAN:
                if eid not in done_ids:
                    continue
                run = done.get((ds, eid))
                before = [p for p in (plans or [])
                          if p.get("dataset") == ds
                          and p.get("experiment_id") == eid
                          and p.get("status") == "DONE"
                          and (p.get("updated_at") or 0) < clean_t]
                after = [p for p in (plans or [])
                         if p.get("dataset") == ds
                         and p.get("experiment_id") == eid
                         and p.get("status") == "DONE"
                         and (p.get("updated_at") or 0) >= clean_t]
                if not after and before and (eid, ds) not in seen:
                    gm = _FINDING_METRICS.get(eid)
                    prev_v = (before[-1].get("metrics") or {}).get(gm) \
                        if gm else None
                    s = _suggestion(eid, 3,
                        f"`{ds}` was cleaned — re-run "
                        f"{EXPERIMENT_REGISTRY[eid]['name']} to confirm the "
                        f"post-clean metrics"
                        + (f" (was {prev_v:.4g})." if isinstance(prev_v, (int, float)) else "."),
                        based_on=[ds, "clean", eid],
                        action="clone")
                    if s: suggestions.append(s); seen.add((eid, ds))
                elif after and before and (eid, ds) not in seen:
                    gm = _FINDING_METRICS.get(eid)
                    defn = EXPERIMENT_REGISTRY.get(eid) or {}
                    hb = bool(defn.get("higher_better", True))
                    if gm:
                        b_last = before[-1]
                        a_last = after[-1]
                        bv = (b_last.get("metrics") or {}).get(gm)
                        av = (a_last.get("metrics") or {}).get(gm)
                        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
                            same_data = (b_last.get("dataset_hash")
                                         == a_last.get("dataset_hash"))
                            improved = (av < bv) if not hb else (av > bv)
                            if same_data:
                                s = _suggestion(eid, 2,
                                    f"`{ds}`: {EXPERIMENT_REGISTRY[eid]['name']} "
                                    f"{'improved' if improved else 'did NOT improve'} "
                                    f"after cleaning ({gm}: {bv:.4g} → {av:.4g}) — "
                                    + ("tracked in the run history."
                                       if improved else "check whether the "
                                       "remediation is the right fix."),
                                    based_on=[ds, "clean", eid])
                            else:
                                s = _suggestion(eid, 2,
                                    f"`{ds}`: {EXPERIMENT_REGISTRY[eid]['name']} "
                                    f"changed ({gm}: {bv:.4g} → {av:.4g}) but the "
                                    "dataset was edited between runs — the delta "
                                    "isn't attributable to cleaning alone.",
                                    based_on=[ds, "clean", eid])
                            if s:
                                suggestions.append(s); seen.add((eid, ds))

        # 7) Seed-sensitivity: single-run stochastic experiments -> verify with
        #    a different seed before trusting the number (skip when the latest
        #    attempt failed — repropose first).
        for eid in _seed_sensitive_ids():
            if eid not in done_ids or eid in failed_ids \
                    or (eid, ds) in seen:
                continue
            runs = [p for p in (plans or [])
                    if p.get("dataset") == ds
                    and p.get("experiment_id") == eid
                    and p.get("status") == "DONE"]
            gm = _FINDING_METRICS.get(eid)
            vals = [(r.get("metrics") or {}).get(gm) for r in runs if gm]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if len(runs) == 1:
                run = done.get((ds, eid))
                alt_seed = derive_seed(eid, ds, "verify#2")
                s = _suggestion(eid, 3,
                    f"`{ds}` {EXPERIMENT_REGISTRY[eid]['name']} has run once "
                    "(seed-sensitive) — clone with a new seed to verify the "
                    "result is stable.",
                    based_on=[ds, eid], action="clone",
                    suggested_seed=alt_seed,
                    evidence=[run.get("id")] if run else [])
                if s: suggestions.append(s); seen.add((eid, ds))
            elif len(vals) >= 2:
                mn, mx = min(vals), max(vals)
                spread = mx - mn
                rel = spread / max(abs(mx), 1e-9)
                if rel > 0.5:
                    s = _suggestion(eid, 3,
                        f"`{ds}` {EXPERIMENT_REGISTRY[eid]['name']} is unstable "
                        f"across {len(runs)} seeds ({gm}: {mn:.4g} … {mx:.4g}, "
                        f"span {spread:.4g}) — investigate the sensitivity "
                        "before drawing conclusions.",
                        based_on=[ds, eid])
                    if s: suggestions.append(s); seen.add((eid, ds))

        # 8) Coverage across the whole registry, failure-aware.
        for eid in _EXPERIMENT_ORDER:
            if eid in done_ids or (eid, ds) in seen:
                continue
            defn = EXPERIMENT_REGISTRY.get(eid)
            if defn is None or not defn.get("needs_dataset"):
                continue
            if eid in failed_ids:
                run = attempts.get((ds, eid)) or {}
                s = _suggestion(eid, 1,
                    f"`{ds}` {defn['name']} last attempt failed — check the "
                    "plan error / repropose before retrying.",
                    based_on=[ds, eid], action="repropose",
                    evidence=[run.get("id")] if run else [])
            else:
                s = _suggestion(eid, 2,
                    f"`{ds}` hasn't had a {defn['name']} run yet — add it for "
                    "full scenario coverage.",
                    based_on=[ds])
            if s: suggestions.append(s); seen.add((eid, ds))

        # 9) Experiments with a DONE history whose latest attempt failed: don't
        #    re-suggest them (clone/coverage would be misleading) — surface the
        #    failure once so the user reproposes instead.
        for eid in failed_ids:
            if (eid, ds) in seen:
                continue
            defn = EXPERIMENT_REGISTRY.get(eid)
            if defn is None:
                continue
            run = attempts.get((ds, eid)) or {}
            s = _suggestion(eid, 1,
                f"`{ds}` {defn['name']} last attempt failed "
                f"({str(run.get('error'))[:100]}) — check the plan error and "
                "re-propose before retrying.",
                based_on=[ds, eid], action="repropose",
                evidence=[run.get("id")] if run else [])
            if s: suggestions.append(s); seen.add((eid, ds))

    # 10) Cross-dataset insight: for each experiment with a declared directional
    #     goal metric run on >=2 datasets, compare the latest value per dataset
    #     and surface the dataset that deviates most (the investigation target).
    _cross_dataset(plans, suggestions)

    # Tie-break deterministically: higher score first, then catalog order, then
    # name — never insertion order. Dismissed suggestions are dropped.
    order = {e: i for i, e in enumerate(_EXPERIMENT_ORDER)}
    suggestions.sort(key=lambda s: (-s.get("score", 0),
                                    order.get(s.get("id"), 99),
                                    s.get("name", "")))
    return [s for s in suggestions
            if s.get("suggestion_id") not in dismissed][:10]


def _cross_dataset(plans, suggestions):
    """Append a cross-dataset comparison suggestion per experiment when the
    latest goal-metric values differ materially across datasets."""
    if not plans:
        return
    by_exp: dict = {}
    for p in plans:
        if p.get("status") != "DONE" or not p.get("dataset"):
            continue
        eid = p.get("experiment_id")
        defn = EXPERIMENT_REGISTRY.get(eid) or {}
        gm = _FINDING_METRICS.get(eid)
        if not gm or defn.get("higher_better") is None:
            continue
        bucket = by_exp.setdefault(eid, {})
        prev = bucket.get(p["dataset"])
        if prev is None or p.get("updated_at", 0) > prev.get("updated_at", 0):
            bucket[p["dataset"]] = p
    for eid in _EXPERIMENT_ORDER:
        bucket = by_exp.get(eid)
        if not bucket or len(bucket) < 2:
            continue
        defn = EXPERIMENT_REGISTRY.get(eid)
        gm = _FINDING_METRICS.get(eid)
        hb = bool(defn.get("higher_better", True))
        rows = sorted(bucket.items())
        vals = [(ds, (p.get("metrics") or {}).get(gm))
                for ds, p in rows if isinstance((p.get("metrics") or {}).get(gm),
                                                (int, float))]
        if len(vals) < 2:
            continue
        best = min(vals, key=lambda x: x[1]) if not hb else max(vals, key=lambda x: x[1])
        worst = max(vals, key=lambda x: x[1]) if not hb else min(vals, key=lambda x: x[1])
        span = abs(worst[1] - best[1])
        if span > 0 and span / max(abs(worst[1]), 1e-9) > 0.5:
            s = _suggestion(eid, 2,
                f"`{eid}` on `{worst[0]}` ({gm} {worst[1]:.4g}) is markedly worse "
                f"than on `{best[0]}` ({best[1]:.4g}) — investigate what differs "
                "about that dataset.",
                based_on=[eid, worst[0], best[0]],
                evidence=[bucket[ds].get("id") for ds, _ in vals])
            if s:
                suggestions.append(s)
