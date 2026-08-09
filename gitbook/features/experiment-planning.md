# Experiment planning

Every experiment can carry a first-class **plan**: an ordered list of concrete,
runnable steps (baseline → variations → best variant) that the agent works
through, one turn at a time, while the workbench tracks progress.

## Where the plan lives

The experiment **detail modal** opens with a 🗺 **Plan** panel above the research
advisor. It shows:

- the ordered steps with a kind icon (🧠 experiment · 🌊 sweep · 🎓 finetune ·
  📊 eval · 🧬 data), each step's hypothesis/plan text, and a status badge
  (`planned` / ▶ `running` / ✓ `done`)
- a progress bar (`n/m steps · pct%`)
- per-step **▶ Run** button — sends the step as a chat turn bound to the
  experiment, so the pipeline view captures the launch; when the turn finishes
  the step is marked **done** and linked to the run it produced
- **↻ Split** — deterministically re-parse the experiment's free-text plan into
  numbered/bulleted steps (no LLM needed)
- **✨ Propose** — ask the LLM for a goal metric/target + a concrete plan,
  grounded in prior learnings; accepted steps replace the plan and the
  experiment's objective is kept in sync

## How steps are made

- **Split** is deterministic: numbered lines, bullets and `Step N:` headings
  become one step each; a plain paragraph becomes a single step. Each step is
  classified (data / sweep / finetune / eval / model) from its text.
- **Propose** calls the LLM (best-effort): it returns a `goal_metric`,
  `goal_target`, `higher_better`, a short plan text and the steps. If the LLM is
  unavailable the proposal falls back to splitting the existing plan, then to a
  sensible baseline → variation → best-variant default.

## The agent works the plan

The experiment context the agent receives each turn includes the plan steps with
their status, so it knows what's done and what's next. Plan steps are first-class
rows (`experiment_steps`) — the advisor and Experiments tab share the same data.

## Endpoints

- `GET  /experiments/{eid}/plan` — steps + progress
- `POST /experiments/{eid}/plan` — set steps, split `plan_text`, or `propose=true`
- `PATCH /experiments/{eid}/plan/steps/{sid}` — mark running/done, link a run
- `POST /experiments/{eid}/plan/steps/{sid}/run` — the chat prompt for a step
