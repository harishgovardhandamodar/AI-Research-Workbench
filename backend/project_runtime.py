"""ProjectRuntime: per-project session state (SQLite store, kernels, notebooks,
workflow tracker) plus the message-context helpers the chat handler uses.

Moved out of main.py so the API routers and the WebSocket handler share it
without a circular import, and so project behaviour can be unit-tested in
isolation.
"""

from __future__ import annotations

import asyncio
import json
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
        self.llm = make_llm()
        self.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        self.max_iters = CONFIG["agent"].get("max_iters", 8)
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
        self._campaign_task: "asyncio.Task | None" = None
        # Forward kernel lifecycle/execution events into the audit trail.
        try:
            self.kernels.python.subscribe(self._on_kernel_event)
        except Exception:  # noqa: BLE001
            pass
        # Round-6: campaigns left running by a previous process are resumable.
        self.recover_campaigns()

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
        self._campaign_task = asyncio.create_task(
            self._run_campaign_task(cid, plan_steps))
        return True, "started"

    async def _run_campaign_task(self, cid: int, plan_steps):
        from .agents.approval import ApprovalBroker
        from .agents.coordinator import Coordinator
        from .campaign import run_campaign
        from .experiment_repo import maybe_autocommit

        async def bus_emit(event: str, payload: dict):
            await self.broadcast(event, payload)

        broker = ApprovalBroker(bus_emit, store=self.store, audit=self.audit_emitter,
                                session_id=self.name, agent_id="Fox")

        def _record_run(r: dict) -> int:
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
                model=r.get("model") or None, code=r.get("code"), env=r.get("env"),
                message_id=r.get("message_id") or None)
            try:
                r["id"] = rid
                if r.get("experiment_id"):
                    asyncio.get_running_loop().create_task(
                        maybe_autocommit(self, r))
            except Exception:  # noqa: BLE001
                pass
            return rid

        coord = Coordinator(
            self.llm, self.ctx(bus_emit, broker), emit=bus_emit,
            persist=lambda role, content, meta=None: self.store.add_message(
                role, content, meta),
            record=_record_run, max_iters=self.max_iters, mcp=None,
            audit=self.audit_emitter, check_abort=lambda: self.campaign_stop)
        try:
            async with self.lock:
                await run_campaign(self, coord, self.build_llm_messages, cid,
                                   emit=bus_emit, workflow=self.workflow,
                                   plan_steps=plan_steps)
        except Exception as e:  # noqa: BLE001
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

    def recover_campaigns(self) -> None:
        """Mark any campaign left 'running' by a previous process as interrupted
        so the UI offers Resume (the resume point is stored in the workflow
        snapshot's invoke metadata)."""
        try:
            for c in self.store.list_campaigns():
                if c["status"] == "running":
                    self.store.update_campaign(
                        c["id"], status="failed",
                        report=(c.get("report") or "") + "\n\n> Interrupted by "
                                "a server restart — use Resume to continue.")
        except Exception:  # noqa: BLE001
            pass

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

    def ctx(self, emit, approval) -> ToolContext:
        return ToolContext(kernels=self.kernels, artifacts=self.artifacts,
                           store=self.store, permissions=self.permissions,
                           approval=approval, emit=emit, notebooks=self.notebooks,
                           workflow=self.workflow, audit=self.audit_emitter)

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

    async def stop(self):
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
