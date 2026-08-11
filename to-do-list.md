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
