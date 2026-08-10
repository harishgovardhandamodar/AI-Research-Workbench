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
import time
import uuid
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
         "needs_dataset": bool(d.get("needs_dataset"))}
        for eid, d in sorted(EXPERIMENT_REGISTRY.items())
    ]


# ------------------------------------------------------------- plan store -----
PLAN_STATUSES = ("DRAFT", "WAITING_APPROVAL", "APPROVED", "RUNNING",
                 "DONE", "FAILED", "REJECTED")


class PlanStore:
    """Per-project plan store backed by <project>/experiment_plans.json."""

    def __init__(self, project_dir: Path):
        self.path = project_dir / "experiment_plans.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"plans": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"plans": {}}

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str),
                             encoding="utf-8")

    def create(self, experiment_id: str, request: str, dataset: str = "",
               seed: int | None = None) -> dict:
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
                import pandas as pd
                try:
                    head = pd.read_csv(ds_path, nrows=1)
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
        seed = seed if seed is not None else int(time.time()) % (2 ** 31)
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
            "seed": seed,
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
        data = self._load()
        data["plans"][plan_id] = plan
        self._save(data)
        return plan

    def get(self, plan_id: str) -> dict | None:
        return self._load()["plans"].get(plan_id)

    def update(self, plan_id: str, **fields) -> dict:
        data = self._load()
        plan = data["plans"].get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        plan.update(fields)
        plan["updated_at"] = time.time()
        self._save(data)
        return plan

    def list(self, status: str | None = None) -> list[dict]:
        plans = list(self._load()["plans"].values())
        plans.sort(key=lambda p: p.get("created_at", 0))
        if status:
            plans = [p for p in plans if p.get("status") == status]
        return plans

    def delete(self, plan_id: str) -> bool:
        data = self._load()
        if plan_id not in data["plans"]:
            return False
        del data["plans"][plan_id]
        self._save(data)
        return True

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
                  "error": None, "metrics": None, "artifact_ids": []}
        if dataset is not None:
            fields["dataset"] = dataset
        if seed is not None:
            fields["seed"] = seed
        if request is not None:
            fields["request"] = request
            defn = EXPERIMENT_REGISTRY.get(plan["experiment_id"]) or {}
            sd = defn.get("plan_steps") or []
            fields["steps"] = (list(sd(request or "", fields["dataset"]))
                               if callable(sd) else list(sd))
        plan = self.update(plan_id, **fields)
        return self.propose(plan_id)

    def clone(self, plan_id: str, *, seed: int | None = None,
              dataset: str | None = None, request: str | None = None) -> dict:
        """Clone a plan into a fresh DRAFT (for re-run / variant with new seed)."""
        src = self.get(plan_id)
        if src is None:
            raise ValueError(f"plan not found: {plan_id}")
        return self.create(
            experiment_id=src["experiment_id"],
            request=request if request is not None else src.get("request", ""),
            dataset=dataset if dataset is not None else src.get("dataset", ""),
            seed=seed if seed is not None else src.get("seed"))


# ------------------------------------------------------------ execution -------
def plan_result_dir(project_dir: Path, plan_id: str) -> Path:
    """Where a plan's persistent outputs (report + figures) are stored."""
    d = project_dir / "plans" / plan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def execute_plan(plan: dict, df, project_dir: Path | None = None,
                 progress=None) -> dict:
    """Run an approved plan deterministically. Returns an updated plan dict
    (status DONE/FAILED, result, metrics). ``progress(step_index, message)``
    receives per-step updates during execution. When ``project_dir`` is given,
    figures + report are persisted under <project>/plans/<id>/ so a DONE plan
    survives restart and can be re-presented.
    """
    if plan.get("status") != "APPROVED":
        raise ValueError(f"plan is '{plan.get('status')}' — approve it first")
    defn = EXPERIMENT_REGISTRY.get(plan["experiment_id"])
    if defn is None:
        raise ValueError(f"unknown experiment '{plan.get('experiment_id')}'")
    plan = dict(plan)
    plan["status"] = "RUNNING"
    steps = plan.get("steps") or []
    try:
        if progress:
            for i, s in enumerate(steps):
                await_progress(progress, i, f"{i + 1}/{len(steps)} {s}")
        res = defn["run"](df, seed=plan.get("seed"))
        report_md = defn["render_report"](res)
        figures = defn["render_figures"](res) if "render_figures" in defn else {}
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
        plan.update({"status": "FAILED", "error": f"{type(e).__name__}: {e}"})
        return plan


def await_progress(progress, i, message):
    """Call a progress callback if it's async or sync."""
    if progress is None:
        return
    try:
        import asyncio
        if asyncio.iscoroutinefunction(progress):
            asyncio.get_event_loop().create_task(progress(i, message))
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
