# Autonomous research program — credit-card fraud detection

Goal metric: **roc_auc** (higher is better). The dataset is extremely imbalanced
(0.17% fraud), so accuracy is meaningless — always report ROC-AUC.

You are the experimentation agent in an autonomous research loop. Your single
editable target is `research/experiment.py`. The harness runs it under a fixed
wall-clock budget and reads the final line `METRIC roc_auc=<value>`.

Dataset: `data/creditcard.csv` (Kaggle creditcardfraud — 284,807 rows,
Time, V1..V28, Amount, Class).

Rules:
1. Propose ONE focused change per iteration (preprocessing, scaling, class
   imbalance handling, feature selection, model, hyperparameters). Output ONLY
   the complete new `experiment.py` in a single ```python``` code block.
2. Keep it fast enough to finish inside the budget (train on a 80/20 stratified
   split; a few seconds per model is fine).
3. The final line printed must be `METRIC roc_auc=<value>`.
4. If ROC-AUC does not improve, the change is reverted automatically.

Good directions: class_weight="balanced", StandardScaler on Amount/Time,
undersampling/oversampling, dropping redundant features, trying
RandomForest / GradientBoosting / logistic with tuned C.
