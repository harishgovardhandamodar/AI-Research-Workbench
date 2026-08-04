# Autonomous research program — Titanic survival

Goal metric: **accuracy** (higher is better).

You are the experimentation agent in an autonomous research loop. Your single
editable target is `research/experiment.py`. The harness runs it under a fixed
wall-clock budget and reads the final line `METRIC accuracy=<value>`.

Dataset: `data/titanic_train.csv` (the classic Kaggle Titanic dataset — predict
survival from passenger features).

Rules:
1. Propose ONE focused change per iteration (feature engineering, imputation,
   scaling, model choice, hyperparameters). Output ONLY the complete new
   `experiment.py` in a single ```python``` code block.
2. Keep it fast enough to finish inside the budget (cross-validation on ~891
   rows is fine).
3. The final line printed must be `METRIC accuracy=<value>`.
4. If accuracy does not improve, the change is reverted automatically — so make
   changes that should genuinely help.

Good directions: age/fare imputation, family size, title extraction from Name,
scaling, adding Age*Class interactions, trying RandomForest / GradientBoosting /
logistic with tuned C.
