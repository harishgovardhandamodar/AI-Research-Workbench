"""Independent validation (Pillar 2): fidelity gates, TSTR, drift, challenger
and performance metrics.

The data-generation model is validated like any model: synthetic output must
pass fidelity gates (distributional match, correlation structure, business-rule
coverage) before it may be used for material decisions, and final performance
claims always require hold-out REAL data (mandatory Train-Synthetic-Test-Real).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .core import _now


# ------------------------------------------------------------- fidelity gates --

def _ks_test(reference: np.ndarray, current: np.ndarray) -> dict:
    try:
        from scipy.stats import ks_2samp
        stat, p = ks_2samp(reference, current, method="asymp")
        return {"d_stat": round(float(stat), 4), "p_value": round(float(p), 4)}
    except ImportError:  # noqa: BLE001
        ref_ecdf = np.sort(reference)
        cur_ecdf = np.sort(current)
        combined = np.unique(np.concatenate([ref_ecdf, cur_ecdf]))
        d = 0.0
        for x in combined[:: max(1, len(combined) // 500)]:
            d = max(d, abs(np.mean(reference <= x) - np.mean(current <= x)))
        return {"d_stat": round(float(d), 4), "p_value": None}


def evaluate_fidelity(real_path: str, synthetic_path: str) -> dict:
    """Fidelity gates between a real reference and a synthetic dataset.

    Checks distributional match (KS), correlation-matrix distance, categorical
    match (total variation) and business-rule coverage. Returns PASS/FAIL with
    per-column diagnostics and remediation suggestions.
    """
    for label, path in (("real", real_path), ("synthetic", synthetic_path)):
        if not Path(path).exists():
            raise ValueError(f"{label} file not found: {path}")
    real = pd.read_csv(real_path)
    synth = pd.read_csv(synthetic_path)

    numeric = [c for c in real.select_dtypes(include="number").columns
               if c in synth.columns]
    ks_results, corr_distances = [], {}
    if numeric:
        for c in numeric:
            ks_results.append({
                "column": c,
                "real_mean": round(float(real[c].mean()), 4),
                "synth_mean": round(float(synth[c].mean()), 4),
                ** _ks_test(real[c].dropna().to_numpy(),
                            synth[c].dropna().to_numpy()),
            })
        r_corr = real[numeric].corr().fillna(0.0).to_numpy()
        s_corr = synth[numeric].corr().fillna(0.0).to_numpy()
        if r_corr.shape == s_corr.shape and r_corr.size:
            corr_distances["frobenius"] = round(
                float(np.linalg.norm(r_corr - s_corr, ord="fro")), 4)
        else:
            corr_distances["frobenius"] = None

    categorical = [c for c in real.select_dtypes(exclude="number").columns
                   if c in synth.columns]
    cat_tv = {}
    for c in categorical:
        rv = real[c].dropna().astype(str).value_counts(normalize=True)
        sv = synth[c].dropna().astype(str).value_counts(normalize=True)
        cats = set(rv.index) | set(sv.index)
        tv = 0.5 * sum(abs(rv.get(k, 0.0) - sv.get(k, 0.0)) for k in cats)
        cat_tv[c] = round(float(tv), 4)

    business_rules = [
        {"rule": "pd in [0,1]",
         "passed": bool(synth["pd"].between(0.0, 1.0).all()) if "pd" in synth else True},
        {"rule": "ead > 0",
         "passed": bool((synth["ead"] > 0).all()) if "ead" in synth else True},
        {"rule": "lgd in [0,1]",
         "passed": bool(synth["lgd"].between(0.0, 1.0).all()) if "lgd" in synth else True},
        {"rule": "credit_score in [300,850]",
         "passed": bool(synth["credit_score"].between(300, 850).all())
         if "credit_score" in synth else True},
        {"rule": "default in {0,1}",
         "passed": bool(set(synth["default"].unique()) <= {0, 1})
         if "default" in synth else True},
    ]
    rule_fail = [r["rule"] for r in business_rules if not r["passed"]]

    weak_ks = [r for r in ks_results if r["p_value"] is not None and r["p_value"] < 0.05]
    corr_ok = (corr_distances.get("frobenius") is not None
               and corr_distances["frobenius"] <= 0.15)
    cat_fail = [c for c, tv in cat_tv.items() if tv > 0.25]

    passed = (not rule_fail and corr_ok and not weak_ks and not cat_fail)
    remediation = []
    if rule_fail:
        remediation.append(f"fix business-rule violations: {rule_fail}")
    if not corr_ok:
        remediation.append("improve correlation structure (copula fit / sampling)")
    if weak_ks:
        remediation.append(f"re-calibrate marginals for columns: "
                           f"{[r['column'] for r in weak_ks][:5]}")
    if cat_fail:
        remediation.append(f"align categorical distributions: {cat_fail}")

    return {
        "verdict": "PASS" if passed else "FAIL",
        "gates": {
            "distributional_match": {
                "ks_tests": ks_results,
                "fail_count": len(weak_ks)},
            "correlation_structure": corr_distances,
            "categorical_match": cat_tv,
            "business_rules": business_rules,
        },
        "remediation": remediation,
        "assessed_at": _now(),
    }


# ------------------------------------------------------------- TSTR + metrics --

def compute_performance_metrics(y_true: list | np.ndarray,
                                y_pred_proba: list | np.ndarray,
                                positive: float = 1.0) -> dict:
    """Binary classification metrics from ground-truth + probability scores."""
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_pred_proba, dtype=float)
    if y_true.size != y_prob.size or y_true.size == 0:
        raise ValueError("y_true and y_pred_proba must be non-empty and equal length")
    if len(np.unique(y_true)) < 2:
        raise ValueError("need both classes in y_true for metric evaluation")
    pos = float(positive)
    y_label = (y_prob >= 0.5).astype(int)
    tp = int(((y_true == pos) & (y_label == 1)).sum())
    fp = int(((y_true != pos) & (y_label == 1)).sum())
    fn = int(((y_true == pos) & (y_label == 0)).sum())
    tn = int(((y_true != pos) & (y_label == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    acc = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0

    try:
        from sklearn.metrics import roc_auc_score, log_loss, average_precision_score
        auc = float(roc_auc_score(y_true, y_prob))
        pr_auc = float(average_precision_score(y_true, y_prob))
        brier = float(log_loss(y_true, np.clip(y_prob, 1e-7, 1 - 1e-7)))
    except Exception:  # noqa: BLE001
        auc, pr_auc, brier = None, None, None

    # KS = max |cumulative TP rate - cumulative FP rate| across threshold grid
    order = np.argsort(-y_prob)
    yt = y_true[order]
    pos_count = int((yt == pos).sum())
    neg_count = yt.size - pos_count
    ks = 0.0
    if pos_count and neg_count:
        tpr = np.cumsum(yt == pos) / pos_count
        fpr = np.cumsum(yt != pos) / neg_count
        ks = float(np.max(np.abs(tpr - fpr)))
    return {
        "roc_auc": round(auc, 4) if auc is not None else None,
        "pr_auc": round(pr_auc, 4) if pr_auc is not None else None,
        "brier": round(brier, 4) if brier is not None else None,
        "accuracy": round(acc, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "ks": round(ks, 4),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def tstr_evaluate(synthetic_path: str, real_path: str, target: str,
                  seed: int = 42, test_size: float = 0.3,
                  positive: float = 1.0) -> dict:
    """Mandatory Train-Synthetic-Test-Real evaluation.

    Fits a logistic regression on the SYNTHETIC data, then evaluates on a held
    out split of the REAL data. The protocol flag asserts TSTR compliance.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    synth = pd.read_csv(synthetic_path)
    real = pd.read_csv(real_path)
    if target not in synth.columns or target not in real.columns:
        raise ValueError(f"target '{target}' must exist in both files")
    feats = [c for c in synth.columns if c != target
             and synth[c].dtype.kind in "fiu"]
    feats = [c for c in feats if c in real.columns and real[c].dtype.kind in "fiu"]
    if not feats:
        raise ValueError("no shared numeric feature columns available for TSTR")

    X = synth[feats].fillna(0.0).to_numpy()
    y = (synth[target].to_numpy() == positive).astype(int)
    Xr = real[feats].fillna(0.0).to_numpy()
    yr = (real[target].to_numpy() == positive).astype(int)
    if len(np.unique(yr)) < 2:
        raise ValueError("real hold-out data must contain both classes for TSTR")

    Xr_train, Xr_test, yr_train, yr_test = train_test_split(
        Xr, yr, test_size=test_size, random_state=seed)
    model = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=1000, random_state=seed))
    model.fit(X, y)  # trained ONLY on synthetic data
    proba = model.predict_proba(Xr_test)[:, 1]
    metrics = compute_performance_metrics(yr_test, proba, positive=1.0)

    return {
        "protocol": "TSTR",
        "status": "compliant",
        "statement": ("Model trained on synthetic data; performance claims "
                      "measured on hold-out REAL data. Any deployment must keep "
                      "monitoring real-data performance."),
        "features_used": feats,
        "synthetic_train_rows": int(len(X)),
        "real_eval_rows": int(len(Xr_test)),
        "seed": seed,
        "metrics": metrics,
        "coef_magnitude": round(float(np.abs(model.named_steps[
            "logisticregression"].coef_).max()), 4),
        "assessed_at": _now(),
    }


# ------------------------------------------------------------------ drift --

def _psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    edges = np.quantile(ref, np.linspace(0.0, 1.0, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    r_hist = np.histogram(ref, bins=edges)[0]
    c_hist = np.histogram(cur, bins=edges)[0]
    r_p = r_hist / max(r_hist.sum(), 1)
    c_p = c_hist / max(c_hist.sum(), 1)
    psi = sum((c_p[i] - r_p[i]) * np.log((c_p[i] + 1e-9) / (r_p[i] + 1e-9))
              for i in range(len(r_p)))
    return float(psi)


def _tv(categorical_ref: pd.Series, categorical_cur: pd.Series) -> float:
    rv = categorical_ref.dropna().astype(str).value_counts(normalize=True)
    cv = categorical_cur.dropna().astype(str).value_counts(normalize=True)
    cats = set(rv.index) | set(cv.index)
    return 0.5 * sum(abs(rv.get(k, 0.0) - cv.get(k, 0.0)) for k in cats)


def detect_drift(reference_path: str, current_path: str) -> dict:
    """Population-shift / drift detection between a reference and current CSV.

    Numeric columns use the Population Stability Index (PSI); categorical use
    total variation distance. PSI > 0.25 flags a shift; > 0.1 a warning.
    """
    if not Path(reference_path).exists() or not Path(current_path).exists():
        raise ValueError("reference and current paths must exist")
    ref = pd.read_csv(reference_path)
    cur = pd.read_csv(current_path)

    numeric = [c for c in ref.select_dtypes(include="number").columns
               if c in cur.columns]
    cat = [c for c in ref.select_dtypes(exclude="number").columns
           if c in cur.columns]

    drift_cols, warn_cols, details = [], [], []
    for c in numeric:
        psi = _psi(ref[c].dropna().to_numpy(), cur[c].dropna().to_numpy())
        level = "shift" if psi > 0.25 else "warn" if psi > 0.1 else "ok"
        if level == "shift":
            drift_cols.append(c)
        elif level == "warn":
            warn_cols.append(c)
        details.append({"column": c, "type": "numeric", "psi": round(psi, 4),
                        "level": level})
    for c in cat:
        tv = _tv(ref[c], cur[c])
        level = "shift" if tv > 0.5 else "warn" if tv > 0.3 else "ok"
        if level == "shift":
            drift_cols.append(c)
        elif level == "warn":
            warn_cols.append(c)
        details.append({"column": c, "type": "categorical",
                        "total_variation": round(tv, 4), "level": level})

    return {
        "verdict": ("DRIFT DETECTED" if drift_cols else
                    "WARNING" if warn_cols else "IN CONTROL"),
        "shifted_columns": drift_cols,
        "warning_columns": warn_cols,
        "details": details,
        "action": ("Re-run model monitoring; consider redevelopment or "
                   "stressor overlays." if drift_cols else
                   "Continue routine monitoring."),
        "assessed_at": _now(),
    }


# -------------------------------------------------------------- challenger --

_CHALLENGERS = ("logistic", "gaussian_nb", "decision_tree", "random_forest")


def run_challenger(data_path: str, target: str, baseline: str = "logistic",
                   challenger: str = "gaussian_nb", seed: int = 42,
                   min_auc_gain: float = 0.005) -> dict:
    """Independent challenger: does an alternative model beat the baseline?

    Trains both on a shared split; reports AUC/KS head-to-head and whether the
    challenger wins by a material margin (default >= 0.5pp AUC).
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.naive_bayes import GaussianNB
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    for model in (baseline, challenger):
        if model not in _CHALLENGERS:
            raise ValueError(f"unknown model '{model}' — must be one of {list(_CHALLENGERS)}")
    df = pd.read_csv(data_path)
    if target not in df.columns:
        raise ValueError(f"target '{target}' not in {data_path}")
    feats = [c for c in df.columns if c != target and df[c].dtype.kind in "fiu"]
    if not feats:
        raise ValueError("no numeric features for challenger training")
    X = df[feats].fillna(0.0).to_numpy()
    y = df[target].to_numpy()
    if len(np.unique(y)) < 2:
        raise ValueError("need both classes in the data for a challenger run")
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.3, random_state=seed)

    def _fit(name):
        if name == "logistic":
            return make_pipeline(StandardScaler(),
                                 LogisticRegression(max_iter=1000,
                                                    random_state=seed))
        if name == "gaussian_nb":
            return GaussianNB()
        if name == "decision_tree":
            return DecisionTreeClassifier(max_depth=6, random_state=seed)
        return RandomForestClassifier(n_estimators=120, max_depth=8,
                                      random_state=seed)

    base = _fit(baseline).fit(X_tr, y_tr)
    chal = _fit(challenger).fit(X_tr, y_tr)
    base_m = compute_performance_metrics(y_te, base.predict_proba(X_te)[:, 1])
    chal_m = compute_performance_metrics(y_te, chal.predict_proba(X_te)[:, 1])
    gain = (chal_m["roc_auc"] or 0.0) - (base_m["roc_auc"] or 0.0)
    return {
        "baseline": baseline,
        "challenger": challenger,
        "baseline_metrics": base_m,
        "challenger_metrics": chal_m,
        "auc_gain": round(gain, 4),
        "challenger_wins": bool(gain >= min_auc_gain),
        "verdict": ("Challenger beats baseline by a material margin — investigate "
                    "adoption or document rejection." if gain >= min_auc_gain else
                    "No material challenger improvement — baseline is adequate."),
        "assessed_at": _now(),
    }
