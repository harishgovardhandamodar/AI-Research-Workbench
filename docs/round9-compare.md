# Round 9 — Compare & evaluate: experiment/campaign leaderboards and model benchmarks

Rounds 2–8 made the workbench able to run, steer, reproduce, orchestrate,
remember, and verify research. Round 9 answers the final question: **"what is
best?"** — comparing experiments, campaigns, and the workbench's own LLMs
systematically. Comparison primitives exist pairwise (`compare_runs`, the 2-run
panel); this round generalizes them and adds a first-class model benchmark.

## Design

### 1. Cross-experiment & cross-campaign leaderboards (`backend/experiments.py`)
- `compare_experiments(store, exps)` → per experiment: best goal value + run id,
  run count, status, `delta_best`/`pct_best` vs the overall best, and
  `to_target`/`pct_target`. Deterministic (reuses `best_metric`).
- `compare_campaigns(store, campaigns)` → per campaign: its goal metric and the
  best value across its steps' experiments, ranked.
- Endpoints: `GET /api/projects/{name}/experiments/compare` and
  `GET /api/projects/{name}/campaigns/compare`.

### 2. N-run comparison
- `compare_runs_many(runs)` in `experiments.py` → a side-by-side table:
  metric × run label (values + best highlighted). Generalizes `compare_runs`.
- `GET /api/projects/{name}/compare?runs=1,2,3` (comma-separated) keeps the
  existing 2-run response shape when two ids are given (`comparison`), else a
  `many` table.

### 3. Model benchmark (round-6 background runner)
- `evals` table (store): id, name, prompt, models TEXT (JSON list),
  goal_metric, higher_better, status (`planned|running|done|failed`), report,
  created_at, updated_at.
- `backend/eval.py` `run_eval(rt, coordinator, build_llm_messages, eval_id,
  emit, workflow)` — for each model: create/pin an experiment named
  `[Eval] {name} · {model}` (round-3 model pinning makes the coordinator use
  that model), run one coordinator turn with the eval prompt, record the run,
  collect the goal metric. Produce a ranked leaderboard report ("model | best
  metric | delta vs best"), stored on the eval + posted to chat + saved as a
  text artifact. Abort-aware, resumable via `set_invoke(kind="eval", ...)`.
- `ProjectRuntime.start_eval(eid)/stop_eval()/eval_running()` mirroring
  `start_campaign`; `recover` marks stale running evals.
- Launch: `intent == "eval"` (chat) + `POST /evals` + `POST /evals/{id}/run`
  + `/stop` + `GET /evals`.

### 4. Frontend
- Experiments tab: a **Compare experiments** leaderboard panel (name · best ·
  Δ best · % target), loaded from `/experiments/compare`; the existing run-
  compare panel gains multi-run support (`/compare?runs=1,2,3`).
- Campaigns panel: show the campaign leaderboard (`/campaigns/compare`).
- An **Eval** panel: name + prompt + model multi-select + goal metric; Run
  (background) / Stop; live status + leaderboard report.

## Files touched
- `backend/experiments.py`, `backend/store.py`, `backend/eval.py` (new),
  `backend/project_runtime.py`, `backend/main.py`, `backend/routers/runs.py`,
  `frontend/app.js`, `frontend/index.html`, `frontend/styles.css`,
  `docs/round9-compare.md`, `tests/test_round9.py`.

## Out of scope
- Distributed/parallel model evals (sequential per model — each is a full turn).
- Automated eval-grading rubrics beyond the task's own goal metric.
