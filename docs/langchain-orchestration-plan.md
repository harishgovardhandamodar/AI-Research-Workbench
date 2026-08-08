# LangChain Orchestration for Reliable AI Agents — Plan

Status: **implemented (v1)** · Branch: current feature branch
Owner: Fox workbench · Dependencies: `langchain`, `langchain-core`, `langchain-openai`, `langgraph` (all optional, lazily imported)

v1 shipped: `backend/agents/orchestrator.py` (LangGraph `invoke → tools → [check]`),
`FOX_ORCHESTRATOR=langgraph` flag (classic is default), shared
`Coordinator._exec_tool_call`, and a test suite (`tests/test_orchestrator.py`) covering
the tool loop, the check/refine gate, cooperative Stop, the step budget and parity of
side-effects with the classic loop. Remaining from the roadmap: SqliteSaver cross-restart
checkpoints, retry-with-backoff on transient LLM failures, and specialist sub-graphs.

## 1. Why

The agent's control loop today is a hand-rolled imperative loop in
`backend/agents/coordinator.py` (`Coordinator.run_turn`): stream → parse
`tool_calls` → execute → append → repeat up to `max_iters`. It is functional but
has structural reliability gaps that are hard to fix inside a flat loop:

1. No structured retry on transient LLM endpoint failures (a single blip ends the turn).
2. No explicit self-correction point (tool errors flow back as plain text, but there is
   no bounded reflect/repair step and no schema hint on retry).
3. No final-answer verification gate (the reviewer in `backend/agents/reviewer.py`
   runs *after* the turn, outside the loop, and cannot cause a retry).
4. No checkpointing — a server crash mid-turn loses the entire turn state.
5. No per-turn wall-clock budget; only a step counter.
6. No contract between loop stages, so each reliability fix is a new `if` in one big method.

LangGraph turns the loop into an explicit, testable state machine while we keep every
existing side-effect (streaming events, approvals, audit, artifacts, experiment/variant
recording, run records, cooperative Stop, MCP `server__tool` namespacing) unchanged.

## 2. Design principle

**Wrap — don't rewrite.** The tool layer, kernel, permission/approval broker, audit
emitter, artifact store and MCP registry stay exactly where they are. Only the control
loop in `Coordinator.run_turn` is replaced by a LangGraph graph, behind an off-by-default
env flag (`FOX_ORCHESTRATOR=langgraph`). The proven classic loop remains the default
until the graph reaches parity and the reliability features are proven by tests.

## 3. Architecture

### 3.1 The graph (one turn = one graph run)

```
 [input] ──► invoke ──► tool_calls? ──no──► check ──valid?──yes──► [final]
              │            │                    │
              │            │ yes                │ no (bounded)
              │            ▼                    ▼
              │         tools ◄──┐          reflect ──► invoke (repair)
              │            │     └────────────┘
              └────────────┴──────────────────────┘
```

### 3.2 Nodes (each a pure async function on the shared `AgentState`)

| Node | Responsibility | Existing behaviour preserved |
|------|----------------|------------------------------|
| `invoke` | Streams an LLM completion (keeps `stream_delta` events), emits `status`; returns assistant message + `tool_calls` or final text. | `llm.stream`, `_on_delta`, `_emit_status` |
| `tools` | Executes exactly one tool per node visit (LangGraph `ToolNode`), keeping the per-tool try/except, approval, audit, artifact-link, metric extraction and `tool_result` emit. Tool failures become structured error messages fed back. | `run_turn` tool block, `emit_tool_audit`, `_artifact_ids`, `_extract_metrics`, `tool_start`/`tool_result` |
| `reflect` | After a failed/partial step, asks the model for a corrected `tool_calls` plan (repair). Bounded by `max_reflect`. | — (new) |
| `check` | Verification gate using `llm.with_structured_output(schema)`: does the answer satisfy the ask, are cited numbers traceable to tool outputs, is anything unresolved. Routes to `[final]` or back to `reflect`. Bounded by `recursion_limit` + wall-clock budget. | — (new) |

### 3.3 State

`AgentState` (TypedDict) carries: `messages` (OpenAI-format transcript), `tool_calls`,
`phase`, `budget_left`, `aborted`, plus the existing `ctx` handle so every node can reach
the kernel, artifacts, audit, workflow and variant context. The store/message transcript
is the single source of truth for both the UI and the graph.

### 3.4 Tool binding

Existing tool schemas (`get_tool_schemas()` + MCP `_mcp_schemas`) are wrapped as
`langchain_core.tools.StructuredTool` objects with the same `<server>__<tool>` names, so
permissions, approval and audit keep working with zero changes.

### 3.5 LLM wiring

The graph uses `langchain_openai.ChatOpenAI` pointed at the local tool endpoint
(`FOX_TOOL_BASE_URL`, model `FOX_MODEL`, temperature 0 for gates) — the same local-only
endpoint the classic loop already uses for tool-calling turns. No cloud APIs are contacted.

## 4. Reliability mechanisms mapped to failure modes

| Failure mode today | Mechanism in the graph |
|---|---|
| LLM endpoint hiccup kills the turn | Retryable `invoke` with bounded backoff; final failure surfaces as a graceful error |
| Malformed tool-call JSON | Keep `parse_tool_call_json` as a parse step; agent-level `handle_parsing_errors` semantics |
| Tool fails, next attempt retries blindly | `reflect` node produces a corrected call; hard `max_reflect` budget |
| Final answer unsupported / off-target | `check` gate + bounded refine loop (JSON-schema enforced output) |
| Server crash mid-turn | LangGraph `MemorySaver` checkpoint (in-session resume); cross-restart `SqliteSaver` persistence is follow-up work |
| Turn runs forever | Per-turn wall-clock budget + `recursion_limit` (preserves `max_iters` semantics) |
| User hits Stop | `abort_event` checked in every node (same cooperative unwind) |
| Unsafe tool args | Approval broker + audit kept as middleware on `tools`, unchanged |

## 5. Integration points

- `backend/agents/orchestrator.py` *(new)* — builds the `StateGraph`, binds tools, exposes
  `run_orchestrated_turn(ctx, messages)` used by the coordinator.
- `backend/agents/coordinator.py` — `run_turn` dispatches on `FOX_ORCHESTRATOR` (read in
  `Coordinator.__init__`): `langgraph` → graph, otherwise the classic loop. All
  emits/audit/record calls moved into `Coordinator._exec_tool_call`, shared by both loops.
- `backend/agents/coordinator.py` — expose the existing tool function registry
  (`self.tools`) and schema list for tool binding.
- `pyproject.toml` — `[project.optional-dependencies] agent = ["langchain", "langchain-core", "langchain-openai", "langgraph"]`.

## 6. Migration strategy (behind the flag)

1. **Parity harness first**: a golden suite runs the *same* canned LLM transcript through
   both loops and asserts identical events / audit / run-records / artifacts.
2. Port node-by-node behind the flag; each port lands green.
3. Add the reliability nodes (`check`, `reflect`, checkpointing, retry) on top.
4. Flip the default once parity + reliability tests pass; classic loop remains available
   via `FOX_ORCHESTRATOR=classic`.

## 7. Testing (how "reliable" is proven)

- Mocked-LLM tests per node: malformed JSON → rescue; tool error → reflect; LLM outage →
  retried then surfaced; stop mid-tool; recursion-limit hit → graceful final.
- Golden parity runs: identical emitted events between classic and graph loops.
- Deterministic check/refine gate tests: accept vs. one-refine vs. give-up.
- Persistence test: interrupt after `tools`, rebuild from checkpoint, assert continuation.

## 8. Out of scope (future)

- Specialist sub-agents as separate graphs (EDA is the first candidate).
- `AgentExecutor`-based specialists (kept for the EDA layer).
- Remote/cloud LLM backends (must stay opt-in).
