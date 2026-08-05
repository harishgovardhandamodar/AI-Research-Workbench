# Autonomous research demos

Demonstrate the workbench's **autonomous research capabilities** with real,
publicly-mirrored Kaggle datasets.

Two complementary loops, both built-in:

1. **Autoresearch loop** (`🤖 Autoresearch` quick action or `/autoresearch <metric>`)
   — karpathy/autoresearch-style: an experimentation agent edits the single
   target `research/experiment.py`, the harness runs it under a fixed wall-clock
   budget, keeps the change only when the goal metric improves (else reverts),
   and logs every attempt. See each demo's `program.md`.
2. **Improve loop** (`🔁 Improve experiment`) — the reviewer-driven loop: run a
   variant → reviewer suggests the next change → apply → rerun toward the goal.

Both loops record every run on the **Experiments timeline/graph** and auto-commit
kept runs to the experiment management repo when configured.

## Demos

| Demo | Dataset | Goal metric | Report |
|---|---|---|---|
| **Titanic** (`titanic/`) | classic Titanic survival | accuracy | — |
| **Credit-card fraud** (`creditcard/`) | Kaggle creditcardfraud (284,807 × 31, Git-LFS) | roc_auc | [run report](../../sample-reports/credit-card-fraud-autonomous-autoresearch-run-report.md) |

### Titanic

```bash
.venv/bin/python examples/autoresearch/setup_demo.py kaggle-demo
```
Autoresearch lifted cross-validation accuracy from ≈0.79 to ≈0.828 (kept
improvements, worst proposal reverted).

### Credit-card fraud

```bash
.venv/bin/python examples/autoresearch/creditcard/setup_demo.py fraud-demo
# then run the loop, e.g. via the MCP tool:
#   autoresearch__research_run(project="fraud-demo", goal_metric="roc_auc", max_iters=6)
```
ROC-AUC improved 0.9823 → **0.9867** with one bad proposal reverted; the dataset
is versioned with **Git-LFS** in `creditcard/data/`.

## Files

```
examples/autoresearch/
  setup_demo.py                      bootstrap: download dataset + seed research/
  titanic/                           Titanic demo (experiment.py, program.md)
  creditcard/                        Credit-card fraud demo
    setup_demo.py                    OpenML download (ARFF->CSV) + seed
    data/creditcard.csv              the Kaggle creditcardfraud dataset (LFS)
    experiment.py                    the agent's editable target (autoresearch)
    program.md                       research instructions the agent follows
```

