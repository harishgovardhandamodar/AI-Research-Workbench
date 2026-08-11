"""ProjectRuntime: per-project session state (SQLite store, kernels, notebooks,
workflow tracker) plus the message-context helpers the chat handler uses.

Moved out of main.py so the API routers and the WebSocket handler share it
without a circular import, and so project behaviour can be unit-tested in
isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from audit import AuditEvent
from .agents.tools import ToolContext
from .artifacts.store import ArtifactStore
from .audit import ProjectDeviationScanner, make_audit
from .kernels.manager import make_kernel_manager
from .notebooks import NotebookService
from .permissions import PermissionManager
from .paths import PROJECTS_DIR
from .state import CONFIG, make_llm
from .store import ProjectStore
from .workflows import WorkflowTracker

log = logging.getLogger("fox.runtime")


class ProjectRuntime:
    def __init__(self, name: str):
        self.name = name
        self.dir = PROJECTS_DIR / name
        self.store = ProjectStore(self.dir)
        self.artifacts = ArtifactStore(self.dir)
        self.kernels = make_kernel_manager(self.dir)
        self.notebooks = NotebookService(self.dir, self.kernels.python)
        self.permissions = PermissionManager(self.store)
        self.lock = asyncio.Lock()
        self._compacting = False
        # Bumped on every access (get_runtime) so the idle-eviction loop can
        # close runtimes that haven't been touched in a while.
        self.last_active = time.time()
        self.llm = make_llm()
        self.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        self.max_iters = CONFIG["agent"].get("max_iters", 20)
        self.turn_timeout = CONFIG["agent"].get("turn_timeout", 0)
        self.workflow = WorkflowTracker(
            persist=lambda snap: self.store.set_setting(
                "workflow_latest", json.dumps(snap)),
            record=self.store.add_workflow_run,
        )
        try:
            latest = self.store.get_setting("workflow_latest", "")
            self.workflow.restore(json.loads(latest) if latest else None)
        except Exception:  # noqa: BLE001
            pass
        # Local agent audit trail (SQLite + hash-chained JSONL).
        self.audit_store, self.audit_emitter = make_audit(self.dir)
        self.audit_scanner = ProjectDeviationScanner(self.audit_store)
        self.audit_emitter.start()
        # Round-6: fan-out for background tasks (campaigns) — any connected
        # chat window that subscribes receives live events even when the task
        # that started the work has disconnected.
        self._event_subs: list = []
        # Background-campaign control.
        self.campaign_stop = False
        self.eval_stop = False
        self._campaign_task: "asyncio.Task | None" = None
        self._campaign_cid: int | None = None
        self._eval_task: "asyncio.Task | None" = None
        self._eval_id: int | None = None
        # Unified plan-run registry: plan_id -> task. Shared by the chat and REST
        # plan executors so a plan can't be double-launched across paths, and
        # drained on stop().
        self._plan_tasks: dict[str, "asyncio.Task"] = {}
        # Forward kernel lifecycle/execution events into the audit trail.
        try:
            self.kernels.python.subscribe(self._on_kernel_event)
        except Exception:  # noqa: BLE001
            pass
        # Round-6: campaigns left running by a previous process are resumable.
        self.recover_campaigns()
        # Round-9: same for model benchmarks.
        self.recover_evals()
        # Robustness: agent runs left 'running' by a crash become 'interrupted'
        # (each one is recorded as an audit event so recovery is traceable).
        try:
            n = self.store.mark_interrupted_runs()
            if n:
                log.warning("marked %d interrupted run(s) in project %r", n, name)
        except Exception:  # noqa: BLE001
            log.exception("could not recover interrupted runs for %r", name)
        # Finetune chat monitor (started on first chat connect).
        self._finetune_monitor: "FinetuneMonitor | None" = None

    # ---------------------------------------------------- event bus (round 6)
    def subscribe_events(self, fn) -> None:
        if fn not in self._event_subs:
            self._event_subs.append(fn)

    def unsubscribe_events(self, fn) -> None:
        if fn in self._event_subs:
            self._event_subs.remove(fn)

    async def broadcast(self, event: str, payload: dict) -> None:
        for fn in list(self._event_subs):
            try:
                await fn(event, payload)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------- background campaigns --
    def plan_running(self, plan_id: str) -> bool:
        """True when a plan execution for ``plan_id`` is in flight (chat or REST)."""
        t = self._plan_tasks.get(plan_id)
        return t is not None and not t.done()

    def launch_plan(self, plan_id: str, coro) -> tuple[bool, str]:
        """Launch (or reject) a plan execution, deduplicating across the chat
        and REST paths. Returns (ok, message)."""
        if self.plan_running(plan_id):
            return False, "a plan execution is already running"

        async def _wrapped():
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("plan %s execution failed", plan_id)
            finally:
                self._plan_tasks.pop(plan_id, None)

        self._plan_tasks[plan_id] = asyncio.create_task(_wrapped())
        return True, "started"

    def cancel_plan_task(self, plan_id: str) -> bool:
        """Best-effort cancel of an in-flight plan execution. Returns True when
        a live task was found and cancelled."""
        t = self._plan_tasks.get(plan_id)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    async def drain_plans(self, timeout: float = 5.0) -> None:
        """Cancel + await all in-flight plan executions (shutdown)."""
        tasks = [t for t in self._plan_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        self._plan_tasks.clear()

    def campaign_running(self) -> bool:
        return (self._campaign_task is not None
                and not self._campaign_task.done())

    def stop_campaign(self) -> bool:
        """Request a graceful stop of the running background campaign (checked
        between steps). Returns False when no campaign is running."""
        if not self.campaign_running():
            return False
        self.campaign_stop = True
        return True

    def start_campaign(self, cid: int,
                       plan_steps: list[dict] | None = None) -> tuple[bool, str]:
        """Launch a background campaign for this project (one at a time). The
        task broadcasts live progress to every subscribed chat window and
        survives the launching connection closing. Returns (ok, message)."""
        if self.campaign_running():
            return False, "a campaign is already running for this project"
        if self.store.get_campaign(cid) is None:
            return False, f"campaign #{cid} not found"
        self.campaign_stop = False
        self._campaign_cid = cid
        self._campaign_task = asyncio.create_task(
            self._run_campaign_task(cid, plan_steps))
        return True, "started"

    async def _run_campaign_task(self, cid: int, plan_steps):
        from .agents.approval import ApprovalBroker
        from .agents.coordinator import Coordinator
        from .campaign import run_campaign
        from .experiment_repo import maybe_autocommit
        from .logging_config import set_log_context, clear_log_context
        set_log_context(project=self.name, campaign=cid)
        # Dedicated kernel: the campaign's code runs in its own subprocess so it
        # never shares state with the chat kernel, and it no longer holds the
        # project lock — the user can keep chatting while a campaign runs.
        task_kernels = make_kernel_manager(self.dir)
        try:
            task_kernels.python.subscribe(self._on_kernel_event)
        except Exception:  # noqa: BLE001
            pass

        async def bus_emit(event: str, payload: dict):
            await self.broadcast(event, payload)

        broker = ApprovalBroker(bus_emit, store=self.store, audit=self.audit_emitter,
                                session_id=self.name, agent_id="Fox")

        def _record_run(r: dict) -> int:
            if r.get("id"):
                rid = self.store.finish_run(
                    rid=int(r["id"]),
                    reply=r.get("reply", ""),
                    status=r.get("status", "done"),
                    finished_at=r.get("finished_at"),
                    tool_sequence=r.get("tool_sequence"),
                    artifact_ids=r.get("artifact_ids"),
                    metrics=r.get("metrics"),
                    config=r.get("config"),
                    label=r.get("label"),
                    code=r.get("code"),
                    env=r.get("env"),
                    dataset=r.get("dataset"),
                    error=r.get("error") or None,
                    review=r.get("review"),
                    plan_id=r.get("plan_id") or None,
                    plan_step_id=r.get("plan_step_id") or None)
            else:
                rid = self.store.add_run(
                    prompt=r.get("prompt", ""), reply=r.get("reply", ""),
                    status=r.get("status", "done"),
                    started_at=r.get("started_at", 0.0),
                    finished_at=r.get("finished_at", time.time()),
                    tool_sequence=r.get("tool_sequence"),
                    artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                    review=r.get("review"),
                    experiment_id=r.get("experiment_id") or None,
                    config=r.get("config"), label=r.get("label"),
                    parent_run_id=r.get("parent_run_id") or None,
                    model=r.get("model") or None, code=r.get("code"),
                    env=r.get("env"),
                    message_id=r.get("message_id") or None,
                    dataset=r.get("dataset") or None,
                    error=r.get("error") or None,
                    plan_id=r.get("plan_id") or None,
                    plan_step_id=r.get("plan_step_id") or None)
            try:
                r["id"] = rid
                if r.get("experiment_id"):
                    asyncio.get_running_loop().create_task(
                        maybe_autocommit(self, r))
            except Exception:  # noqa: BLE001
                pass
            return rid

        coord = Coordinator(
            self.llm, self.ctx(bus_emit, broker, kernels=task_kernels),
            emit=bus_emit,
            persist=lambda role, content, meta=None: self.store.add_message(
                role, content, meta),
            record=_record_run, max_iters=self.max_iters, mcp=None,
            audit=self.audit_emitter, check_abort=lambda: self.campaign_stop,
            turn_timeout=self.turn_timeout)
        try:
            await run_campaign(self, coord, self.build_llm_messages, cid,
                               emit=bus_emit, workflow=self.workflow,
                               plan_steps=plan_steps)
        except Exception as e:  # noqa: BLE001
            log.exception("campaign %s failed", cid)
            try:
                self.store.update_campaign(
                    cid, status="failed",
                    report=f"Campaign failed: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.broadcast("error", {"message": f"Campaign failed: {e}"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.campaign_stop = False
            try:
                await task_kernels.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                clear_log_context("campaign")
            except Exception:  # noqa: BLE001
                pass

    def recover_campaigns(self) -> None:
        """Mark any campaign left 'running' by a previous process as interrupted
        so the UI offers Resume. The durable resume point is derived from the
        persisted step statuses (see store.campaign_resume_step), so it survives
        restarts and concurrent chat turns — running steps are reset to 'planned'
        so the next run resumes cleanly."""
        try:
            for c in self.store.list_campaigns():
                if c["status"] == "running":
                    self.store.update_campaign(
                        c["id"], status="failed",
                        report=(c.get("report") or "") + "\n\n> Interrupted by "
                                "a server restart — use Resume to continue.")
                    # Any step left mid-flight becomes resumable again.
                    try:
                        for s in self.store.list_campaign_steps(c["id"]):
                            if (s.get("status") or "planned") in ("running", "planned") \
                                    and s.get("status") != "done":
                                self.store.update_campaign_step(
                                    s["id"], status="planned",
                                    note="Interrupted by a server restart — resume to continue.")
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning("campaign %s recovered (was running)", c["id"])
        except Exception:  # noqa: BLE001
            log.exception("campaign recovery scan failed")

    # ------------------------------------------------------ background evals --
    def eval_running(self) -> bool:
        return (self._eval_task is not None and not self._eval_task.done())

    def stop_eval(self) -> bool:
        if not self.eval_running():
            return False
        self.eval_stop = True
        return True

    def start_eval(self, eid: int) -> tuple[bool, str]:
        """Launch a background model benchmark for this project."""
        if self.eval_running():
            return False, "an eval is already running for this project"
        if self.store.get_eval(eid) is None:
            return False, f"eval #{eid} not found"
        self.eval_stop = False
        self._eval_id = eid
        self._eval_task = asyncio.create_task(self._run_eval_task(eid))
        return True, "started"

    async def _run_eval_task(self, eid: int):
        from .agents.approval import ApprovalBroker
        from .agents.coordinator import Coordinator
        from .eval import run_eval
        from .experiment_repo import maybe_autocommit
        from .logging_config import set_log_context, clear_log_context
        set_log_context(project=self.name, eval=eid)
        # Dedicated kernel (see _run_campaign_task): isolated state, no lock.
        task_kernels = make_kernel_manager(self.dir)
        try:
            task_kernels.python.subscribe(self._on_kernel_event)
        except Exception:  # noqa: BLE001
            pass

        async def bus_emit(event: str, payload: dict):
            await self.broadcast(event, payload)

        broker = ApprovalBroker(bus_emit, store=self.store, audit=self.audit_emitter,
                                session_id=self.name, agent_id="Fox")

        def _record_run(r: dict) -> int:
            if r.get("id"):
                rid = self.store.finish_run(
                    rid=int(r["id"]),
                    reply=r.get("reply", ""),
                    status=r.get("status", "done"),
                    finished_at=r.get("finished_at"),
                    tool_sequence=r.get("tool_sequence"),
                    artifact_ids=r.get("artifact_ids"),
                    metrics=r.get("metrics"),
                    config=r.get("config"),
                    label=r.get("label"),
                    code=r.get("code"),
                    env=r.get("env"),
                    dataset=r.get("dataset"),
                    error=r.get("error") or None,
                    review=r.get("review"),
                    plan_id=r.get("plan_id") or None,
                    plan_step_id=r.get("plan_step_id") or None)
            else:
                rid = self.store.add_run(
                    prompt=r.get("prompt", ""), reply=r.get("reply", ""),
                    status=r.get("status", "done"),
                    started_at=r.get("started_at", 0.0),
                    finished_at=r.get("finished_at", time.time()),
                    tool_sequence=r.get("tool_sequence"),
                    artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                    review=r.get("review"),
                    experiment_id=r.get("experiment_id") or None,
                    config=r.get("config"), label=r.get("label"),
                    parent_run_id=r.get("parent_run_id") or None,
                    model=r.get("model") or None, code=r.get("code"),
                    env=r.get("env"),
                    message_id=r.get("message_id") or None,
                    dataset=r.get("dataset") or None,
                    error=r.get("error") or None,
                    plan_id=r.get("plan_id") or None,
                    plan_step_id=r.get("plan_step_id") or None)
            try:
                r["id"] = rid
                if r.get("experiment_id"):
                    asyncio.get_running_loop().create_task(
                        maybe_autocommit(self, r))
            except Exception:  # noqa: BLE001
                pass
            return rid

        coord = Coordinator(
            self.llm, self.ctx(bus_emit, broker, kernels=task_kernels),
            emit=bus_emit,
            persist=lambda role, content, meta=None: self.store.add_message(
                role, content, meta),
            record=_record_run, max_iters=self.max_iters, mcp=None,
            audit=self.audit_emitter, check_abort=lambda: self.eval_stop,
            turn_timeout=self.turn_timeout)
        try:
            await run_eval(self, coord, self.build_llm_messages, eid,
                           emit=bus_emit, workflow=self.workflow)
        except Exception as e:  # noqa: BLE001
            log.exception("eval %s failed", eid)
            try:
                self.store.update_eval(
                    eid, status="failed",
                    report=f"Eval failed: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.broadcast("error", {"message": f"Eval failed: {e}"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.eval_stop = False
            try:
                await task_kernels.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                clear_log_context("eval")
            except Exception:  # noqa: BLE001
                pass

    def recover_evals(self) -> None:
        try:
            for e in self.store.list_evals():
                if e["status"] == "running":
                    self.store.update_eval(
                        e["id"], status="failed",
                        report=(e.get("report") or "") + "\n\n> Interrupted by "
                                "a server restart — use Run to continue.")
                    log.warning("eval %s recovered (was running)", e["id"])
        except Exception:  # noqa: BLE001
            log.exception("eval recovery scan failed")

    # --------------------------------------------------- finetune chat monitor
    def start_finetune_monitor(self) -> None:
        """Ensure the finetune monitor task is running for this project. It
        tails dk-lora job logs and broadcasts live debug lines + pipeline
        snapshots to every subscribed chat window. Idempotent."""
        if self._finetune_monitor is None:
            from .finetune_monitor import FinetuneMonitor
            self._finetune_monitor = FinetuneMonitor(self)
        if not self._finetune_monitor.running:
            self._finetune_monitor.start()

    def stop_finetune_monitor(self) -> None:
        if self._finetune_monitor is not None:
            self._finetune_monitor.stop()
            self._finetune_monitor = None

    def _on_kernel_event(self, event: str, payload: dict):
        if event not in ("busy", "idle", "output", "reset"):
            return
        if self.audit_emitter is None:
            return
        if event == "busy":
            method, severity = "run_code", "info"
            args = {"code": (payload.get("code") or "")[:2000]}
            status = "running"
        elif event == "idle":
            method, severity = "run_code", "info"
            args = {"ok": payload.get("last_ok")}
            status = "ok" if payload.get("last_ok") is not False else "error"
        elif event == "output":
            method, severity = "output", "info"
            args = {"text": (payload.get("text") or "")[:2000]}
            status = "ok"
        else:
            method, severity = "reset", "warning"
            args = {}
            status = "ok"

        ev = {
            "agent_id": "kernel", "source": "kernel", "severity": severity,
            "method": f"kernel.{method}", "tool_name": f"kernel.{event}",
            "session_id": self.name,
            "arguments_redacted": args,
            "result_summary": AuditEvent.result_summary_for(status),
            "duration_ms": payload.get("duration_ms"),
            "tags": ["kernel", event],
        }
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._audit_emit(ev))
        except RuntimeError:
            pass

    async def _audit_emit(self, ev: dict):
        try:
            await self.audit_emitter.emit(ev)
        except Exception:  # noqa: BLE001
            pass

    def ctx(self, emit, approval, kernels=None, workflow=None) -> ToolContext:
        return ToolContext(kernels=kernels or self.kernels,
                           artifacts=self.artifacts,
                           store=self.store, permissions=self.permissions,
                           approval=approval, emit=emit, notebooks=self.notebooks,
                           workflow=workflow or self.workflow,
                           audit=self.audit_emitter)

    def status(self) -> dict:
        """Unified observability view: what this project is running right now,
        kernel health (incl. restarts), the workflow snapshot, and audit stats."""
        kernels = {}
        try:
            kernels["python"] = self.kernels.python.status()
        except Exception:  # noqa: BLE001
            kernels["python"] = {"state": "unknown"}
        try:
            r = getattr(self.kernels, "r", None)
            kernels["r"] = r.status() if r is not None else None
        except Exception:  # noqa: BLE001
            kernels["r"] = None
        audit = {}
        try:
            audit = {"events": self.audit_store.count(),
                     "open_deviations": self.audit_store.count_open_deviations()}
        except Exception:  # noqa: BLE001
            pass
        return {
            "name": self.name,
            "campaign_running": self.campaign_running(),
            "campaign_id": self._campaign_cid,
            "eval_running": self.eval_running(),
            "eval_id": self._eval_id,
            "plans_running": sorted(k for k, t in self._plan_tasks.items()
                                    if not t.done()),
            "kernels": kernels,
            "workflow": self.workflow.snapshot(),
            "audit": audit,
            "campaign_resume_step": (self.store.campaign_resume_step(self._campaign_cid)
                                     if self._campaign_cid is not None else None),
            "improve_latest": self._improve_latest(),
        }

    def _improve_latest(self) -> dict | None:
        """The durable improve-loop resume record, if any."""
        try:
            raw = self.store.get_setting("improve_latest", "")
            if raw:
                data = json.loads(raw)
                return data if data.get("kind") == "improve" else None
        except Exception:  # noqa: BLE001
            pass
        return None

    def is_busy(self) -> bool:
        """True when the runtime has live chat subscribers or running background
        work — i.e. it must not be evicted."""
        try:
            if self.workflow._subs or self._event_subs:
                return True
        except Exception:  # noqa: BLE001
            pass
        if self.campaign_running() or self.eval_running():
            return True
        return any(not t.done() for t in self._plan_tasks.values())

    async def evict(self) -> None:
        """Close an idle runtime for good: drain/stop background work, stop the
        kernels + audit emitter, and release the SQLite connection."""
        await self.stop()
        try:
            from .store import close_project_db
            close_project_db(self.dir)
        except Exception:  # noqa: BLE001
            pass

    def _experiment_context(self) -> str:
        """A goal-first block describing the experiment the agent should steer
        toward, so it works on the user's objective (hypothesis, goal metric/
        target, distance to target) instead of only reacting to the last message.

        Selection: the project's explicit *focus* experiment wins; otherwise the
        most recently *active* experiment (updated_at moves on every new run, so
        this tracks real activity, not creation time).
        """
        try:
            exps = self.store.list_experiments()
        except Exception:  # noqa: BLE001
            return ""
        if not exps:
            return ""
        focus = self.store.get_setting("focus_experiment_id", "")
        pick = None
        if str(focus).isdigit():
            for e in exps:
                if e.get("id") == int(focus):
                    pick = e
                    break
        if pick is None:
            active = [e for e in exps if e.get("status") == "active"]
            if active:
                pick = sorted(active, key=lambda e: e.get("updated_at") or 0,
                              reverse=True)[0]
        if pick is None:
            return ""
        focused = str(focus).isdigit() and int(focus) == pick.get("id")
        name = (pick.get("name") or "untitled").strip()
        hypothesis = (pick.get("hypothesis") or "").strip()
        goal = (pick.get("goal_metric") or "").strip()
        target = pick.get("goal_target")
        higher = pick.get("higher_better") is not False
        plan = (pick.get("plan") or "").strip()

        heading = "## Focused experiment context" if focused else "## Active experiment context"
        lines = [f"{heading}: {name}"]
        if hypothesis:
            lines.append(f"- Hypothesis: {hypothesis}")
        if goal:
            dirn = "higher is better" if higher else "lower is better"
            tgt = "" if target is None else f", target {target}"
            lines.append(f"- Goal: {goal} ({dirn}{tgt})")
        # Goals from the Goals panel that apply to this experiment (scoped or
        # project-wide) — so the objectives UI and the agent agree.
        try:
            extra = [g for g in self.store.goals_for_experiment(pick.get("id"))
                     if g["metric"] != goal]
            if extra:
                bits = [f"{g['metric']} {'↑' if g['higher_better'] else '↓'} {g['target']}"
                        for g in extra]
                lines.append(f"- Additional goals: {', '.join(bits)}")
        except Exception:  # noqa: BLE001
            pass
        if plan:
            lines.append(f"- Plan: {plan}")
        try:
            steps = self.store.list_experiment_steps(pick.get("id"))
            if steps:
                lines.append("- Plan steps: " + "; ".join(
                    f"{i + 1}. {s['title']} ({s['status']})"
                    for i, s in enumerate(steps)))
        except Exception:  # noqa: BLE001
            pass
        try:
            runs = self.store.list_runs(limit=2000)
            exp_runs = [r for r in runs if r.get("experiment_id") == pick.get("id")]
            if exp_runs:
                best = None
                for r in exp_runs:
                    m = (r.get("metrics") or {}).get(goal) if goal else None
                    if m is None:
                        continue
                    if best is None or (higher and m > best[1]) or (not higher and m < best[1]):
                        best = (r.get("id"), m)
                if best is not None:
                    line = (f"- Runs recorded: {len(exp_runs)}; best {goal or 'metric'} "
                            f"so far: {best[1]} (run #{best[0]})")
                    if target is not None:
                        if (best[1] >= target if higher else best[1] <= target):
                            line += "; **target reached** ✓"
                        elif target:
                            line += f"; {best[1] / target * 100:.0f}% of target"
                    lines.append(line)
                else:
                    lines.append(f"- Runs recorded: {len(exp_runs)}")
                # Cross-experiment memory: best value for this metric anywhere.
                xbest = None
                for r in runs:
                    m = (r.get("metrics") or {}).get(goal) if goal else None
                    if m is None:
                        continue
                    if xbest is None or (higher and m > xbest[1]) or (not higher and m < xbest[1]):
                        xbest = (r.get("id"), m, r.get("experiment_id"))
                if xbest is not None and xbest[0] not in (b[0] for b in [best] if best):
                    lines.append(
                        f"- Best {goal or 'metric'} across experiments: {xbest[1]} "
                        f"(run #{xbest[0]}, experiment #{xbest[2] or '—'})")
        except Exception:  # noqa: BLE001
            pass
        data = self._project_data_context()
        if data:
            lines.append(data)
        # Round-7 knowledge memory: what prior experiments already learned about
        # this goal metric, so the agent builds on it instead of re-trying.
        try:
            prior = self.store.list_learnings(metric=goal, limit=5) if goal else []
            if prior:
                lines.append("- Prior learnings: " + "; ".join(
                    f"\"{l['summary']}\"" for l in prior))
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    def _project_data_context(self) -> str:
        """List the project's data files (name + size) so the agent knows what is
        available instead of loading files blind. Files live under the project
        directory (uploads land in its root, Kaggle imports under data/)."""
        try:
            entries = []
            if self.dir.is_dir():
                for sub in (self.dir, self.dir / "data"):
                    if not sub.is_dir():
                        continue
                    for p in sorted(sub.iterdir()):
                        if not p.is_file() or p.name.startswith("."):
                            continue
                        if p.suffix.lower() in (".db", ".pyc", ".lock"):
                            continue
                        if p.name in ("workbench.db", "workbench.db-wal", "workbench.db-shm"):
                            continue
                        try:
                            size = p.stat().st_size
                        except OSError:
                            continue
                        rel = p.relative_to(self.dir)
                        entries.append((size, str(rel)))
            if not entries:
                return ""
            entries.sort(key=lambda x: -x[0])
            shown = entries[:20]
            size_fmt = lambda b: (f"{b/1e6:.1f} MB" if b > 1e6 else f"{b/1e3:.0f} KB")
            out = ["", "## Available project data"]
            for size, rel in shown:
                out.append(f"- {rel} ({size_fmt(size)})")
            if len(entries) > len(shown):
                out.append(f"- … and {len(entries) - len(shown)} more files")
            out.append(f"Project directory: {self.dir}")
            return "\n".join(out)
        except Exception:  # noqa: BLE001
            return ""

    def build_llm_messages(self) -> list[dict]:
        from .agents.coordinator import SYSTEM_PROMPT
        from .skills import skills_context

        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        summary = self.store.get_setting("context_summary", "")
        rows = self.store.list_messages()
        msgs: list[dict] = []
        for r in rows:
            if r["id"] <= cutoff:
                continue
            role = r["role"]
            meta = r.get("meta") or {}
            if role == "system":
                continue
            if role == "user":
                msgs.append({"role": "user", "content": r["content"]})
            elif role == "assistant":
                d = {"role": "assistant", "content": r["content"]}
                tcs = meta.get("tool_calls")
                if tcs:
                    d["tool_calls"] = wire_tool_calls(tcs)
                msgs.append(d)
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""),
                             "content": r["content"]})
        sk = skills_context()
        system = SYSTEM_PROMPT + ("\n\n" + sk if sk else "")
        exp_ctx = self._experiment_context()
        if exp_ctx:
            system += "\n\n" + exp_ctx
        if summary:
            system += ("\n\n## Summary of earlier conversation (compacted)\n"
                       "The following is a persistent summary of turns that were "
                       "compacted out of the live context:\n" + summary)
        msgs.insert(0, {"role": "system", "content": system})
        return sanitize_messages(msgs)

    # Number of fresh messages kept before older turns get compacted away.
    COMPACTION_LIMIT = 60
    # Always keep this many of the most recent messages fresh in the context.
    COMPACTION_KEEP = 24

    async def maybe_compact(self):
        """Summarize older turns into a persistent summary once the conversation
        grows past COMPACTION_LIMIT fresh messages.

        The summary + the message-id cutoff are stored in settings, so the
        compaction survives restarts and is only performed once per block.
        """
        if self._compacting:
            return  # another compaction is in flight (it awaits the LLM)
        self._compacting = True
        try:
            await self._maybe_compact()
        finally:
            self._compacting = False

    async def _maybe_compact(self):
        rows = self.store.list_messages()
        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        fresh = [r for r in rows if r["id"] > cutoff]
        if len(fresh) <= self.COMPACTION_LIMIT:
            return
        block = fresh[:-self.COMPACTION_KEEP]
        if not block:
            return
        prev = self.store.get_setting("context_summary", "")
        summary = await _summarize_conversation(self.llm, prev, block)
        new_cutoff = block[-1]["id"]
        self.store.set_setting("context_summary", summary)
        self.store.set_setting("context_cutoff", str(new_cutoff))
        # Traceability: compaction itself is audited (how many turns were folded
        # into the summary and where the fresh-context cut landed).
        try:
            if self.audit_emitter is not None:
                from .audit import emit_session_event
                from .logging_config import clear_log_context, set_log_context
                set_log_context(project=self.name)
                await emit_session_event(
                    self.audit_emitter, agent_id="system",
                    session_id=self.name, trace_id=None, run_id=None,
                    kind="compaction", tool_name=None, payload={
                        "event": "compaction",
                        "folded": len(block),
                        "cutoff": new_cutoff,
                        "kept": len(fresh) - len(block),
                        "used_llm": summary != _conversation_digest(block, limit=160),
                    },
                    severity="info")
                clear_log_context("project")
        except Exception:  # noqa: BLE001
            log.debug("compaction audit emit failed", exc_info=True)

    async def stop(self, drain_timeout: float = 10.0):
        """Graceful shutdown: request stop on background work, await the
        campaign/eval tasks (they check ``campaign_stop`` between steps and
        persist a resumable point), cancel stragglers, then stop the finetune
        monitor, audit emitter and kernels. Returns when drained or timed out."""
        self.campaign_stop = True
        self.eval_stop = True
        self.stop_finetune_monitor()
        await self.drain_plans()
        tasks = [t for t in (self._campaign_task, self._eval_task) if t is not None]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=drain_timeout)
            except Exception:  # noqa: BLE001
                pass
            for t in tasks:
                if not t.done():
                    log.warning("cancelling unfinished task for %r", self.name)
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        try:
            await self.audit_emitter.stop()
        except Exception:  # noqa: BLE001
            pass
        await self.kernels.stop()


def wire_tool_calls(tcs: list) -> list:
    """Normalize stored tool_calls to the OpenAI wire format (arguments as JSON string)."""
    out = []
    for tc in tcs or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append({
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": json.dumps(args)},
        })
    return out


def sanitize_messages(msgs: list[dict]) -> list[dict]:
    """Ensure OpenAI tool-call history is well-formed (tool results follow calls)."""
    clean: list[dict] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            remaining = msgs[i + 1:]
            call_ids = {tc.get("id") for tc in m["tool_calls"]}
            if not any(r.get("role") == "tool" and r.get("tool_call_id") in call_ids
                       for r in remaining):
                m = {"role": "assistant", "content": m.get("content", "")}
        clean.append(m)
    return clean


def _conversation_digest(rows: list[dict], limit: int = 120) -> str:
    """Deterministic fallback summary: one compacted line per message."""
    out: list[str] = []
    for r in rows:
        role = r["role"]
        content = " ".join((r["content"] or "").split())
        if role == "user":
            out.append(f"user: {content[:limit]}")
        elif role == "assistant":
            out.append(f"assistant: {content[:limit]}")
        elif role == "tool":
            meta = r.get("meta") or {}
            out.append(f"tool({meta.get('name', 'tool')}): {content[:100]}")
    return "\n".join(out[:300])


async def _summarize_conversation(llm, prev: str, rows: list[dict]) -> str:
    """Produce (or extend) a persistent summary of compacted conversation turns.

    Best-effort: an LLM summary when available, otherwise a deterministic
    digest of the message contents.
    """
    transcript = _conversation_digest(rows)
    if prev:
        transcript = f"Existing summary:\n{prev}\n\nNew turns to fold in:\n{transcript}"
    prompt = (
        "You maintain a persistent summary of an agentic research conversation. "
        "Read the turns below and produce a compact summary capturing: the user's "
        "research goal and constraints, what experiments/analyses were run, key "
        "results and metric values, and any open questions or next steps. Plain "
        "sentences or short bullets, no markdown headings, keep it under 400 words.\n\n"
        + transcript[:8000])
    try:
        resp = await llm.complete([{"role": "user", "content": prompt}],
                                  temperature=0.2, tools=None)
        text = (resp.get("content") or "").strip()
        if text:
            return text[:4000]
    except Exception:  # noqa: BLE001
        pass
    return _conversation_digest(rows, limit=160)
