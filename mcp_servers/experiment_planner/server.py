"""Experiment Planner MCP server.

Plan, propose, confirm, execute and manage deterministic experiments. The
planner never runs anything without explicit user confirmation: it builds a
concrete plan (experiment, dataset, seed, steps + expected outputs), the host
proposes it in the chat, and execution happens only after approval.

State is shared with the workbench backend through the project's PlanStore
(<project>/experiment_plans.json), so plans created here appear in the chat and
vice-versa.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__

mcp = MCPServer(
    "experiment_planner",
    version=__version__,
    instructions=(
        "Plan deterministic experiments, propose them, and execute only after "
        "confirmation. Always call plan_experiment first, then submit_plan / "
        "wait for the user to approve before run_experiment."
    ),
)

RO = ToolAnnotations(read_only_hint=True)


def _store_dir(project: str = "") -> Path:
    """The per-project plan store dir. The host sets FOX_PLAN_STORE to the
    workbench projects dir (or a specific project dir); falls back to ~/.fox.

    With an explicit ``project`` the dir is FOX_PLAN_STORE/<project>. Without
    one, it resolves to the *active* project — the subdirectory whose
    ``experiment_plans.json`` was touched most recently — so MCP calls that omit
    ``project`` share state with the REST host (which uses
    PROJECTS_DIR/<project>/experiment_plans.json). Falls back to the base dir.
    """
    base = Path(os.environ.get("FOX_PLAN_STORE", "~/.fox")).expanduser()
    if project:
        return base / project
    best, best_m = None, -1.0
    if base.is_dir():
        for sub in sorted(base.iterdir()):  # deterministic tie-break by name
            if not sub.is_dir():
                continue
            store = sub / "experiment_plans.json"
            if not store.exists():
                continue
            m = store.stat().st_mtime
            if m > best_m:
                best, best_m = sub, m
    return best if best is not None else base


def _out(**data) -> str:
    return json.dumps({"ok": True, **data}, default=str)


def _err(exc: Exception, recovery: str = "") -> str:
    return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "recovery": recovery}, default=str)


def _plans() -> dict:
    p = _store_dir() / "experiment_plans.json"
    if not p.exists():
        return {"plans": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"plans": {}}


def _save(data: dict) -> None:
    p = _store_dir() / "experiment_plans.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# -------------------------------------------------------------- experiment --
@mcp.tool(annotations=RO)
def health() -> str:
    return _out(status="ok")


def _ensure_registry() -> None:
    """Importing experiment_registry registers the deterministic experiments
    (side-effect). Import it in every tool that needs the registry."""
    try:
        import backend.experiment_registry  # noqa: F401
    except Exception:  # noqa: BLE001
        pass


@mcp.tool(annotations=RO)
def list_experiments() -> str:
    """List deterministic experiments the planner can orchestrate."""
    try:
        _ensure_registry()
        from backend.experiment_planner import list_experiments as _le
        return _out(experiments=_le())
    except Exception as e:  # noqa: BLE001
        return _err(e, "Run inside the workbench host so backend is importable.")


@mcp.tool()
def plan_experiment(experiment_id: str, request: str = "",
                    dataset: str = "", seed: int | None = None,
                    project: str = "") -> str:
    """Create + propose a plan for a deterministic experiment in a project.
    Returns a plan proposal; NOTHING runs until the user approves
    (run_experiment)."""
    try:
        _ensure_registry()
        from backend.experiment_planner import PlanStore, list_experiments
        st = PlanStore(_store_dir(project))
        if experiment_id not in {e["id"] for e in list_experiments()}:
            raise ValueError(f"unknown experiment '{experiment_id}' — "
                             f"available: {[e['id'] for e in list_experiments()]}")
        plan = st.create(experiment_id=experiment_id, request=request,
                         dataset=dataset, seed=seed)
        st.propose(plan["id"])
        p = st.get(plan["id"])
        return _out(plan_id=p["id"], name=p["name"], experiment_id=p["experiment_id"],
                    dataset=p["dataset"], seed=p["seed"], steps=p["steps"],
                    status=p["status"], project=project,
                    note="Plan proposed — the host shows it in chat; the user "
                         "must approve before run_experiment will execute.")
    except Exception as e:  # noqa: BLE001
        return _err(e, "Provide a valid experiment_id + a dataset file in the project.")


@mcp.tool(annotations=RO)
def get_plan(plan_id: str, project: str = "") -> str:
    """Fetch a plan and its current status / result."""
    try:
        from backend.experiment_planner import PlanStore
        p = PlanStore(_store_dir(project)).get(plan_id)
        if p is None:
            raise ValueError(f"plan not found: {plan_id}")
        out = {k: v for k, v in p.items() if k not in ("_figures_bytes", "_report_md")}
        return _out(plan=out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_plans(status: str = "", project: str = "") -> str:
    """List plans in a project, optionally filtered by status."""
    try:
        from backend.experiment_planner import PlanStore
        plans = PlanStore(_store_dir(project)).list(status or None)
        slim = [{k: v for k, v in p.items() if k not in ("_figures_bytes", "_report_md")}
                for p in plans]
        return _out(plans=slim)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def run_experiment(plan_id: str, project: str = "", timeout: float = 300.0) -> str:
    """Execute an APPROVED plan deterministically. Requires status=APPROVED
    (the user confirmed it in the chat). Persists RUNNING first (so the REST
    host can recover/cancel it) and writes the figures + report to the project
    so a DONE plan survives a restart."""
    try:
        import time as _time
        from backend.experiment_planner import (PlanStore, execute_plan,
                                                load_dataset)
        st = PlanStore(_store_dir(project))
        plan = st.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        if plan.get("status") != "APPROVED":
            raise ValueError(
                f"plan is '{plan.get('status')}' — the user must approve it "
                "first (it is shown in the chat for confirmation)")
        # Execute synchronously (imports the pure functions; heavy but local).
        # Persist RUNNING + started_at so a restart recovers an orphaned run and
        # a concurrent cancel in the host is honoured below.
        _ensure_registry()
        st.update(plan_id, status="RUNNING", started_at=_time.time())
        df = load_dataset(_store_dir(project) / plan["dataset"])
        done = execute_plan(plan, df, project_dir=_store_dir(project),
                            timeout=timeout)
        # A cancel that raced the compute flipped the status to REJECTED.
        cur = st.get(plan_id) or {}
        if cur.get("status") == "REJECTED":
            return _out(status="CANCELLED",
                        message="plan was cancelled during the run")
        if done["status"] != "DONE":
            st.update(done["id"], status=done["status"],
                      error=done.get("error"))
            raise RuntimeError(done.get("error") or "experiment failed")
        st.update(done["id"], status=done["status"], result=done["result"],
                  metrics=done["metrics"], error=done.get("error"),
                  result_dir=done.get("result_dir"))
        out = {k: v for k, v in done.items()
               if k not in ("_figures_bytes", "_report_md")}
        return _out(status="DONE", plan=out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def delete_plan(plan_id: str, project: str = "") -> str:
    """Delete a plan by id."""
    try:
        from backend.experiment_planner import PlanStore
        ok = PlanStore(_store_dir(project)).delete(plan_id)
        return _out(deleted=ok)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run(transport="stdio")
