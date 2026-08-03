"""Coordinator agent: a generalist scientific assistant that orchestrates tools
(run_python / run_r / run_shell / save_artifact) in a persistent sandboxed kernel,
emitting streaming events to the client."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from ..llm import LLMClient
from .approval import ApprovalBroker
from .tools import ToolContext, build_tools, get_tool_schemas

SYSTEM_PROMPT = """\
You are Fox, an open-source AI science workbench running fully on the user's machine.

You are a scientific research assistant that works hands-on with code. You help
computational biologists, chemists, physicists and data scientists run real analyses.

Working style:
- You solve problems by writing and running code in a persistent, sandboxed Python
  kernel (numpy, pandas, scipy, matplotlib available). Variables persist across calls.
- Use run_python for computation, data analysis and figures. Use matplotlib to make
  clear, well-labelled publication-style figures.
- Figures are AUTOMATICALLY saved as artifacts — do NOT call plt.savefig() or any
  save function for that.
- Use the save_artifact TOOL (a separate tool call, never inside the Python kernel)
  to persist important tables/summaries/data.
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
                 max_iters: int = 8, mcp=None):
        self.llm = llm
        self.ctx = ctx
        self.emit = emit or (lambda t, p: None)
        self.persist = persist or (lambda r, c, m: None)
        self.max_iters = max_iters
        self.mcp = mcp
        self.tools = build_tools(ctx)
        self._mcp_loaded = False

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

        Intermediate assistant/tool messages are persisted via `persist`.
        Returns {"text": final assistant text}."""
        await self._ensure_mcp()
        tools = get_tool_schemas() + list(getattr(self, "_mcp_schemas", []) or [])
        for _ in range(self.max_iters):
            full = await self.llm.stream(messages, tools, on_delta=self._on_delta)
            tcs = full.get("tool_calls")
            if not tcs:
                # Rescue: some local models emit a tool call as JSON text instead of
                # a structured tool_calls chunk. Parse it if it clearly matches.
                rescued = parse_tool_call_json(full.get("content", ""), self.tools)
                if rescued is not None:
                    name, args = rescued
                    tcs = [{"id": "rescue", "type": "function",
                            "function": {"name": name, "arguments": args}}]
                    full = {"role": "assistant", "content": "", "tool_calls": tcs}
            if not tcs:
                return {"text": full.get("content", "")}

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
                    await self.emit("tool_result", {"id": tc.get("id"), "name": name,
                                                    "output": result, "ok": ok})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
                self.persist("tool", result, {"name": name, "tool_call_id": tc.get("id", "")})

        return {"text": self._fallback()}

    @staticmethod
    def _fallback() -> str:
        return ("I hit the maximum number of tool steps for this turn and couldn't "
                "finish. Let me know if you'd like me to continue or adjust the approach.")
