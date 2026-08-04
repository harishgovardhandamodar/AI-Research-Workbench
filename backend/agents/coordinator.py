"""Coordinator agent: a generalist scientific assistant that orchestrates tools
(run_python / run_r / run_shell / save_artifact) in a persistent sandboxed kernel,
emitting streaming events to the client."""

from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable

from ..llm import LLMClient
from .approval import ApprovalBroker
from .tools import ToolContext, build_tools, get_tool_schemas

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
  create_experiment (hypothesis + goal metric/target + baseline config), then run
  variants. Inside run_python code, call report_metric("name", value) for each
  headline number so every run records structured, comparable metrics.
- For each config point you evaluate, delimit it explicitly: call start_run
  (variant label + config) before running that variant's code and finish_run
  (optional notes) after, so every variant is recorded with its own label, config
  and metrics and can be compared against the baseline.
- Use run_shell only when necessary; prefer the Python kernel. Shell commands that
  touch the network or are destructive will ask the user for permission.
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

Data-obfuscation experiments: the SWIFT obfuscation study is bundled under
examples/obfuscation/ (data generator, obfuscation library, and 9 threat
scenarios). Import it in the kernel with
  import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd()))
  from examples.obfuscation.swift_data import generate_swift
  from examples.obfuscation import experiments as exp
  df = generate_swift(2000, seed=42); report = exp.run_all(df)
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


class Coordinator:
    def __init__(self, llm: LLMClient, ctx: ToolContext,
                 emit: Callable[[str, dict], Awaitable[None]] | None = None,
                 persist: Callable[[str, str, dict], None] | None = None,
                 record: Callable[[dict], int] | None = None,
                 max_iters: int = 8, mcp=None):
        self.llm = llm
        self.ctx = ctx
        self.emit = emit or _noop_emit
        self.persist = persist or (lambda r, c, m: None)
        self.record = record or (lambda r: None)
        self.max_iters = max_iters
        self.mcp = mcp
        self.tools = build_tools(ctx)
        self._mcp_loaded = False
        self._run_seq: list[dict] = []
        self._run_artifacts: list[str] = []
        self._run_metrics: dict = {}
        self._run_started = 0.0
        self.agent_name = "Fox"
        self.model_name = getattr(self.llm, "model", "") or ""
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

    async def run_turn(self, messages: list[dict]) -> dict:
        """Run one agent turn over `messages` (already ending with the user message).

        Intermediate assistant/tool messages are persisted via `persist`. When the
        turn completes, a run record (prompt → tool trail → reply) is emitted via
        `record` so every agent turn is traceable.
        Returns {"text": final assistant text}."""
        await self._ensure_mcp()
        tools = get_tool_schemas() + list(getattr(self, "_mcp_schemas", []) or [])
        workflow = getattr(self.ctx, "workflow", None)
        self._run_seq = []
        self._run_artifacts = []
        self._run_metrics = {}
        self._run_started = time.time()
        self.ctx.run_id = ""
        status = "done"
        text = ""
        try:
            await self._emit_status(phase="starting")
            for _ in range(self.max_iters):
                full = await self.llm.stream(messages, tools, on_delta=self._on_delta)
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
                    return {"text": text}

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
                             {"tool_calls": assistant_msg["tool_calls"]})

                for tc in full["tool_calls"]:
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
                        if workflow is not None:
                            await workflow.on_tool_start(name)
                        try:
                            if name == "run_shell":
                                result = await self.tools[name](command=args.get("command", ""),
                                                                timeout=args.get("timeout", 30))
                            else:
                                result = await self.tools[name](**args)
                            ok = not result.startswith("[error]")
                        except Exception as e:  # noqa: BLE001
                            result = f"[error] {type(e).__name__}: {e}"
                            ok = False
                        if workflow is not None:
                            await workflow.on_tool_end(name, ok)
                        await self.emit("tool_result", {"id": tc.get("id"), "name": name,
                                                        "output": result, "ok": ok})
                    self._run_seq.append({
                        "name": name, "ok": ok,
                        "args": _snippet(args, 200),
                        "result": _snippet(result, 300),
                    })
                    self._run_artifacts.extend(_artifact_ids(name, result))
                    structured = getattr(self.ctx, "last_metrics", None) or {}
                    if structured:
                        self._run_metrics.update(structured)
                        if self.ctx.variant:
                            self.ctx.variant.setdefault("metrics", {}).update(structured)
                        self.ctx.last_metrics = None
                    if name not in ("start_run", "finish_run", "create_experiment"):
                        # Only compute tools feed the regex metric fallback, so
                        # bookkeeping output (e.g. a config dump) isn't misread.
                        self._run_metrics.update(_extract_metrics(result))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                    self.persist("tool", result, {"name": name, "tool_call_id": tc.get("id", "")})

            if workflow is not None:
                await workflow.finish()
            text = self._fallback()
            await self._emit_status(phase="complete")
            return {"text": text}
        except Exception:  # noqa: BLE001
            status = "error"
            if workflow is not None:
                await workflow.finish()
            raise
        finally:
            self._record_run(messages, status, text)

    def _record_run(self, messages: list[dict], status: str, text: str) -> None:
        prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                prompt = m.get("content", "")
                break
        # The run's identity: the most recently finished variant wins, otherwise a
        # variant still open at turn end, otherwise the experiment baseline.
        variant = (self.ctx.finished_variants or [None])[-1] or self.ctx.variant
        variant_metrics = dict(variant.get("metrics") or {}) if variant else {}
        metrics = dict(self._run_metrics)
        metrics.update(variant_metrics)
        run_id = self.record({
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
        })
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
