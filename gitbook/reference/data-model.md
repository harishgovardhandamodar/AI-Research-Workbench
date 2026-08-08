# Data model

Each project has its own SQLite database (`workbench.db`), opened in WAL mode
with a single shared connection.

## Tables

| Table | Purpose | Key fields |
|---|---|---|
| `messages` | Chat transcript | role, content, meta (tags, tool_calls, experiment_id) |
| `runs` | Recorded agent turns | prompt, reply, status, tool_sequence, artifact_ids, metrics, review, experiment_id, config, label, kind, parent_run_id, model, git_commit, code, env, message_id, integrity_hash |
| `experiments` | Families of runs around one goal | name, hypothesis, goal_metric, goal_target, higher_better, status, plan, model, updated_at |
| `goals` | Target metrics (Goals panel) | metric, target, higher_better, label, experiment_id (NULL = project-wide) |
| `suggestions` | First-class reviewer suggestions | experiment_id, source_run_id, run_id, title, action, prompt, status, baseline_value, outcome_value, delta, improved |
| `learnings` | Measured outcomes (knowledge memory) | experiment_id, run_id, metric, baseline_value, outcome_value, delta, improved, summary, source |
| `campaigns` | Background research investigations | name, research_question, goal_metric, higher_better, status, report |
| `campaign_steps` | One campaign step | campaign_id, step_order, title, kind, hypothesis, plan, experiment_id, best_run_id, status |
| `evals` | Model benchmarks | name, prompt, models (JSON), goal_metric, higher_better, status, report |
| `settings` | Key/value project settings | focus_experiment_id, workflow_latest, context_cutoff, … |
| `workflow_runs` | Archived workflow snapshots | title, status, pct, stages (JSON) |
| `grants` | Permission decisions | kind, pattern, decision |
| `approval_log` | Approval history | kind, command, decision |

## The run record

The most important row. `_row_run` exposes:

- identity: id, kind, label, status, parent_run_id, model
- content: prompt, reply
- measurement: metrics, config, review
- provenance: tool_sequence, artifact_ids, code (full), env, git_commit
- verifiability: message_id (audit trace), integrity_hash

`kind` tags the source: `agent_run`, `sweep`, `campaign`, `notebook`, `eval`,
`restore`, `autoresearch`, `privacy_workflow`.

## Migrations

New columns are added with `ALTER TABLE ... ADD COLUMN` in guarded
`try/except` blocks (`_init_db`), so older databases upgrade in place on first
open.

## Threading

The store connection is created on the event-loop thread and used there; report
and export generation also run on the event loop (not in worker threads) to
avoid SQLite cross-thread errors.
