"""Adversarial robustness evaluation helpers built on obfuscation-study data.

Provides the dataset builders (SWIFT transactions from the obfuscation-study
generator, and the clinical cohort) plus a small FGSM-style attack and the
robustness metrics from the `robustness` MCP server functions. Used by the
sample adversarial-testing notebooks and the run_adversarial_eval script.
"""

from __future__ import annotations

import json

import numpy as np

from examples.obfuscation.swift_data import generate_swift
from examples.privacy.clinical_cohort import build_cohort

# Set by the evaluation helpers so the workbench can record run metrics for
# the Experiments tab after a notebook executes.
LAST_RESULT: dict = {}


def _std(X: np.ndarray) -> np.ndarray:
    from sklearn.preprocessing import StandardScaler
    return StandardScaler().fit_transform(X.astype(float))


def swift_binary_dataset(n_rows: int = 2000, seed: int = 42):
    """Binary task: does a SWIFT payment get marked URGENCY?

    Returns (X_scaled, y, feature_names, target_label).
    """
    df = generate_swift(n_rows, seed)
    feats = ["transaction_amount_usd", "fx_rate_to_usd", "amount_in_usd",
             "transaction_fee"]
    X = _std(df[feats].astype(float).values)
    y = (df["priority"] == "URGENCY").astype(int).values
    return X, y, feats, "priority == URGENCY"


def clinical_binary_dataset(n_rows: int = 300, seed: int = 7):
    """Binary task: does the clinical patient have insurance?

    Returns (X_scaled, y, feature_names, target_label).
    """
    df = build_cohort(n_rows, seed)
    df["sex01"] = (df["sex"] == "M").astype(int)
    df["zip_num"] = df["zip_prefix"].astype(int)
    feats = ["age", "visit_amount_usd", "sex01", "zip_num"]
    X = _std(df[feats].astype(float).values)
    y = df["insurance"].astype(int).values
    return X, y, feats, "insurance"


def train_test(X, y, seed: int = 42, test_size: float = 0.3):
    from sklearn.model_selection import train_test_split
    return train_test_split(X, y, test_size=test_size, random_state=seed)


def train(model_type: str, Xtr, ytr, seed: int = 42):
    """Train a classifier. model_type: 'lr' | 'rf' | 'mlp'."""
    if model_type == "rf":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(n_estimators=80, random_state=seed).fit(Xtr, ytr)
    if model_type == "mlp":
        from sklearn.neural_network import MLPClassifier
        return MLPClassifier(hidden_layer_sizes=(16,), max_iter=1500,
                             random_state=seed).fit(Xtr, ytr)
    from sklearn.linear_model import LogisticRegression
    return LogisticRegression(max_iter=3000).fit(Xtr, ytr)


def fgsm_grad(model, X, y) -> np.ndarray:
    """Per-sample FGSM gradient direction (linear-model proxy).

    For linear models uses the true gradient direction sign((pred - y) * w);
    for non-linear models falls back to a fixed random direction (still a
    valid demonstration of an untargeted perturbation).
    """
    if hasattr(model, "decision_function"):
        s = model.decision_function(X)
    else:
        s = model.predict_proba(X)[:, 1]
    if hasattr(model, "coef_"):
        w = np.asarray(model.coef_).reshape(-1)
        err = np.clip(s - y, -1, 1)[:, None]
        return err * w[None, :]
    rng = np.random.default_rng(0)
    return np.sign(rng.normal(size=X.shape)).astype(float)


def perturb_batch(model, X, y, eps: float) -> np.ndarray:
    """X + eps * sign(FGSM gradient), clipped to the training range."""
    g = fgsm_grad(model, X, y)
    X_adv = np.clip(X + eps * np.sign(g), float(X.min()), float(X.max()))
    return X_adv


def evaluate_robustness(model, X, y, eps: float) -> dict:
    """Clean vs adversarial metrics for a given epsilon (uses robustness MCP fn)."""
    from mcp_servers import robustness_tools as rt

    clean = model.predict(X).tolist()
    X_adv = perturb_batch(model, X, y, eps)
    adv = model.predict(X_adv).tolist()
    res = json.loads(rt.robustness_metrics_from_predictions(clean, adv, y.tolist()))
    LAST_RESULT.update({
        "eps": float(eps),
        "clean_accuracy": res["clean_accuracy"],
        "robust_accuracy": res["robust_accuracy"],
        "asr": res["attack_success_rate_on_correct"],
    })
    return res


def robustness_sweep(model, X, y, epsilons) -> list[dict]:
    rows = []
    for eps in epsilons:
        r = evaluate_robustness(model, X, y, eps)
        rows.append({
            "eps": eps,
            "clean_accuracy": r["clean_accuracy"],
            "robust_accuracy": r["robust_accuracy"],
            "asr": r["attack_success_rate_on_correct"],
        })
    LAST_RESULT["sweep"] = rows
    LAST_RESULT["eps"] = epsilons[-1]
    return rows
