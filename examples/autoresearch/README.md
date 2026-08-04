# Autonomous research demo (Kaggle Titanic)

Demonstrates the workbench's **autonomous research capabilities** with a real
(publicly mirrored) Kaggle dataset — the classic **Titanic** survival dataset.

Two complementary loops, both built-in:

1. **Autoresearch loop** (`🤖 Autoresearch` quick action or `/autoresearch accuracy`)
   — karpathy/autoresearch-style: an experimentation agent edits the single
   target `research/experiment.py`, the harness runs it under a fixed wall-clock
   budget, keeps the change only when the goal metric improves (else reverts),
   and logs every attempt. See `examples/autoresearch/titanic/program.md`.

2. **Improve loop** (`🔁 Improve experiment` on the "titanic survival" experiment)
   — the reviewer-driven loop: run a variant → reviewer suggests the next change
   → apply → rerun toward the goal.

Both loops record every run on the **Experiments timeline/graph** (accuracy
evolution over iterations) and auto-commit kept runs to the experiment
management repo if configured.

## Files

```
examples/autoresearch/
  setup_demo.py                      bootstrap: download dataset + seed research/
  titanic/
    experiment.py                    the agent's editable target (autoresearch)
    program.md                       research instructions the agent follows
```

## Run it

```bash
# 1. One-time bootstrap (creates/refreshes the kaggle-demo project):
.venv/bin/python examples/autoresearch/setup_demo.py kaggle-demo

# 2. In the UI (project: kaggle-demo, experiment: titanic survival):
#    - Improve loop:  🔁 Improve experiment
#    - Autoresearch:  🤖 Autoresearch   (or type: /autoresearch accuracy)
```

## Expected result

Starting baseline is cross-validation accuracy ≈ **0.79** (logistic regression on
raw features). The autoresearch agent typically lifts it past **0.82** by adding
feature engineering (imputation, family size, title extraction) and/or a stronger
model; changes that do not improve the metric are reverted automatically, and the
full iteration log lands in `research/log.md`.
