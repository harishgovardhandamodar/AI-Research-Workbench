# Round 12 — Resilient & proactive autonomy

The workbench runs long, autonomous research — but a transient LLM failure
(Ollama restarting, a dropped connection, a timeout) currently kills a turn or a
campaign step outright, and nothing *proactively* tells the user what to do
next. Round 12 makes the loop **resilient** (retry-with-backoff on transient LLM
errors) and **proactive** (a "next research" agenda derived from the recorded
gaps and learnings).

## Design

### 1. LLM retry-with-backoff (`backend/llm.py`)
- `LLMClient(..., retries=2, retry_backoff=1.0)`: `complete` and `stream` retry
  the underlying `chat.completions.create` on transient exceptions
  (connection/timeout/`ConnectionError`), sleeping `retry_backoff * attempt`
  between tries, then raise `LLMError`. Successful responses return immediately.
- Applies everywhere automatically (coordinator `stream`, reviewer `complete`,
  report exec-summary) since they all go through the client.

### 2. Next-research agenda (`backend/next_research.py`)
- `next_research_agenda(rt) -> str` — deterministic aggregation of the recorded
  state:
  - **Push toward target**: experiments with a target not yet reached.
  - **Unfinished work**: campaigns/evals not `done`.
  - **What didn't work**: no-gain learnings.
  - **What worked**: improved learnings (top few).
  - **Untested models**: models not yet benchmarked (vs `list_models`).
  - **Open goals**: Goals-panel entries without a reached target.
  Empty → "the project looks settled; try a new question or a model benchmark."
- `suggest_next_research(rt) -> str` (async) — the agenda + an optional LLM
  paragraph proposing a concrete next campaign ("Suggested next campaign: …"),
  best-effort.

### 3. Endpoint + UI
- `GET /api/projects/{name}/next` → `{"agenda": str}` (deterministic, fast).
- Experiments header: a **▶ Next** button posts the agenda to chat and offers a
  "Run as campaign" quick action that starts a background campaign seeded with
  the agenda as its research question.

## Files touched
- `backend/llm.py`, `backend/next_research.py` (new), `backend/routers/runs.py`,
  `frontend/app.js`, `frontend/index.html`, `docs/round12-resilient.md`,
  `tests/test_round12.py`.

## Out of scope
- Full LangGraph SqliteSaver mid-turn resume (tool side-effects are non-
  replayable); retry-with-backoff covers transient LLM errors within a turn.
