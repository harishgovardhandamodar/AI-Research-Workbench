"""LangChain / LangGraph orchestration for the reliable agent coordinator.

Replaces the hand-rolled ReAct loop in ``Coordinator.run_turn`` with an explicit
LangGraph state machine — ``invoke → tools → [check] → final`` — while keeping
every existing side-effect (streaming events, approvals, audit, artifacts, run
records, experiment/variant metrics, cooperative Stop) by reusing the
coordinator's shared tool executor (``Coordinator._exec_tool_call``).

Enabled with ``FOX_ORCHESTRATOR=langgraph``; the classic loop remains the
default. Requires the optional ``agent`` extras (langchain / langgraph) which are
imported lazily so the workbench runs fine without them.

Reliability features (``FOX_ORCHESTRATOR_RELIABILITY``, default on):
  - retryable ``invoke`` on transient LLM failures;
  - a JSON-schema-enforced QA gate (``check``) that verifies the final answer and,
    when invalid, feeds one corrective instruction back into a bounded refine loop;
  - a per-turn step budget (preserves ``max_iters``) plus the cooperative Stop.
"""

from __future__ import annotations

import json
import operator
import time
from typing import Annotated, Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field, create_model

from ..llm import LLMError
from .coordinator import TurnAborted


class OrchState(TypedDict, total=False):
    """LangGraph state. ``messages`` uses an append reducer (operator.add)."""

    messages: Annotated[list[dict], operator.add]
    tool_calls: list[dict]
    text: str
    steps: int
    reflect_used: int
    final: bool
    phase: str


_CHECK_PROMPT = """\
You are a rigorous QA gate. Review the assistant's final answer against the
user's request and the tool outputs in the conversation below.

- Set valid=true only if the answer directly addresses the request AND every
  number or claim it makes is traceable to a tool output in the transcript.
- If valid=false, feedback must be ONE short imperative instruction telling the
  assistant exactly what to fix and rerun. Never invent evidence.
"""


class CheckVerdict(BaseModel):
    valid: bool = Field(description="true if the answer is complete and supported")
    feedback: str = Field(
        default="", description="if invalid, one short corrective instruction")


# --------------------------------------------------------------------------- #
# JSON schema -> pydantic args model (for LangChain StructuredTool binding)
# --------------------------------------------------------------------------- #

def _field_type(spec: dict) -> Any:
    t = spec.get("type")
    if t == "string":
        if spec.get("enum"):
            return Literal[tuple(spec["enum"])]
        return str
    if t == "number":
        return float
    if t == "integer":
        return int
    if t == "boolean":
        return bool
    if t == "array":
        return list
    if t == "object":
        return dict
    return Any


def _args_model_from_schema(name: str, parameters: dict) -> Any:
    """Build a pydantic model from an OpenAI tool-call JSON schema."""
    props = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])
    fields: dict[str, Any] = {}
    for pname, pspec in props.items():
        if not pname.isidentifier():
            continue
        ftype = _field_type(pspec) if isinstance(pspec, dict) else Any
        if pname in required:
            fields[pname] = (Optional[ftype], Field(..., description=(pspec or {}).get("description") or ""))
        else:
            fields[pname] = (Optional[ftype], Field(default=None, description=(pspec or {}).get("description") or ""))
    return create_model(f"{name}_args", **fields)


# --------------------------------------------------------------------------- #
# message conversion (OpenAI-dict transcript <-> langchain messages)
# --------------------------------------------------------------------------- #

def _to_lc(m: dict):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    role = m.get("role")
    content = m.get("content") or ""
    if role == "system":
        return SystemMessage(content=content)
    if role == "user":
        return HumanMessage(content=content)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id=m.get("tool_call_id") or "")
    tcs = m.get("tool_calls") or []
    if tcs:
        def _parsed_args(raw):
            if isinstance(raw, dict):
                return raw
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError:
                return {}
        return AIMessage(
            content=content,
            tool_calls=[
                {"name": t["function"]["name"],
                 "args": _parsed_args(t["function"].get("arguments")),
                 "id": t.get("id") or f"call_{i}"}
                for i, t in enumerate(tcs)
            ],
        )
    return AIMessage(content=content)


def _assistant_dict(content: str, tool_calls: list[dict] | None) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": tc.get("id") or f"call_{i}", "type": "function",
             "function": {"name": tc.get("name", ""),
                          "arguments": json.dumps(tc.get("args", {}))}}
            for i, tc in enumerate(tool_calls)
        ]
    return msg


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #

class LangChainOrchestrator:
    """LangGraph agent loop wrapping an existing ``Coordinator``.

    All side-effects go through ``coord`` (its ``_exec_tool_call``, ``emit``,
    ``persist``, ``_record_run``, ``_audit_*``), so behaviour is identical to the
    classic loop; only the control flow is explicit and testable.
    """

    def __init__(self, coord, llm=None):
        self.coord = coord
        self.llm = llm  # optional injection (tests / custom endpoint)
        self._graph = None
        self._audit_meta: dict = {}
        self.check_verdict_fn = self._llm_check  # swappable in tests

    # -- LLM --------------------------------------------------------------- #

    def _build_llm(self):
        from langchain_openai import ChatOpenAI

        base = getattr(self.coord.llm, "tool_base_url", "") or "http://127.0.0.1:11434/v1"
        model = self.coord.model_name or ""
        if not model:
            raise RuntimeError("No LLM model configured for the LangGraph orchestrator.")
        return ChatOpenAI(base_url=base, model=model, temperature=0, api_key="ollama",
                          timeout=120, max_retries=1)

    def _bound_llm(self):
        llm = self.llm or self._build_llm()
        tools = self._bind_tools()
        return llm.bind_tools(tools)

    # -- tools ------------------------------------------------------------- #

    def _tool_schemas(self):
        from .tools import get_tool_schemas

        return list(get_tool_schemas()) + list(getattr(self.coord, "_mcp_schemas", []) or [])

    def _bind_tools(self):
        from langchain_core.tools import StructuredTool

        fns = self.coord.tools
        bound = []
        for s in self._tool_schemas():
            f = s.get("function") or {}
            name = f.get("name")
            if name not in fns:
                continue
            fn = fns[name]

            async def _run(**kwargs):
                return await fn(**kwargs)

            bound.append(StructuredTool(
                name=name,
                description=f.get("description", ""),
                args_schema=_args_model_from_schema(name, f.get("parameters") or {}),
                coroutine=_run,
            ))
        return bound

    # -- nodes ------------------------------------------------------------- #

    async def _invoke(self, state: OrchState) -> dict:
        if self.coord.check_abort is not None and self.coord.check_abort():
            raise TurnAborted()
        steps = int(state.get("steps") or 0)
        if steps >= self.coord.max_iters:
            # Budget exhausted: fall back like the classic loop.
            text = self.coord._fallback()
            return {"text": text, "final": True, "steps": steps}
        steps += 1

        msgs = [ _to_lc(m) for m in state.get("messages") or [] ]
        llm = self._bound_llm()
        content = ""
        tool_chunks: dict[int, dict[str, str]] = {}
        try:
            async for chunk in llm.astream(msgs):
                if chunk.content:
                    content += chunk.content
                    await self.coord.emit("stream_delta", {"text": chunk.content})
                for tcc in getattr(chunk, "tool_call_chunks", None) or []:
                    if isinstance(tcc, dict):
                        tname, targs, tidx = tcc.get("name", ""), tcc.get("args", ""), tcc.get("index")
                    else:
                        tname, targs, tidx = tcc.name, tcc.args, tcc.index
                    idx = tidx or 0
                    slot = tool_chunks.setdefault(idx, {"name": "", "args": ""})
                    if tname:
                        slot["name"] += tname
                    if targs:
                        slot["args"] += targs
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM stream failed: {e}") from e

        tool_calls = []
        for i in sorted(tool_chunks):
            slot = tool_chunks[i]
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": f"call_{i}", "name": slot["name"], "args": args})

        if tool_calls:
            assistant = _assistant_dict(content, tool_calls)
            self.coord.persist(
                "assistant", content,
                self.coord._exp_meta({"tool_calls": assistant["tool_calls"]}))
            return {"messages": [assistant], "tool_calls": tool_calls, "steps": steps}

        text = content or ""
        return {"text": text, "steps": steps, "tool_calls": []}

    async def _tools(self, state: OrchState) -> dict:
        new_msgs: list[dict] = []
        for tc in state.get("tool_calls") or []:
            # Normalise to the OpenAI tool-call shape the shared executor expects.
            tc_openai = {
                "id": tc.get("id") or "call_0",
                "type": "function",
                "function": {"name": tc.get("name", ""),
                             "arguments": tc.get("args", {})},
            }
            await self.coord._exec_tool_call(tc_openai, self._audit_meta, new_msgs)
            if self.coord.check_abort is not None and self.coord.check_abort():
                raise TurnAborted()
        return {"messages": new_msgs, "tool_calls": []}

    async def _llm_check(self, state: OrchState) -> CheckVerdict:
        from langchain_core.messages import HumanMessage, SystemMessage

        transcript = "\n".join(
            f"{m.get('role').upper()}: {(m.get('content') or '')[:4000]}"
            for m in state.get("messages") or [] if m.get("content"))
        llm = (self.llm or self._build_llm()).with_structured_output(CheckVerdict)
        try:
            resp = await llm.ainvoke([
                SystemMessage(content=_CHECK_PROMPT),
                HumanMessage(content=f"USER REQUEST + ASSISTANT ANSWER:\n{transcript}"),
            ])
        except Exception:  # noqa: BLE001
            return CheckVerdict(valid=True, feedback="")
        return resp if isinstance(resp, CheckVerdict) else CheckVerdict(valid=True, feedback="")

    async def _check(self, state: OrchState) -> dict:
        text = state.get("text") or ""
        if not text or not self.coord.orchestrator_reliability:
            return {"final": True, "text": text}
        verdict = await self.check_verdict_fn(state)
        if verdict.valid:
            return {"final": True, "text": text}
        feedback = (verdict.feedback or "Revise the answer so it is complete and "
                                        "fully supported by the tool outputs.").strip()
        reflect = {"role": "user",
                   "content": f"[QA gate] Your answer was flagged:\n{feedback}\n"
                              f"Revise the answer (re-run tools if needed) and "
                              f"output a corrected final answer now."}
        return {"messages": [reflect], "reflect_used": int(state.get("reflect_used") or 0) + 1,
                "final": False, "text": ""}

    # -- graph ------------------------------------------------------------- #

    def _build_graph(self):
        from langgraph.graph import END, StateGraph

        g = StateGraph(OrchState)
        g.add_node("invoke", self._invoke)
        g.add_node("tools", self._tools)
        g.add_node("check", self._check)

        def after_invoke(state: OrchState) -> str:
            if state.get("tool_calls"):
                return "tools"
            if state.get("final") or not state.get("text"):
                return END
            if self.coord.orchestrator_reliability:
                return "check"
            return END

        def after_check(state: OrchState) -> str:
            return END if state.get("final") else "invoke"

        g.set_entry_point("invoke")
        g.add_conditional_edges("invoke", after_invoke,
                                {"tools": "tools", "check": "check", END: END})
        g.add_edge("tools", "invoke")
        g.add_conditional_edges("check", after_check, {END: END, "invoke": "invoke"})
        from langgraph.checkpoint.memory import MemorySaver

        return g.compile(checkpointer=MemorySaver())

    # -- run --------------------------------------------------------------- #

    async def run(self, messages: list[dict]) -> dict:
        """Run one turn like ``Coordinator.run_turn``, returning
        ``{"text", "tools", "model"}``."""
        await self.coord._ensure_mcp()
        self.coord._run_seq = []
        self.coord._run_artifacts = []
        self.coord._run_metrics = {}
        self.coord._run_started = time.time()
        self.coord.ctx.run_id = ""
        status = "done"
        text = ""
        self._audit_meta = self.coord._audit_meta()
        await self.coord._audit_turn_event("turn_start", self._audit_meta)
        graph = self._build_graph()
        self._graph = graph
        try:
            await self.coord._emit_status(phase="starting")
            result = await graph.ainvoke(
                {"messages": [dict(m) for m in messages]},
                config={"recursion_limit": max(self.coord.max_iters * 4, 16),
                        "configurable": {"thread_id": "turn-%s" % time.time_ns()}},
            )
            text = result.get("text") or ""
            if not text and result.get("messages"):
                last = result["messages"][-1]
                text = last.get("content") or "" if isinstance(last, dict) else ""
            await self.coord._emit_status(phase="complete")
            return {"text": text, "tools": self.coord._tool_summary(),
                    "model": self.coord.model_name}
        except TurnAborted:
            status = "stopped"
            raise
        except Exception:  # noqa: BLE001
            status = "error"
            raise
        finally:
            await self.coord._audit_turn_event("turn_end", self._audit_meta, status=status)
            self.coord._record_run(messages, status, text)
