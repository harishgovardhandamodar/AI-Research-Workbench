# Architecture

## Overview

```
┌────────────────────────────────────────────────────────────────┐
│  Frontend (frontend/ — plain HTML/JS, no framework)            │
│  Chat · Experiments · Agent · Editor · Graphs · Audit · VS Code│
└───────────────▲────────────────────────────────────────────────┘
                │ HTTP + WebSocket
┌───────────────┴────────────────────────────────────────────────┐
│  Backend (backend/ — FastAPI + uvicorn, one process)           │
│  main.py (WS chat + turn loop)                                 │
│  routers/*  →  project_runtime.py  →  store.py (SQLite)        │
│  agents/    →  coordinator (classic loop) + orchestrator       │
│              (langgraph), tools, reviewer                      │
│  campaign.py / eval.py / report.py / export.py / next_research │
│  experiment_repo.py (git snapshots)  ·  audit.py               │
│  kernels/   →  PythonKernel (JSONL subprocess) + RKernel + RKG │
└───────────────┬────────────────────────────────────────────────┘
                │
     ┌──────────┼───────────────┐
   Python      Rscript        Research Knowledge Graphs
   kernel      kernel         (corpus + RAG + scheduler)
```

## Backend modules

| Module | Responsibility |
|---|---|
| `main.py` | App factory, routers, WebSocket chat (`ws_chat`, `handle_turn`), slash commands, background task wiring |
| `project_runtime.py` | Per-project state (store, kernels, notebooks, workflow, audit), message-context builders, event bus, background campaign/eval runner |
| `store.py` | SQLite persistence (messages, runs, experiments, goals, learnings, campaigns, evals, suggestions, settings) |
| `agents/coordinator.py` | The classic agent loop + shared `_exec_tool_call` (tools, audit, artifacts, metrics, transcript, run record) |
| `agents/orchestrator.py` | Opt-in LangGraph state machine with a QA gate |
| `agents/tools.py` | The tool registry (`run_python`, `run_sweep`, notebooks, editors, RKG, …) |
| `agents/reviewer.py` | Background reviewer + `build_review_context` |
| `experiment_loop.py` | The improve loop (bounded, reviewer-driven, resumable) |
| `autoresearch.py` | Karpathy-style single-file optimizer |
| `campaign.py` | Multi-step research campaigns (plan → execute → synthesize) |
| `eval.py` | Model benchmarks |
| `report.py` / `export.py` | Project report + zip export |
| `next_research.py` | Next-research agenda |
| `literature.py` | RKG RAG grounding for planning/review/reports |
| `experiment_repo.py` | Management-repo git snapshots + commit/push/restore |
| `workflows.py` | WorkflowTracker (stage state machine, broadcast, resume metadata) |
| `audit.py` | Hash-chained, redacted audit events + deviation scanner |
| `kernels/` | Python kernel (JSONL subprocess), R kernel, kernel manager, headless server |
| `research_knowledge_graphs/` | The RKG subsystem (corpus, RAG, scheduler, scenarios) |

## The agent loop

`Coordinator.run_turn` runs a bounded ReAct loop: stream → tool calls →
execute each tool → append → repeat. `_exec_tool_call` is the single choke
point shared by the classic loop and the LangGraph orchestrator, so every path
produces identical events, audit, artifacts, and run records.

Per-turn recording: `_record_run` writes one `runs` row with the variant's
config/label, merged metrics, full code, environment, model, message id, and
integrity hash; the chat path also auto-commits to the management repo.

## Kernel

`PythonKernel` spawns `worker.py` as a JSONL subprocess (request/response over
stdin/stdout with an `asyncio.Lock`); on timeout it kills and restarts. State
persists across calls until reset. A headless `fox-kernel` server exposes the
same protocol over HTTP+WebSocket for remote execution.

## WebSocket chat

One WebSocket per connection; a per-project `asyncio.Lock` serializes turns.
The receive loop handles approvals/ping/stop directly off the queue, so
approvals resolve and Stop works even mid-campaign. Background campaigns run
under the same lock with their own coordinator and broadcast via the project
event bus.

## Concurrency & thread-safety

- The SQLite connection is used only on the event-loop thread (report/export run
  there too).
- `LLMClient` retries transient failures with backoff (round 12).
- Background tasks (campaigns/evals) are `asyncio.Task`s — one per project —
  serialized under the project lock; resume reconstructs from persisted state
  (tool side-effects are not replayable, so no graph-checkpoint resume).
