# Model benchmarks

Benchmarks compare the workbench's own LLMs on a task, using **per-experiment
model pinning** so each run provably used the tested model.

## Data model

`evals` records: name, task prompt, model list, goal metric, direction, status
(`planned|running|done|failed`), and a leaderboard report. `GET /evals` lists
them; `POST /evals` + `POST /evals/{id}/run` create and start; `/stop` halts.

## Execution

For each model:

1. An experiment `[Eval] <name> · <model>` is created **pinned to that model**.
2. One agent turn runs the task prompt; the agent reports the goal metric via
   `report_metric`.
3. The run is recorded with its model, environment, code, and integrity hash.

Benchmarks run in the **background** (event bus + workflow stages), are
stop-able, and produce a **leaderboard** report (`model · best metric · Δ vs
best`, best model highlighted) posted to chat and saved as an artifact.

## Reading the result

- The ranked table shows which model achieved the goal best on this task.
- Because each run records model + env + code + integrity, the comparison is
  reproducible and verifiable.
- Rerun any benchmark later; a model change between runs is visible per-run.
