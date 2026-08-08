# Round 3 — Close the loop: suggestions lifecycle, runs/reverts, sweeps, workflow retry, model pinning

Rounds 1–2 made the chat surface clean and steered the agent toward a goal
(focus experiment, objective refinement, goal-grounded reviewer, distance-to-target).
Round 3 closes the *improvement loop*: it makes each suggestion traceable and
measurable, lets the user diff/revert runs, run parallel parameter sweeps, retry
failed workflow stages, and pin a model per experiment.

## Findings (audit, 4 explore agents)
- **Suggestions are ephemeral**: an anonymous `{title, action, prompt}` inside the
  `runs.review` JSON blob. "Apply & rerun" sends only the prompt (`app.js:981/1018`)
  with no suggestion id; the server links the new run via a "last run" heuristic
  (`main.py:1166`) and never records which suggestion was applied or whether it
  improved the goal. The improve loop blindly takes `suggestions[0]`
  (`experiment_loop.py:232`) with no de-dup and no regression stop.
- **No run diff / revert**: only metric deltas exist (`compare_runs`,
  `experiments.py:216`). Config/tool diffs are not shown; no revert-to-best action.
- **Single sequential kernel** (`kernels/manager.py:44`, `python_kernel.py:37`):
  zero parallelism today; but each `PythonKernel` is an independent subprocess, so a
  pool is straightforward. `parent_run_id` (schema-ready) keeps the branch graph
  correct under concurrency; the shared store connection is only safe if all writes
  stay on the event loop.
- **Workflow stages are a display state machine** (`workflows.py:79`): no per-stage
  action, no stored invoke args, no retry primitive. Improve loop always restarts at
  iteration 1.
- **Model is global-only**: fixed at `LLMClient` construction (`llm.py:38`); the
  experiments table has no `model` column; `runs.model` records the label only.

## Design

### 1. First-class suggestions + regression check
- `backend/store.py`: new `suggestions` table (id, experiment_id, source_run_id,
  run_id, title, action, prompt, status `pending|applied|accepted|rejected`,
  baseline_value, outcome_value, delta, improved, created_at, applied_at) +
  methods `add_suggestions`, `get_suggestion`, `list_suggestions`,
  `mark_suggestion_applied`, `resolve_suggestion_outcome`.
- `backend/main.py`: after each review, `add_suggestions` and attach `id`s to the
  WS review payload; on `rerun_suggestion` with `msg_extra.suggestion_id`, mark
  applied, set `parent_run_id = source_run_id`, and resolve the outcome after the
  turn (regression check via the goal metric, reusing `best_metric`).
- `backend/experiment_loop.py`: persist suggestions per iteration, select the first
  **pending** suggestion, resolve outcomes, record `delta`/`improved` in history,
  and stop after 2 consecutive applied suggestions that fail to improve.
- `frontend/app.js`: apply buttons send `suggestion_id`; render status badges;
  disable already-applied suggestions.

### 2. Per-experiment model pinning
- `backend/store.py`: `experiments.model` column (migration); accept `model` in
  `create_experiment`/`update_experiment`/`_row_experiment`.
- `backend/llm.py`: `complete`/`stream` accept optional `model` override threaded
  into `_params`.
- `backend/agents/coordinator.py`: resolve the pinned model per turn from the
  experiment (fallback focus → global), pass to `llm.stream`, set `model_name`
  for records/labels.
- `frontend`: Model field in the experiment edit modal; show pinned model on cards.
- Route: `create_project_experiment` / PATCH accept `model`.

### 3. Run diff + revert-to-best
- `backend/experiments.py`: `run_diff(a, b)` — config key diff, metric deltas
  (reuse `compare_runs`), tool-sequence added/removed/failed.
- `backend/routers/runs.py`: `GET /runs/{rid}/diff?run_b=`; `POST /runs/{rid}/revert`
  handled as WS intent `rerun_run` (re-sends the run's prompt as a fresh turn,
  `parent_run_id = rid`).
- `frontend`: diff view in the branch/run detail; "Revert to this run" on ranking.

### 4. Workflow-stage retry
- `backend/workflows.py`: `WorkflowTracker.set_invoke(...)` + `invoke` in snapshot
  (persisted with `workflow_latest`): `{kind, experiment_id, prompt, iterations}`.
- `backend/experiment_loop.py`: `run_improve_loop(..., start_at=1)` — resume from
  iteration N (stage ids offset, lineage from the best prior run).
- `backend/main.py`: WS intent `retry_stage` → for improve workflows re-run the
  loop from the failed iteration.
- `frontend/app.js`: retry button on failed stages.

### 5. Parallel parameter sweeps
- `backend/kernels/manager.py`: `KernelManager.pool(n)` spawns extra independent
  `PythonKernel` instances (local mode).
- `backend/agents/tools.py`: `run_sweep(code, configs, label_prefix)` tool — runs
  the snippet on `min(n, len(configs))` kernels via `asyncio.gather` (all store
  writes on the event loop), records one run per config
  (`kind="sweep"`, explicit `parent_run_id`), returns a markdown summary.
- Agent-discoverable, so users can ask "sweep eps over {0.5,1,2}".

## Files touched
- `backend/store.py`, `backend/llm.py`, `backend/agents/coordinator.py`,
  `backend/agents/tools.py`, `backend/agents/reviewer.py` (no change expected),
  `backend/experiments.py`, `backend/experiment_loop.py`, `backend/main.py`,
  `backend/workflows.py`, `backend/kernels/manager.py`,
  `backend/routers/runs.py`, `frontend/app.js`, `frontend/index.html`,
  `frontend/styles.css`, `tests/test_round3_*.py` (new).

## Out of scope (unchanged)
- Git-backed run reverts (management repo commit hash not yet recorded per run).
- Full code diffs (code survives only in ≤200-char tool snippets / artifacts).
- Distributed/remote kernel sweeps (`RemoteKernelManager` pool).
