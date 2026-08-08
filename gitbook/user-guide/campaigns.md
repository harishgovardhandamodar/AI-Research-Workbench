# Campaigns

A **campaign** is a long-horizon autonomous research investigation: it *plans*
a multi-step study, *executes* each step as its own experiment through the live
agent, and *writes a synthesis report*.

## Launch

- **🧭 Campaign** quick-action, or `/campaign <research question>` in chat.
- The planner (optionally grounded in literature + prior learnings) produces
  3–5 steps (`[{title, kind, hypothesis, plan}]`).

## Execution

Each step:

1. Creates its own experiment (with the campaign's goal metric).
2. Runs one agent turn (variants via `start_run`/`finish_run` or `run_sweep`,
   metrics via `report_metric`).
3. Records the best run; chains lineage via `parent_run_id` so the branch graph
   shows the campaign.
4. Runs the reviewer + records suggestions/learnings.

Steps run **in the background** (survive disconnect), stream live progress to
every open window, and are resumable: the resume point is persisted after each
step, and a failed/interrupted campaign offers **Resume**.

## Report

The campaign finishes with a **synthesis report** (per-step best metrics,
leaderboard), stored on the campaign, posted to chat, and saved as a text
artifact. Reports also appear in the project report's **Campaigns** section.

## Control

- **⏹ Stop** halts gracefully at the next step boundary.
- **Resume** continues from the last completed step (also after a server
  restart — interrupted campaigns are marked resumable).

See [Background campaigns](../features/background-campaigns.md).
