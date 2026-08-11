# Fox Workbench — Lifecycle Hardening To-Do List

Gap analysis of the chat → planning → drafting → constructing → managing → running
experiment lifecycle (stability, orchestration, robustness, traceability, loggability).

## Phase 1 (in progress)

- [x] **Item 1 — Unify plan records.** Persist plan/proposal/step as a single
  ProjectStore record linked to the run lineage; add a unified, queryable plan
  record.
  - Added `experiment_plans` SQLite table (ProjectStore) — the unified plan
    record (`upsert_plan` / `get_plan_record` / `list_plan_records` /
    `plan_runs`). `backend/store.py`.
  - Added `runs.plan_id` + `runs.plan_step_id` columns (with migrations) through
    `add_run` / `begin_run` / `finish_run` / `_row_run`. Plan linkage is lineage
    metadata, excluded from the integrity hash so old runs keep verifying.
  - Planner router now mirrors every plan mutation into ProjectStore
    (`_sync_plan`) and records plan runs with `plan_id` (`present_result`);
    added `GET …/experiment-plans/{plan_id}/runs`. `backend/routers/experiment_planner.py`.
  - Coordinator records `plan_id` / `plan_step_id` on turn runs; `plan_step`
    intent threads `plan_step_id` through. `backend/agents/coordinator.py`,
    `backend/main.py`.
  - NOTE: `PlanStore` JSON is kept as the cross-process source of truth (the
    separate experiment-planner MCP process reads it); the SQLite table is the
    unified queryable mirror + run-lineage index. Full JSON retirement deferred.

- [x] **Item 2 — Second-class runs → first-class.** Route tool-produced runs
  through the two-phase `begin_run`/`finish_run` lifecycle for structured
  `error` capture + integrity hash + a pre-created run_id.
  - `tools.py`: new `_record_tool_run` helper; `_sweep_point`, `_run_finetune`
    and `_run_notebook` now record first-class runs (sweep failures persist a
    structured `error` on the run row).
  - `main.py`: peer-experiment run carries `plan_id` when driven from a plan.
  - Note: audit events per sweep-point run remain a Phase-2 item (kernel events
    from ephemeral pool kernels aren't subscribed to the audit trail).

- [x] **Item 3 — Graceful drain in `ProjectRuntime.stop()`.** Requests stop on
  background work, awaits campaign/eval tasks (they persist resumable points),
  cancels stragglers after a timeout, then stops the finetune monitor, audit
  emitter and kernels. `backend/project_runtime.py:620`.

- [x] **Regression tests.** `tests/test_lifecycle_round2.py` (8 tests): plan
  linkage + integrity, plan-record upsert/query/lineage, first-class tool runs,
  graceful-drain/cancel. Full suite: 713 passed.

## Phase 2 (in progress)

- [x] **Item 4 — Sweep kernel-pool cleanup on abort.** `ToolContext` now tracks
  ephemeral kernels (`active_kernels`, `register_kernels`/`unregister_kernels`/
  `stop_kernels`); `_run_sweep` registers the pool and stops it via
  `asyncio.shield` (cancellation-proof), and the coordinator's `run_turn`
  `finally` stops any kernels still open on abort/failure. `tools.py`,
  `coordinator.py`.
- [x] **Item 5 — Extend integrity hashing to messages.** Per-message hash chain:
  `messages.prev_hash` + `messages.integrity_hash` (with migrations), computed in
  `add_message`; `verify_message_chain()` detects content edits and chain breaks
  (legacy rows skipped, not errors); new `GET …/messages/verify` endpoint.
  `store.py`, `routers/runs.py`.
- [x] **Item 6 — Persist LLM request fidelity.** Each turn persists the exact
  assembled message list + LLM params (model/temperature/max_tokens) as a
  `kind="transcript"` artifact linked to the run — reproducible even after
  compaction summarizes the conversation. `coordinator.py` `_persist_transcript`.
- [x] **Item 7 — Correlation in logs + close silent gaps.** `logging_config`
  gained a contextvar-based correlation context (`set_log_context` /
  `clear_log_context`) and a `ContextFormatter` that appends `project=/run=/…`
  to every log line; wired into `handle_turn`, `coordinator.run_turn`, and
  campaign/eval tasks. `experiment_repo` auto-commit failure now logs via
  `fox.experiment_repo` instead of `print`.
- [x] **Item 8 — Per-point abort checks + conservative tool retry.** Sweep checks
  `ctx.check_abort` before starting and between sequential points; the
  coordinator sets it per turn. Read-only/idempotent tools (`editor__read_file`,
  `editor__list_files`, `editor__open`, …) retry once on a transient exception;
  mutating tools never auto-retry.
- [x] **Phase 2 regression tests.** `tests/test_lifecycle_round2.py` extended to
  18 tests (message chain, kernel-pool tracking, log correlation, transcript
  fidelity). Full suite: **723 passed**.

## Phase 3 (in progress)

- [x] **A1 — Unified plan-in-flight registry.** Plan executions now live on
  ProjectRuntime (`_plan_tasks`, `plan_running`/`launch_plan`/`cancel_plan_task`/
  `drain_plans`), shared by the chat (`main.py` `_execute_plan`) and REST
  (`routers/experiment_planner.py` `run_plan`) executors so a plan can't be
  double-launched across paths. The module-global `_run_tasks` is gone, and
  `ProjectRuntime.stop()` drains plan tasks on shutdown.
  `project_runtime.py`, `routers/experiment_planner.py`, `main.py`.
- [x] **A2 — Abort in-flight turn on disconnect.** `ws_chat` now sets
  `abort_event` in its `finally`, so a normal turn unwinds cleanly at the next
  LLM/tool boundary when the tab closes (detached campaigns/evals unaffected).
  `main.py`.
- [x] **B — Audit sweep-point runs.** `_sweep_point` pre-creates each point's
  run row (`begin_run`) and emits an `emit_tool_audit` event linked to its own
  `run_id` before `finish_run` — per-point kernel executions are now traceable.
  `tools.py`.
- [x] **C — Artifact integrity hashing.** `ArtifactStore` hashes artifact bytes
  (or stable metadata fields for data-less artifacts, excluding the
  link-mutated `message_id`/`run_id`) into `artifacts.integrity_hash` (migration
  included); `verify_artifact`/`verify_artifacts` + `GET …/artifacts/verify` and
  `…/artifacts/{id}/verify` endpoints. `artifacts/store.py`, `routers/artifacts.py`.
- [x] **D — REST log-correlation middleware.** `@app.middleware("http")` sets the
  project context for `/api/projects/{name}/…` requests so router logs are
  greppable by project. The remaining `except: pass` blocks audited are all
  intentional best-effort paths (JSON rescue, skip-broken-notebook, optional
  git/file ops); the one real silent gap (experiment-repo auto-commit failure)
  was already converted to `fox.experiment_repo` logging. `main.py`.
- [x] **Phase 3 regression tests.** `tests/test_lifecycle_round2.py` now 23 tests
  (plan dedup/drain, artifact integrity, sweep-point audit). Full suite:
  **728 passed**.

## Phase 4 (in progress)

- [x] **Item 9 — Whole-turn wall-clock budget.** `Coordinator` gained a
  `turn_timeout` (seconds, 0 = unlimited; wired from
  `CONFIG["agent"]["turn_timeout"]`) enforced at LLM/tool boundaries: a turn
  that runs past its budget stops gracefully, persists the run as `stopped`,
  and tells the user progress was saved. `backend/agents/coordinator.py`,
  `backend/project_runtime.py`, `backend/main.py`.
- [x] **Item 10 — Kernel restart transparency.** `PythonKernel` tracks a
  `restarts` counter (incremented when `_send` detects a dead subprocess and
  auto-restarts, exposed via `status()` and a `reset`/`restarted` notify); the
  coordinator records `_kernel_restarts` into the run's env snapshot and logs a
  warning, so a run is honest about kernel state loss.
  `backend/kernels/python_kernel.py`, `backend/agents/coordinator.py`.
- [x] **Item 11 — Deployment verification.** Rebuilt the Docker image
  (`docker compose up -d --build fox`) and booted `fox-workbench` (healthy).
  Verified against the existing seeded `mrm-sample-session`: schema migrations
  applied cleanly (runs.plan_id/plan_step_id/error, messages chain columns,
  artifacts.integrity_hash, `experiment_plans` table), and the new
  `…/messages/verify` + `…/artifacts/verify` endpoints report all pre-migration
  rows as "skipped" (no errors/mismatches).
- [x] **Phase 4 regression tests.** Turn budget (stops + records `stopped`,
  and no-budget completes), kernel restart counting (`tests/test_kernel.py`),
  and restart surfacing in run env. Full suite: **732 passed**.

## Phase 5 (in progress)

- [x] **Item 12 — Campaign/eval kernel isolation + concurrent chat.** Background
  campaigns and evals now run on a **dedicated kernel** (`make_kernel_manager`)
  instead of the chat kernel, and no longer hold `rt.lock` — the user can keep
  chatting while a campaign runs, and the two can't clobber each other's kernel
  state. The dedicated kernel is subscribed to the audit trail and stopped in
  the task `finally`. `project_runtime.py` (`ctx()` gains a `kernels` override).
- [x] **Compaction is audited.** `maybe_compact` emits a `compaction` audit
  event (folded count, cutoff, kept, whether an LLM summary was used) so
  summarization is traceable. `project_runtime.py`.
- [x] **Cross-thread SQLite fix (latent bug).** `connect_project_db` now uses
  `check_same_thread=False` — the cached project connection may be touched from
  both the event-loop thread and `asyncio.to_thread` workers (experiment
  execution / git auto-commit). Previously a connection first created in a
  worker thread broke later main-thread access with `ProgrammingError`.
  `backend/store.py`.
- [x] **Test hygiene: fixed monkeypatch leaks.** `tests/test_goal_steering.py`
  and `tests/test_round9.py` patched `runs.get_runtime` without restoring it,
  corrupting every later `backend.routers.runs` endpoint test (and causing the
  intermittent `focus`/`compare` route-test flakiness). Both now restore the
  original in `finally`.
- [x] **Phase 5 regression tests.** `tests/test_lifecycle_round2.py` now 30
  tests (+ verify-endpoint, compaction-audit, campaign-kernel-isolation). Full
  suite: **736 passed**. Docker image rebuilt; `fox-workbench` healthy with the
  seeded project intact.

## Phase 6 (in progress)

- [x] **Item 13 — Unified project status endpoint.** `ProjectRuntime.status()`
  + `GET /api/projects/{name}/status` expose in-flight campaigns/evals/plans
  (with ids), kernel health incl. `restarts`, the workflow snapshot, and audit
  stats (event count, open deviations). Closes the "no unified in-flight view"
  gap. `project_runtime.py`, `routers/projects.py`.
- [x] **Item 14 — Sweep kernel-pool cap.** A grid bigger than
  `MAX_SWEEP_KERNELS` (8) spawns at most 8 ephemeral kernels; the excess points
  run sequentially on the main kernel (parallel+sequential hybrid), so a huge
  grid can't spawn hundreds of subprocesses. `tools.py`.
- [x] **Item 15 — Durable campaign resume.** `campaigns.resume_step` (migration)
  is persisted at the end of every `run_campaign`; `campaign_resume_step()`
  derives the next step from persisted step statuses, so Resume works after a
  restart / interruption / concurrent chat — no longer dependent on the volatile
  workflow snapshot. `retry_stage` and `recover_campaigns` use it; interrupted
  running steps reset to `planned`. `store.py`, `campaign.py`, `main.py`,
  `project_runtime.py`.
- [x] **Phase 6 regression tests.** `tests/test_lifecycle_round2.py` now 34
  tests (+ status endpoint, durable resume, sweep cap). Full suite: **740
  passed**. Docker image rebuilt; `/status` verified live.

## Phase 7 (in progress)

- [x] **Item 16 — Plan executions are audited.** `present_result` now emits
  `plan_started` / `plan_completed` / `plan_failed` / `plan_cancelled` audit
  events linked to the plan run's `run_id` (with dataset/seed/steps/metrics/
  error), so deterministic plan executions are visible in the audit trail
  (previously zero audit coverage). `routers/experiment_planner.py`.
- [x] **Item 17 — Durable improve-loop resume.** `run_improve_loop` persists a
  durable `improve_latest` resume record (kind/experiment_id/iterations/prompt)
  to settings; `retry_stage` falls back to it when a concurrent campaign/chat
  turn clobbered the volatile workflow snapshot. `experiment_loop.py`, `main.py`.
- [x] **Item 18 — Compaction concurrency guard.** `maybe_compact` is guarded by
  an in-flight flag so two interleaved compactions (it awaits the LLM mid-body)
  can't double-fold overlapping ranges. `project_runtime.py`.
- [x] **Phase 7 regression tests.** Plan audit lifecycle events, improve-loop
  resume persistence, and the compaction guard. Full suite: **743 passed**.

## Phase 8 (in progress)

- [x] **Item 19 — Independent campaign/eval stop flags.** Phase 5 made campaigns
  and evals runnable concurrently (dedicated kernels, no shared lock), but they
  still shared one `campaign_stop` flag — stopping the eval stopped a concurrent
  campaign and vice-versa. Added a separate `eval_stop`; `stop_eval`/`start_eval`
  and the eval coordinator now use it, and `stop()` sets both.
  `backend/project_runtime.py`.
- [x] **Item 20 — Eval-level audit events.** `run_eval` now emits
  `eval_started` / `eval_completed` / `eval_failed` audit events (eval id, name,
  goal metric, models, error), so model benchmarks have an eval-level audit
  trail. `backend/eval.py`.
- [x] **Item 21 — Status endpoint enrichment.** `/status` now also reports the
  running campaign's durable `campaign_resume_step` and the `improve_latest`
  resume record. `project_runtime.py`, `routers/projects.py`.
- [x] **Phase 8 regression tests.** Separate-stop-flag isolation, eval audit
  events, status enrichment. Full suite: **747 passed**.

## Phase 9 (in progress)

- [x] **Item 22 — Idle runtime eviction (opt-in).** Each opened project keeps a
  `ProjectRuntime` (kernel subprocesses + SQLite connection + audit emitter)
  cached forever in `runtimes`. New `agent.runtime_idle_timeout` config (seconds,
  0 = disabled) plus a lifespan loop evict runtimes that are idle (no chat
  subscribers, no campaign/eval/plan tasks, kernel idle) past the timeout —
  `get_runtime()` bumps `last_active` on every access, so the frontend keeps a
  project alive while polling. `ProjectRuntime.is_busy()`/`evict()`.
  `project_runtime.py`, `state.py`, `main.py`.
- [x] **Item 23 — Eval retry in `retry_stage`.** Retrying a failed eval stage now
  re-runs the whole model benchmark (`run_eval`) instead of erroring.
  `main.py`.
- [x] **Item 24 — Kernel audit session context.** Kernel lifecycle/execution
  audit events now carry the project `session_id`, so they correlate with the
  rest of a project's audit trail. `project_runtime.py`.
- [x] **Phase 9 regression tests.** `is_busy`/`evict` behaviour, and kernel
  audit session context. Full suite: **752 passed**.

## Phase 10 — ten improvement rounds (each committed)

- [x] **R1 — MCP resilience** (`backend/mcp.py`): close+reconnect on any tool-call
  failure (not just timeout), a 3-strike circuit breaker, and per-server
  healthy/failures in `/api/mcp` status. `d0a3b85`
- [x] **R2 — Campaign step retry** (`backend/campaign.py`): a failed step is
  retried up to `step_retries` (default 2) with per-attempt notices instead of
  failing the whole campaign. `f1e8a96`
- [x] **R3 — Cascade cleanup** (`store.py`, `artifacts/store.py`): deleting a plan
  now drops its `experiment_plans` mirror; `sweep_orphans()` removes artifact
  files with no DB row. `e84f861`
- [x] **R4 — Kernel crash logging** (`python_kernel.py`): auto-restart logs the
  exit code + stderr tail and records the reason in `last_error`. `721e328`
- [x] **R5 — API request logging** (`main.py`): middleware logs one line per API
  call (method, path, status, duration) with project correlation. `2769495`
- [x] **R6 — Bounded chat queue** (`main.py`): the WS incoming queue is capped at
  64; excess messages are dropped with a notice instead of buffering unbounded.
  `cdbacac`
- [x] **R7 — Enriched health** (`routers/system.py`): `/api/health` reports
  loaded runtimes + busy count + in-flight campaigns/evals/plans. `b877303`
- [x] **R8 — Pinned-model fallback** (`coordinator.py`, `store.py`): an
  unavailable per-experiment model falls back to the default once; `finish_run`
  accepts a model override so the run records the model actually used. `3b6ec43`
- [x] **R9 — Notebook per-cell timeout** (`notebooks.py`): each cell (and the
  prelude) runs under a configurable `cell_timeout` (default 120s). `0739452`
- [x] **R10 — Eval retry reuse** (`eval.py`): re-running an eval skips models
  already benchmarked, reusing their best result; `run_eval` returns per-model
  results including `skipped`. `a32f17d`
- [x] Full suite after the 10 rounds: **758 passed**.
