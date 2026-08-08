# Round 7 — Learnings & knowledge memory

Rounds 2–6 taught the workbench to run experiments, measure suggestions, and
orchestrate campaigns — but the knowledge gained is **ephemeral**: it lives only
in chat text and per-run reports. Round 7 adds a **compounding memory**: every
measured outcome becomes a structured *learning* that is fed back into the
agent's context, the reviewer, and the campaign planner, so each experiment
starts from what earlier ones already discovered.

## Design

### 1. Learnings store (`backend/store.py`)
- `learnings` table: id, experiment_id, run_id, metric, baseline_value,
  outcome_value, delta, improved INTEGER (1/0/null), summary TEXT, source TEXT
  (`suggestion` — an applied reviewer suggestion, or `best` — a new-best run),
  created_at. Migration via the standard `try/except OperationalError` guard.
- Methods: `add_learning`, `list_learnings(experiment_id, metric, limit)`,
  `delete_learning(id)`.

### 2. Capture — deterministic, from what we already measure
- **Resolved suggestions** (the R3 regression check): when a suggestion is
  resolved as `accepted`/`rejected`, record a learning
  `"Tried '<title>': <metric> <baseline>→<outcome> (<delta:+.3g>) — improved"`
  (or "no gain"). Hook points: `experiment_loop.run_improve_loop` (after
  `resolve_suggestion_outcome`) and `main.py`'s `rerun_suggestion` path (same).
- **New bests**: when `goal_notices` reports a new best, optionally record a
  `best` learning. (Kept light — suggestions are the primary source.)
- A shared helper `record_suggestion_learning(store, sug)` used by both paths.

### 3. Injection — feed memory back in
- `ProjectRuntime._experiment_context()`: append a **"Prior learnings"** block —
  the top ~5 learnings for the active/focused experiment (falling back to the
  goal metric across experiments) — so the agent builds on them.
- `reviewer.build_review_context()`: include the same learnings so suggestions
  respect what was already tried (de-dups the improve loop's "no-gain" repeats).
- Campaign planner (`campaign._plan_campaign`): include prior learnings for the
  campaign's goal metric in the planning prompt.

### 4. REST + UI
- `GET /api/projects/{name}/learnings?experiment_id=` + `DELETE
  /learnings/{id}`.
- Experiments panel: a **Learnings** collapsible per experiment card listing
  `summary` + `delta` chips (✓ improved / ✗ no gain).
- Branch detail: show the experiment's learnings.

## Files touched
- `backend/store.py`, `backend/campaign.py`, `backend/experiment_loop.py`,
  `backend/main.py`, `backend/project_runtime.py`, `backend/agents/reviewer.py`,
  `backend/routers/runs.py`, `frontend/app.js`, `frontend/index.html`,
  `frontend/styles.css`, `docs/round7-learnings.md`, `tests/test_round7.py`.

## Out of scope
- Cross-project knowledge sharing (learnings are per-project).
- Free-text "insights" extraction from arbitrary runs (kept structured: only
  measured suggestion outcomes / new bests become learnings).
