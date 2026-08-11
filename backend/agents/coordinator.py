"""Coordinator agent: a generalist scientific assistant that orchestrates tools
(run_python / run_r / run_shell / save_artifact) in a persistent sandboxed kernel,
emitting streaming events to the client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable

from ..llm import LLMClient
from .approval import ApprovalBroker
from .tools import ToolContext, build_tools, get_tool_schemas

log = logging.getLogger("fox.coordinator")

# Read-only / idempotent tools safe to retry once after a transient failure.
# Mutating tools (run_python, run_shell, run_sweep, editor__edit_file, …) must
# never auto-retry — a retry could double-execute side effects.
_RETRYABLE_TOOLS = {
    "editor__read_file", "editor__list_files", "editor__open",
    "list_files", "get_env", "list_variables",
}

SYSTEM_PROMPT = """\
You are Fox, an open-source experiment workbench running fully on the user's machine.

You are a scientific research assistant that works hands-on with code. You help
computational biologists, chemists, physicists and data scientists run real analyses.

Working style:
- You solve problems by writing and running code in a persistent, sandboxed Python
  kernel (numpy, pandas, scipy, matplotlib available). Variables persist across calls.
- Use run_python for computation, data analysis and figures. Use matplotlib to make
  clear, well-labelled publication-style figures.
- run_r also runs R code, but each run_r call starts a FRESH Rscript process:
  variables and loaded packages do NOT persist between R calls (unlike Python),
  so re-set state inside each R snippet or prefer run_python for multi-step work.
- Figures are AUTOMATICALLY saved as artifacts — do NOT call plt.savefig() or any
  save function for that.
- Use the save_artifact TOOL (a separate tool call, never inside the Python kernel)
  to persist important tables/summaries/data.
- When the user asks to run/compare/optimise an experiment, FIRST call
  create_experiment (hypothesis + goal metric/target + baseline config) and include
  an explicit PLAN: the hypothesis, the goal metric and target, the exact configs /
  variable values you intend to try (a short list), and the stopping criteria for
  the experiment. Then run variants. Inside run_python code, call
   report_metric("name", value) for each headline number so every run records
   structured, comparable metrics.
- When a run uses a specific dataset (e.g. a real vs a synthetic/obfuscated
  copy), tag it from inside the run_python code with report_dataset("<name>")
  (e.g. "real" / "synthetic") right after loading the data, so the Experiments
  tab can group and compare the experiment's runs across datasets.
- For each config point you evaluate, delimit it explicitly: call start_run
  (variant label + config) before running that variant's code and finish_run
  (optional notes) after, so every variant is recorded with its own label, config
  and metrics and can be compared against the baseline.
- Use run_shell only when necessary; prefer the Python kernel. Shell commands that
  touch the network or are destructive will ask the user for permission.
- The shared Research Knowledge Graph (rkg__* tools) gives you a literature
  corpus with a RAG index and domain research reports. When a question is about
  published literature or related work, ground your claims with
  rkg__query_rag / rkg__paper_notes before answering, and check
  rkg__scenario_status / rkg__scenario_report for existing domain reports.
- Tools that come from external MCP servers are named like <server>__<tool> (e.g.
  science__uniprot_lookup). Use them for domain lookups (databases, sequence
  analysis, etc.). MCP tools that may modify data or launch compute will ask the
  user for permission.
- Every figure records its exact code and environment so it can be reproduced.
  Prefer to reference artifacts by their id.
- Be rigorous: cite numbers you actually computed. If you don't know, say so.
- Keep the user informed of what you're doing at each step. Be concise in prose.

Privacy: everything stays on the user's machine unless they explicitly approve a
shell command that touches the network.

Workspace: your working directory is the workbench repository root. Example
experiments live in examples/experiments/ (01_... 02_... 03_...). Run them with
run_python by exec'ing the file, e.g. exec(open("examples/experiments/01_simple_decay_fit.py").read()).
For experiments the user wants to keep as notebooks, use create_notebook and
run_notebook so the results are stored inside the .ipynb file.

Data-obfuscation experiments: the obfuscation study (adapted to credit-card
transaction data) is bundled under examples/obfuscation/ (data generator,
obfuscation library, and 9 threat scenarios). Import it in the kernel with
  import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd()))
  from examples.obfuscation.credit_card_data import generate_credit_card
  from examples.obfuscation import experiments as exp
  df = generate_credit_card(2000, seed=42); report = exp.run_all(df)
The notebooks examples/notebooks/18_obfuscation_techniques.ipynb and
19_obfuscation_threat_scenarios.ipynb demonstrate the techniques and scenarios;
run them with run_notebook. To obfuscate the user's own uploaded data, load it
with pandas and use examples.obfuscation.obfuscate (apply_masking, tokenize,
k_anonymize, noisy_aggregate, sanitize_metadata, fuzzy_bucket).

Privacy: the "privacy" MCP server provides privacy__<tool> tools (PII detection,
dataframe privacy assessment, membership-inference / re-identification
evaluation, red-team checklists, Laplace/Gaussian differential privacy with
budget tracking and (ε,δ) guarantee summaries, and schema-preserving synthetic
data generation + quality reports). When handling potentially sensitive
scientific data: 1) start with privacy__detect_pii_in_text and
privacy__assess_dataframe_privacy; 2) if the user wants to share/publish, run
privacy__privacy_redteam_checklist and privacy__reidentification_scenario;
3) prefer synthetic data (privacy__generate_synthetic_tabular) or DP aggregates
(privacy__apply_laplace_dp) over releasing microdata; 4) track privacy budget
with privacy__dp_privacy_budget_report and surface the (ε,δ) guarantee from
privacy__dp_guarantee_summary; 5) attach each assessment as an artifact.
Example notebooks: examples/notebooks/20_privacy_assessment.ipynb,
21_differential_privacy.ipynb, 22_synthetic_data.ipynb; runnable example:
examples/privacy/run_privacy_eval.py.

Privacy workflow: if the researcher asks to exploit privacy as a peer in the
distribution / run corner-case red-team analysis / apply DP and check robustness
/ document the process as an audit trail, the backend auto-runs
examples/privacy/run_peer_exploitation.py (deterministic, no LLM needed) and
registers its reports + figures as artifacts. You can also run it yourself with
run_python by exec'ing that file, then summarize the stage 1-3 findings.
"""


async def _noop_emit(event: str, payload: dict):
    return None


def tool_mcp_action(name: str) -> tuple[str, str]:
    """Split a tool name into (MCP server, action).

    MCP tools are namespaced as ``<server>__<tool>`` (see backend/mcp.py); core
    workbench tools have no server and are attributed to the workbench itself.
    Returns ("", "") for a falsy name so callers can fall back to a plain label.
    """
    if not name:
        return "", ""
    if "__" in name:
        mcp, _, action = name.partition("__")
        return mcp, action
    return "core", name


def parse_tool_call_json(content: str, tools: dict) -> tuple | None:
    """Try to interpret assistant text as a JSON tool call.

    Accepts `{"name": "...", "parameters": {...}}` or `{"name": "...", "arguments": {...}}`,
    bare or wrapped in a fenced code block. Only matches known tool names.
    """
    import re

    text = (content or "").strip()
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = m.group(1).strip() if m else text
    if not (candidate.startswith("{") and "}" in candidate):
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    args = obj.get("parameters") or obj.get("arguments") or obj.get("args") or {}
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    if name not in tools:
        return None
    return name, args


class TurnAborted(Exception):
    """Raised inside Coordinator.run_turn when the user hits Stop.

    The turn unwinds cooperatively (checked at LLM/tool boundaries) so no
    half-finished kernel call is left running; the run is recorded as 'stopped'.
    """


class Coordinator:
    def __init__(self, llm: LLMClient, ctx: ToolContext,
                 emit: Callable[[str, dict], Awaitable[None]] | None = None,
                 persist: Callable[[str, str, dict], None] | None = None,
                 record: Callable[[dict], int] | None = None,
                 max_iters: int = 8, mcp=None,
                 audit=None,
                 check_abort: Callable[[], bool] | None = None,
                 turn_timeout: float = 0.0):
        self.llm = llm
        self.ctx = ctx
        self.emit = emit or _noop_emit
        self.persist = persist or (lambda r, c, m: None)
        self.record = record or (lambda r: None)
        self.max_iters = max_iters
        self.mcp = mcp
        self.audit = audit
        self.check_abort = check_abort
        # Wall-clock cap for one whole turn (0 = unlimited). Checked at LLM/tool
        # boundaries so a degenerate turn can't burn resources indefinitely.
        self.turn_timeout = max(0.0, float(turn_timeout))
        self._turn_deadline: float | None = None
        self.tools = build_tools(ctx)
        self._mcp_loaded = False
        self._run_seq: list[dict] = []
        self._run_artifacts: list[str] = []
        self._run_metrics: dict = {}
        self._run_dataset: str | None = None
        self._run_code: list[dict] = []
        self._run_env: dict = {}
        self._run_started = 0.0
        self._run_error: str = ""
        self._pre_run_id: int | None = None
        self.agent_name = "Fox"
        self.model_name = getattr(self.llm, "model", "") or ""
        self._stream_model_support: bool | None = None
        # Which agent loop drives turns: "classic" (hand-rolled, default) or
        # "langgraph" (LangChain orchestration, reliability features).
        self.orchestrator = os.environ.get("FOX_ORCHESTRATOR", "classic").strip().lower() or "classic"
        self.orchestrator_reliability = os.environ.get(
            "FOX_ORCHESTRATOR_RELIABILITY", "1").strip().lower() not in ("0", "false", "no")
        try:
            from ..skills import load_skills

            self._skill_names = [s.get("name") for s in load_skills() if s.get("name")]
        except Exception:  # noqa: BLE001
            self._skill_names = []

    async def _emit_status(self, phase: str = "", tool: str = "",
                           mcp: str = "", skills: list | None = None) -> None:
        """Rich, structured status so the chat window can show, live, which
        tools / MCP servers / skills / workflow stage the agent is using."""
        if self.emit is None:
            return
        payload: dict = {"agent": self.agent_name, "model": self.model_name,
                         "phase": phase, "tool": tool, "mcp": mcp,
                         "skills": skills or []}
        wf = getattr(self.ctx, "workflow", None)
        if wf is not None:
            try:
                snap = wf.snapshot()
                if snap.get("status") == "running" and (
                        snap.get("message") or snap.get("title")):
                    payload["workflow"] = (
                        f"{snap.get('title') or 'Workflow'}: "
                        f"{snap.get('message') or 'running'}")
            except Exception:  # noqa: BLE001
                pass
        await self.emit("status", payload)

    async def _ensure_mcp(self):
        """Merge MCP server tools into the tool set (lazy, once)."""
        if self.mcp is None or self._mcp_loaded:
            return
        self._mcp_loaded = True
        try:
            schemas, fns = await self.mcp.build_tools(self.ctx)
        except Exception:  # noqa: BLE001
            return
        self._mcp_schemas = schemas
        self.tools.update(fns)

    async def _on_delta(self, text: str):
        await self.emit("stream_delta", {"text": text})

    def _raise_if_aborted(self) -> None:
        if self.check_abort is not None and self.check_abort():
            raise TurnAborted()

    def _budget_exceeded(self) -> bool:
        """True when the turn's wall-clock budget has been spent."""
        return (self._turn_deadline is not None
                and time.monotonic() >= self._turn_deadline)

    def _budget_message(self) -> str:
        return ("I hit this turn's time budget and stopped to avoid burning "
                "resources. Progress so far was saved — tell me to continue "
                "and I'll pick up where I left off.")

    def _exp_meta(self, base: dict | None = None) -> dict:
        """Message meta tagged with the experiment this turn belongs to, so the
        chat window can group and navigate experiment messages."""
        meta = dict(base or {})
        eid = getattr(self.ctx, "experiment_id", "")
        if str(eid).isdigit():
            meta["experiment_id"] = int(eid)
        return meta

    def _stream_supports_model(self) -> bool:
        if self._stream_model_support is None:
            try:
                import inspect
                self._stream_model_support = "model" in inspect.signature(
                    self.llm.stream).parameters
            except Exception:  # noqa: BLE001
                self._stream_model_support = False
        return self._stream_model_support

    def _pinned_model(self) -> str:
        """Per-experiment model pin: the experiment's `model` wins over the
        global default (falling back to the focused experiment when the turn has
        no explicit experiment). Returns "" to use the client default."""
        try:
            store = getattr(self.ctx, "store", None)
            if store is None:
                return ""
            eid = getattr(self.ctx, "experiment_id", "")
            if not str(eid).isdigit():
                fid = store.get_setting("focus_experiment_id", "")
                eid = fid if str(fid).isdigit() else ""
            if str(eid).isdigit():
                exp = store.get_experiment(int(eid))
                return (exp or {}).get("model") or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def run_turn(self, messages: list[dict]) -> dict:
        """Run one agent turn over `messages` (already ending with the user message).

        Intermediate assistant/tool messages are persisted via `persist`. When the
        turn completes, a run record (prompt → tool trail → reply) is emitted via
        `record` so every agent turn is traceable.
        Returns {"text": final assistant text}."""
        if self.orchestrator == "langgraph":
            try:
                from .orchestrator import LangChainOrchestrator
            except ImportError as e:  # noqa: BLE001
                raise RuntimeError(
                    "FOX_ORCHESTRATOR=langgraph requires the optional agent "
                    "extras. Run: pip install -e '.[agent]'"
                ) from e
            return await LangChainOrchestrator(self).run(messages)
        await self._ensure_mcp()
        tools = get_tool_schemas() + list(getattr(self, "_mcp_schemas", []) or [])
        workflow = getattr(self.ctx, "workflow", None)
        self._run_seq = []
        self._run_artifacts = []
        self._run_metrics = {}
        self._run_dataset = None
        self._run_code = []
        self._run_env = {}
        self._run_started = time.time()
        self._run_error = ""
        self._pre_run_id = None
        self.ctx.run_id = ""
        self.ctx.check_abort = self.check_abort
        self._turn_deadline = (time.monotonic() + self.turn_timeout
                               if self.turn_timeout > 0 else None)
        try:
            from ..logging_config import clear_log_context
            clear_log_context("run", "trace")
        except Exception:  # noqa: BLE001
            pass
        status = "done"
        text = ""
        model_override = self._pinned_model()
        if model_override:
            self.model_name = model_override
        # Per-run environment snapshot (kernel env is cached per session — cheap).
        try:
            self._run_env = await self.ctx.kernels.get_env()
        except Exception:  # noqa: BLE001
            self._run_env = {}
        # Kernel state-loss transparency: if a kernel died + restarted this turn,
        # record it so the run is honest about which execution state it started
        # with (a restarted kernel has no prior variables/modules).
        try:
            restarts = {n: int(getattr(k, "restarts", 0) or 0)
                        for n, k in (("python", self.ctx.kernels.python),
                                     ("r", getattr(self.ctx.kernels, "r", None)))
                        if k is not None}
            if any(restarts.values()):
                self._run_env["_kernel_restarts"] = restarts
                log.warning("kernel restarted this turn (%s); state was lost",
                            {n: r for n, r in restarts.items() if r})
        except Exception:  # noqa: BLE001
            pass
        audit_meta = self._audit_meta()
        try:
            from ..logging_config import set_log_context
            set_log_context(project=audit_meta.get("session_id"),
                            trace=str(getattr(self.ctx, "message_id", "") or ""))
        except Exception:  # noqa: BLE001
            pass
        # Two-phase run lifecycle: pre-create the run row (status='running') so
        # its numeric id is known before any tool runs — every audit event this
        # turn emits links to it via run_id, and a crash mid-turn leaves a row
        # that recovery marks 'interrupted' instead of a phantom.
        try:
            store = getattr(self.ctx, "store", None)
            if store is not None and hasattr(store, "begin_run"):
                rid = store.begin_run(
                    prompt=_last_user_content(messages),
                    started_at=self._run_started,
                    kind="agent_run",
                    experiment_id=(int(self.ctx.experiment_id)
                                   if str(getattr(self.ctx, "experiment_id", "")).isdigit()
                                   else None),
                    parent_run_id=getattr(self.ctx, "parent_run_id", None),
                    model=self.model_name,
                    message_id=(int(self.ctx.message_id)
                                if str(getattr(self.ctx, "message_id", "")).isdigit()
                                else None))
                if rid:
                    self._pre_run_id = rid
                    self.ctx.run_id = str(rid)
                    audit_meta = dict(audit_meta)
                    audit_meta["run_id"] = str(rid)
                    try:
                        from ..logging_config import set_log_context
                        set_log_context(run=str(rid),
                                        trace=str(getattr(self.ctx, "message_id", "") or ""),
                                        project=audit_meta.get("session_id"))
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            log.warning("could not pre-create run row", exc_info=True)
        await self._audit_turn_event("turn_start", audit_meta)
        try:
            await self._emit_status(phase="starting")
            for _ in range(self.max_iters):
                self._raise_if_aborted()
                if self._budget_exceeded():
                    status = "stopped"
                    text = self._budget_message()
                    break
                stream_kwargs = {"on_delta": self._on_delta}
                if self._stream_supports_model():
                    stream_kwargs["model"] = model_override or None
                full = await self.llm.stream(messages, tools, **stream_kwargs)
                # A Stop arriving mid-stream takes effect as soon as it completes
                # (the stream itself can't be interrupted safely).
                self._raise_if_aborted()
                tcs = full.get("tool_calls")
                if not tcs:
                    # Rescue: some local models emit a tool call as JSON text instead
                    # of a structured tool_calls chunk. Parse it if it clearly matches.
                    rescued = parse_tool_call_json(full.get("content", ""), self.tools)
                    if rescued is not None:
                        name, args = rescued
                        tcs = [{"id": "rescue", "type": "function",
                                "function": {"name": name, "arguments": args}}]
                        full = {"role": "assistant", "content": "", "tool_calls": tcs}
                if not tcs:
                    if workflow is not None:
                        await workflow.finish()
                    text = full.get("content", "")
                    await self._emit_status(phase="complete")
                    return {"text": text, "tools": self._tool_summary(),
                            "model": self.model_name}

                assistant_msg = {
                    "role": "assistant",
                    "content": full.get("content", "") or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["function"]["name"],
                                      "arguments": json.dumps(tc["function"]["arguments"])}}
                        for tc in full["tool_calls"] if tc.get("function")
                    ],
                }
                messages.append(assistant_msg)
                self.persist("assistant", assistant_msg["content"],
                             self._exp_meta({"tool_calls": assistant_msg["tool_calls"]}))

                for tc in full["tool_calls"]:
                    await self._exec_tool_call(tc, audit_meta, messages)
                    self._raise_if_aborted()
                    if self._budget_exceeded():
                        status = "stopped"
                        text = self._budget_message()
                        break
                if status == "stopped":
                    break

            if workflow is not None:
                await workflow.finish()
            if status == "stopped":
                log.warning("turn stopped at wall-clock budget")
                await self._emit_status(phase="complete")
                return {"text": text, "tools": self._tool_summary(),
                        "model": self.model_name}
            text = self._fallback()
            await self._emit_status(phase="complete")
            return {"text": text, "tools": self._tool_summary(),
                    "model": self.model_name}
        except TurnAborted:
            status = "stopped"
            if workflow is not None:
                await workflow.finish()
            raise
        except Exception:  # noqa: BLE001
            status = "error"
            self._run_error = traceback.format_exc(limit=20)
            log.exception("agent turn %s failed",
                          getattr(self.ctx, "message_id", None) or "?")
            if workflow is not None:
                await workflow.finish()
            raise
        finally:
            # Robustness: stop any ephemeral kernels (sweep pools) still open so
            # an aborted/failed turn can't leak subprocesses. Shielded so the
            # cleanup survives the current task's cancellation.
            try:
                await asyncio.shield(self.ctx.stop_kernels())
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self._audit_turn_event(
                "turn_end", audit_meta, status=status)
            self._record_run(messages, status, text)

    def _audit_meta(self) -> dict:
        """Session/trace identity for audit events: project name + turn id."""
        session = getattr(getattr(self.ctx, "store", None), "name", None) or "workbench"
        if session == "workbench":
            try:
                session = getattr(self.ctx.artifacts, "project_dir", Path(".")).name
            except Exception:  # noqa: BLE001
                pass
        return {"session_id": str(session or "workbench"),
                "trace_id": str(getattr(self.ctx, "message_id", "") or "") or None}

    async def _audit_turn_event(self, kind: str, meta: dict, status: str = "") -> None:
        if self.audit is None:
            return
        try:
            from ..audit import emit_session_event

            payload = {"event": kind}
            if status:
                payload["status"] = status
            await emit_session_event(
                self.audit, agent_id=self.agent_name,
                session_id=meta.get("session_id"), trace_id=meta.get("trace_id"),
                run_id=meta.get("run_id"), kind=kind, tool_name=None,
                payload=payload,
                severity="info" if kind == "turn_start" else
                        ("critical" if status == "error" else "info"))
        except Exception:  # noqa: BLE001
            pass

    async def _exec_tool_call(self, tc: dict, audit_meta: dict,
                              messages: list[dict]) -> bool:
        """Execute one tool call with every side-effect (streaming events,
        approval, audit, artifacts, metrics, transcript append).

        Shared by the classic loop and the LangGraph orchestrator so both paths
        are guaranteed identical. Returns ``ok`` (success flag)."""
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args = fn.get("arguments", {}) or {}
        if name not in self.tools:
            result = f"[error] unknown tool: {name}"
            ok = False
        else:
            await self.emit("tool_start", {"id": tc.get("id"), "name": name,
                                           "args": args, "ok": True})
            mcp_server = name.split("__", 1)[0] if "__" in name else ""
            await self._emit_status(phase="tool", tool=name,
                                    mcp=mcp_server,
                                    skills=self._skill_names)
            workflow = getattr(self.ctx, "workflow", None)
            if workflow is not None:
                await workflow.on_tool_start(name)
            t0 = time.perf_counter()
            try:
                if name == "run_shell":
                    result = await self.tools[name](command=args.get("command", ""),
                                                    timeout=args.get("timeout", 30))
                else:
                    result = await self.tools[name](**args)
                ok = not result.startswith("[error]")
            except Exception as e:  # noqa: BLE001
                # Conservative retry: only read-only / idempotent tools are
                # retried once after a transient failure (a mutating tool like
                # run_python must never run twice). The retry only happens when
                # the tool raised before producing any side effect we can detect.
                if name in _RETRYABLE_TOOLS:
                    try:
                        await asyncio.sleep(0.2)
                        result = await self.tools[name](**args)
                        ok = not result.startswith("[error]")
                    except Exception as e2:  # noqa: BLE001
                        result = f"[error] {type(e2).__name__}: {e2}"
                        ok = False
                        log.warning("tool %s retry failed: %s", name, e2,
                                    exc_info=True)
                else:
                    result = f"[error] {type(e).__name__}: {e}"
                    ok = False
                    log.warning("tool %s failed: %s", name, e, exc_info=True)
            duration_ms = (time.perf_counter() - t0) * 1000.0
            if self.audit is not None:
                try:
                    from ..audit import emit_tool_audit

                    await emit_tool_audit(
                        self.audit,
                        agent_id=self.agent_name,
                        session_id=audit_meta["session_id"],
                        trace_id=audit_meta["trace_id"],
                        run_id=audit_meta.get("run_id"),
                        tool_name=name, method=name,
                        args=args, result=result, ok=ok,
                        duration_ms=duration_ms,
                        source="mcp_proxy" if "__" in name else "coordinator",
                        mcp_server=mcp_server or None)
                except Exception:  # noqa: BLE001
                    pass
            if workflow is not None:
                await workflow.on_tool_end(name, ok)
            await self.emit("tool_result", {"id": tc.get("id"), "name": name,
                                            "output": result, "ok": ok})
        self._run_seq.append({
            "name": name, "ok": ok,
            "args": _snippet(args, 200),
            "result": _snippet(result, 300),
            "duration_ms": round(duration_ms, 1),
        })
        # Round-4 provenance: keep the FULL executed code per tool call
        # (index-aligned with _run_seq) so runs are reproducible and diffable.
        full_code = ""
        if isinstance(args, dict):
            full_code = str(args.get("code") or args.get("command") or "")
        self._run_code.append({"name": name, "code": full_code})
        self._run_artifacts.extend(_artifact_ids(name, result))
        # Exact artifact linkage from the tool itself (figures, saved artifacts,
        # notebook outputs), not text scraping.
        produced = list(getattr(self.ctx, "last_artifact_ids", []) or [])
        if produced:
            self._run_artifacts.extend(produced)
            self.ctx.last_artifact_ids = []
        structured = getattr(self.ctx, "last_metrics", None) or {}
        if structured:
            self._run_metrics.update(structured)
            if self.ctx.variant:
                self.ctx.variant.setdefault("metrics", {}).update(structured)
            self.ctx.last_metrics = None
        # Dataset tag: the last report_dataset() call in this turn wins.
        if getattr(self.ctx, "last_dataset", None):
            self._run_dataset = self.ctx.last_dataset
            if self.ctx.variant:
                self.ctx.variant["dataset"] = self.ctx.last_dataset
            self.ctx.last_dataset = None
        if name not in ("start_run", "finish_run", "create_experiment"):
            # Only compute tools feed the regex metric fallback, so bookkeeping
            # output (e.g. a config dump) isn't misread.
            self._run_metrics.update(_extract_metrics(result))
        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", ""),
            "content": result,
        })
        self.persist("tool", result, self._exp_meta(
            {"name": name,
             "mcp": tool_mcp_action(name)[0],
             "action": tool_mcp_action(name)[1],
             "tool_call_id": tc.get("id", "")}))
        return ok

    def _tool_summary(self) -> list[dict]:
        """The turn's tool trail, namespaced into (mcp, action) pairs so the
        chat bubbles and Experiments timeline can label what Fox actually did."""
        out = []
        for t in self._run_seq:
            mcp, action = tool_mcp_action(t.get("name", ""))
            out.append({"name": t.get("name", ""), "mcp": mcp, "action": action,
                        "ok": t.get("ok", False)})
        return out

    def _persist_transcript(self, messages: list[dict]) -> str:
        """Persist the exact LLM request (params + assembled messages) as a
        transcript artifact for this turn. Returns the artifact id, or '' on
        any failure (never crashes the turn)."""
        try:
            if self.ctx.artifacts is None:
                return ""
            params = {
                "model": self.model_name,
                "temperature": getattr(self.llm, "temperature", None),
                "max_tokens": getattr(self.llm, "max_tokens", None),
            }
            payload = json.dumps({"params": params, "messages": messages},
                                 ensure_ascii=False, default=str)
            from ..artifacts.store import Artifact
            art = Artifact(
                kind="transcript",
                name=f"transcript-{int(self._run_started)}",
                description=("Exact LLM request for this run: model params + "
                             "the full assembled message list as sent."),
                code="", env=self._run_env or {},
                message_id=self.ctx.message_id or "", run_id="",
                data_type="text")
            self.ctx.artifacts.add_artifact(art, data=payload.encode(),
                                            data_type="text")
            return art.id
        except Exception:  # noqa: BLE001
            log.debug("could not persist turn transcript", exc_info=True)
            return ""

    def _record_run(self, messages: list[dict], status: str, text: str) -> None:
        prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                prompt = m.get("content", "")
                break
        # LLM request fidelity: persist the exact assembled messages + params as
        # a transcript artifact so the run is reproducible even after compaction
        # summarizes (not deletes) the conversation. Immune to context_cutoff.
        transcript_id = self._persist_transcript(messages)
        if transcript_id and transcript_id not in self._run_artifacts:
            self._run_artifacts.append(transcript_id)
        # The run's identity: the most recently finished variant wins, otherwise a
        # variant still open at turn end, otherwise the experiment baseline.
        variant = (self.ctx.finished_variants or [None])[-1] or self.ctx.variant
        variant_metrics = dict(variant.get("metrics") or {}) if variant else {}
        metrics = dict(self._run_metrics)
        metrics.update(variant_metrics)
        record = {
            "prompt": prompt,
            "reply": text,
            "status": status,
            "started_at": self._run_started,
            "finished_at": time.time(),
            "tool_sequence": self._run_seq,
            "artifact_ids": self._run_artifacts,
            "metrics": metrics,
            "experiment_id": int(self.ctx.experiment_id) if str(self.ctx.experiment_id).isdigit() else None,
            "config": (variant.get("config") if variant else None)
                      or self.ctx.experiment_config,
            "label": (variant.get("label") if variant else None),
            "parent_run_id": getattr(self.ctx, "parent_run_id", None),
            "model": self.model_name,
            "code": self._run_code,
            "env": self._run_env,
            "dataset": self._run_dataset,
            "message_id": (int(self.ctx.message_id)
                           if str(getattr(self.ctx, "message_id", "")).isdigit() else None),
            "plan_id": str(getattr(self.ctx, "plan_id", "") or "") or None,
            "plan_step_id": (str(getattr(self.ctx, "plan_step_id", "") or "")
                             or None),
        }
        if self._pre_run_id is not None:
            record["id"] = self._pre_run_id
        if self._run_error:
            record["error"] = self._run_error[:10000]
        run_id = self.record(record)
        if run_id and self._run_artifacts:
            self.ctx.run_id = str(run_id)
            try:
                self.ctx.artifacts.link_artifacts(
                    self._run_artifacts,
                    message_id=self.ctx.message_id,
                    run_id=str(run_id))
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _fallback() -> str:
        return ("I hit the maximum number of tool steps for this turn and couldn't "
                "finish. Let me know if you'd like me to continue or adjust the approach.")


def _last_user_content(messages: list[dict]) -> str:
    """The last user message text in a turn's message list."""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _snippet(value, limit: int) -> str:
    """Compact a tool-call argument or result to a bounded one-line snippet."""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s[:limit]


def _artifact_ids(name: str, result: str) -> list[str]:
    """Best-effort extraction of artifact ids from tool results."""
    if not result or not isinstance(result, str):
        return []
    ids: list[str] = []
    m = re.search(r"Saved artifact (\S+)", result)
    if m:
        ids.append(m.group(1))
    m = re.search(r"Figures generated \(artifacts\):\s*([^\n]+)", result)
    if m:
        ids.extend(re.findall(r"\b[0-9a-f]{32}\b", m.group(1)))
    return ids


_METRIC_RE = re.compile(r"['\"]?([A-Za-z_][A-Za-z0-9_.]*)['\"]?\s*[:=]\s*(-?\d+(?:\.\d+)?)")


def _extract_metrics(result: str, max_keys: int = 30) -> dict:
    """Best-effort numeric metric extraction from tool output.

    Catches labelled numeric values like ``accuracy: 0.9`` or ``{"rmse": 1.2}``.
    Only a bounded set of keys is kept, and values must be finite.
    """
    out: dict = {}
    if not result or not isinstance(result, str):
        return out
    for m in _METRIC_RE.finditer(result):
        key, val = m.group(1), float(m.group(2))
        if key in out or len(out) >= max_keys:
            continue
        if abs(val) < 1e308:
            out[key] = val
    return out
