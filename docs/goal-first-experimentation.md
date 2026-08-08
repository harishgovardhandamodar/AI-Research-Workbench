# Round 2 — Goal-first experimentation, objective co-design & data-aware steering

Follow-up to `docs/chat-ui-ux-redesign.md` (round 1, committed as `0521907`). Round 1 fixed
the Chat surface (edit/retry/delete, `@schema` cards, model-dropdown grouping, experiment
context injection, inline next-steps, quick actions). Round 2 audits the **end-to-end
experimentation loop** — from a thought/objective to a measured run to the next experiment —
and closes the gaps that make the objective and the data invisible to both the user and the
agent.

## Findings (verified against the code)

### A. The objective is split, stale, and never measured
1. **Two disconnected goal systems.** `experiments.goal_metric/target` steers the improve
   loop, the context block, and the leaderboard; the `goals` table (Goals panel) only feeds
   ephemeral chat notices (`backend/main.py:609-660`). `store.goals_for_experiment` is never
   called (`backend/store.py:408-412`).
2. **Objective drift.** `_experiment_context()` picks the "most recently updated active
   experiment" (`backend/project_runtime.py:118-121`) but `updated_at` only moves on create /
   status change — never on new runs, so the model silently re-focuses after any experiment
   is created.
3. **No objective refinement.** The only PATCH route changes `status`; hypothesis/goal/target/
   plan are immutable after creation (`backend/routers/runs.py:111-124`), forcing
   recreate-and-orphan.
4. **No distance-to-target.** Every comparison is run-vs-best-run. The leaderboard
   (`backend/experiments.py:421-455`) has no "to target" / "% of target"; nothing measures a
   run against the user's actual objective, and there is no auto "goal reached" completion.
5. **Reviewer is blind to the goal.** It reviews only the last 8 transcript messages
   (`backend/agents/reviewer.py:47,54`) with no metrics table, no goal, no best-so-far — so
   suggestions are generic instead of objective-driven.

### B. The agent cannot see the data
6. **Uploads land in the project dir; kernels run from the repo root** (`backend/kernels/
   manager.py:42`) and the agent has no listing of available project data files. `@schema` is
   UI-only; the model still "loads the CSV blind."

### C. Model suggestions are cosmetic
7. `GET /api/models` keeps only `{id, owned_by}` (`backend/llm.py:62-63`); family/size are
   regex-guessed in the browser (`frontend/app.js:2258-2273`). No size/quantization metadata,
   and the settings picker is a bare-id datalist while the topbar shows rich labels.

## Redesign (implemented in this branch)

### 1. One goal system (back-end)
- `store.add_run` bumps the owning experiment's `updated_at` → context selection tracks
  recent activity instead of creation time.
- `store.update_experiment(...)` edits name/hypothesis/goal_metric/goal_target/higher_better/
  plan; the PATCH route accepts fields with validation (numeric target; target requires a
  metric).
- `_experiment_context()` now:
  - prefers the project's **focus experiment** (`focus_experiment_id` setting), else the most
    recently active;
  - merges the experiment goal **and** Goals-panel goals (scoped + project-wide);
  - reports **distance to target** for the best run;
  - adds **cross-experiment memory** (best value for the same metric across all runs);
  - lists **project data files** (name + size) so the agent knows what exists.
- Improve loop treats the experiment + Goals-panel goals as stop conditions and **auto-marks
  the experiment completed** when a target is reached.
- Reviewer receives an "Experiment context" block (goal, target, direction, best-so-far, this
  run's metrics) so suggestions chase the objective.

### 2. Focus experiment — end objective drift (back-end + UI)
- Project setting `focus_experiment_id` + `GET/POST /api/projects/{name}/experiments/focus`.
- `/focus <name|id|off>` slash command; `_resolve_experiment_id` and context selection prefer
  the focus; free-form turns inherit the focus id so runs/timelines attach automatically.

### 3. Objective refinement UI (back-end + UI)
- ✎ edit button on each experiment card opens a modal pre-filled with all objective fields;
  save PATCHes it.

### 4. Measure against the objective, not just the best run (back-end + UI)
- `rank_runs(..., goal_target)` adds `to_target` / `pct_target` per row; the ranking route
  passes the experiment target and the leaderboard gains a "to target" column.

### 5. Model suggestions (back-end + UI)
- `llm.list_models()` enriches results via the native Ollama `/api/tags` (parameter size +
  quantization) when reachable (best-effort).
- Settings datalist uses the same rich labels as the topbar (`id · size · family`).

## Files touched
- `backend/store.py`, `backend/routers/runs.py`, `backend/agents/reviewer.py`,
  `backend/experiment_loop.py`, `backend/main.py`, `backend/project_runtime.py`,
  `backend/experiments.py`, `backend/llm.py`, `frontend/app.js`, `frontend/index.html`,
  `frontend/styles.css`, `tests/test_goal_steering.py` (new).

## Deferred (documented, not built)
- Parallel variant fan-out / parameter sweeps (single-kernel, sequential coordinator today).
- First-class suggestion records (id/status/regression check).
- Revert-to-best-run and non-metric run diffs.
- Workflow-stage retry from the panel.
- Per-experiment model pinning.
