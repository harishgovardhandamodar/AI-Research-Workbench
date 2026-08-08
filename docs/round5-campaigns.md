# Round 5 — Research campaigns: plan, execute, synthesize

Rounds 1–4 built the loop (steering, improvement, provenance). Round 5 adds the
**campaign**: a long-horizon autonomous run that plans a multi-step
investigation, executes each step as its own experiment using all the machinery
(goals, reviewer suggestions, sweeps, git lineage), and writes a synthesis
report. It generalizes the single-file autoresearch loop into whole studies.

## Findings (audit, 3 explore agents)
- **autoresearch** (`backend/autoresearch.py`) optimizes one `experiment.py` under
  a time budget with keep/revert on a `METRIC` line. Gaps: it funnels everything
  into one "autoresearch" experiment by name, never runs the reviewer/suggestions,
  omits `parent_run_id`/`model`/`code`/`env` on its own `add_run`, and double-records
  (its own row + the coordinator's `_record_run` row).
- **Substrate exists**: `run_improve_loop` (`experiment_loop.py:47`) is the exact
  structural template; `WorkflowTracker` + `set_invoke`/`retry_stage`
  (`workflows.py`, `main.py:1119`) is the resume primitive; `build_llm_messages`,
  `maybe_compact`, `_record_run` (full provenance), `Reviewer`, `build_run_report`
  are reusable as-is. `rt.lock` serializes one campaign per project; Stop
  interleaves via the receive loop. Resume must reconstruct from the persisted
  transcript + `invoke` metadata (not graph checkpoints — tool side-effects are
  non-replayable).
- **Synthesis primitives**: per-run `build_run_report` + `_summarize_run`
  (`runs.py:382/463`), per-experiment `_loop_summary` tables
  (`experiment_loop.py:326`), the RKG `run_synthesis` generate→review→improve
  pattern (`research_loop.py:691`). Nothing aggregates a project's own experiments
  into a study report yet.

## Design

### 1. Campaign data model (`backend/store.py`)
- `campaigns`: id, name, research_question, goal_metric, higher_better, status
  (`planned|running|done|failed`), report TEXT, created_at, updated_at.
- `campaign_steps`: id, campaign_id, step_order, title, kind
  (`experiment|sweep|comparison|synthesis`), hypothesis, plan, experiment_id,
  best_run_id, status, note, created_at, updated_at.
- Methods: `create_campaign`, `get_campaign`, `list_campaigns`,
  `update_campaign(status/report)`, `add_campaign_step`, `list_campaign_steps`,
  `update_campaign_step`. (Standard `try/except OperationalError` migration.)

### 2. Campaign loop (`backend/campaign.py`)
- `run_campaign(rt, coordinator, build_llm_messages, campaign_id, emit=None,
  workflow=None, resume_step=1, plan_steps=None)` modeled on `run_improve_loop`:
  1. **Plan** (only when no steps): LLM call returns a JSON array of steps
     `[{title, kind, hypothesis, plan}]` (robust parse + default fallback);
     persisted via `add_campaign_step`.
  2. **Execute** each step: create a per-step experiment (`create_experiment`),
     set `ctx.experiment_id`/`parent_run_id` (chained from the prior step's best
     run), add a user message + `coordinator.run_turn`, find the step's best run
     (`best_metric`), run the reviewer + `add_suggestions` (round-2/3), persist
     step status/best_run_id, advance the workflow stage, `maybe_compact` between
     steps, honor `check_abort` (persist before returning on Stop).
  3. **Synthesis**: aggregate each step's best run (metrics/goal/delta tables) →
     a campaign report (markdown) stored on `campaigns.report`, posted as a chat
     message (tags `["campaign","report"]`), and saved as a `kind="text"`
     artifact (pattern `runs.py:492-498`).
- `workflows.py`: `campaign_stages(n)` stage builder (mirror `improve_stages`).

### 3. Launch / resume (`backend/main.py`)
- `intent == "campaign"`: create the campaign from `text` (research question) +
  `msg_extra.campaign` config (goal_metric, higher_better, max_steps), then
  `run_campaign(...)` — mirror the `improve_loop` block (`main.py:1104-1118`).
- `/campaign <question>` slash command (`_run_slash_command`).
- Extend `retry_stage` (`main.py:1119-1141`) to `kind == "campaign"` →
  `run_campaign(resume_step=N)`.
- `GET /api/projects/{name}/campaigns` + `GET .../campaigns/{id}` REST routes.

### 4. UI
- A "Run campaign" quick-action button (`data-intent="campaign"`) and `/campaign`
  usage in help. Everything else rides the existing UI: workflow panel stages,
  chat step messages, Experiments graph/branches (steps are runs chained by
  `parent_run_id`), and the report rendering as markdown + artifact.

## Files touched
- `backend/store.py`, `backend/campaign.py` (new), `backend/workflows.py`,
  `backend/main.py`, `backend/routers/runs.py`, `frontend/index.html`,
  `frontend/app.js`, `docs/round5-campaigns.md`, `tests/test_round5.py` (new).

## Out of scope
- Cross-project / background (detached-from-socket) campaigns — one campaign per
  project, running inside a turn under `rt.lock`.
- LangGraph SqliteSaver mid-turn resume (tool side-effects are non-replayable);
  campaign resume reconstructs from the persisted transcript + `invoke` metadata.
