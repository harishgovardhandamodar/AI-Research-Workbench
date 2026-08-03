"""Adversarial Robustness Evaluation MCP server for the Fox workbench.

Systematically evaluates how brittle trained scientific/ML models are under
adversarial perturbations — evasion attacks (FGSM / PGD), robust accuracy,
attack success rate, and simple robustness reports. Everything runs locally.

This server is designed to be ADDED via the Agent dashboard (or Settings -> MCP)
rather than being enabled by default — see
docs/Add_MCP_adversarial_robustness_evaluation.md for the GUI walk-through.

Tools (namespaced `robustness__<tool>` when the server is named "robustness"):
  - evaluate_sklearn_robustness      (ART: FGSM / PGD on sklearn classifiers)
  - robustness_metrics_from_predictions  (framework-agnostic, needs no library)
  - adversarial_robustness_checklist (threat-model / evaluation plan)
  - simple_fgsm_perturbation         (lightweight L-infinity perturbation)

Dependencies: `adversarial-robustness-toolbox` enables the full ART evaluation;
without it the framework-agnostic and checklist tools still work.

Run standalone (stdio):
    .venv/bin/python mcp_servers/robustness_tools.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer("fox-robustness-tools", version="0.1.0")
    RO = ToolAnnotations(read_only_hint=True)
except ImportError:  # allow importing the plain functions without the mcp package
    mcp = None
    RO = None

import numpy as np  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data) -> str:
    return json.dumps(data, indent=2)


def _predict_one(model, X) -> np.ndarray:
    """Return the predicted class per row for a fitted sklearn model."""
    preds = model.predict(X)
    return np.asarray(preds).reshape(-1)


def evaluate_sklearn_robustness(model_path: str, X_test_path: str, y_test_path: str,
                                attack: str = "FastGradientMethod", eps: float = 0.1,
                                norm: str = "inf") -> str:
    """Evaluate adversarial robustness of an sklearn classifier with ART.

    Args:
        model_path: Path to a joblib/pickle sklearn model.
        X_test_path: .npy file with test features.
        y_test_path: .npy file with test labels.
        attack: "FastGradientMethod" (default) or "ProjectedGradientDescent".
        eps: Perturbation budget.
        norm: "inf" or "2".
    """
    try:
        import joblib
        from art.attacks.evasion import FastGradientMethod, ProjectedGradientDescent
        from art.estimators.classification import SklearnClassifier
    except ImportError as e:
        return _json({"error": f"Missing dependency ({e}). Install with: "
                                 "pip install adversarial-robustness-toolbox"})

    try:
        model = joblib.load(model_path)
        X_test = np.load(X_test_path)
        y_test = np.asarray(np.load(y_test_path)).reshape(-1)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Could not load model or data: {e}"})
    if X_test.ndim != 2 or X_test.shape[0] != y_test.shape[0]:
        return _json({"error": "X_test must be 2-D (n_samples, n_features) "
                               "and y_test must match n_samples"})

    classifier = SklearnClassifier(model=model,
                                   clip_values=(float(X_test.min()), float(X_test.max())))
    clean_pred = classifier.predict(X_test).argmax(axis=1)
    clean_acc = float(np.mean(clean_pred == y_test))

    norm_val = np.inf if norm == "inf" else 2
    if attack == "ProjectedGradientDescent":
        attack_obj = ProjectedGradientDescent(estimator=classifier, norm=norm_val,
                                              eps=eps, eps_step=eps / 10, max_iter=20)
    else:
        attack_obj = FastGradientMethod(estimator=classifier, norm=norm_val, eps=eps)

    X_adv = attack_obj.generate(x=X_test)
    adv_pred = classifier.predict(X_adv).argmax(axis=1)
    robust_acc = float(np.mean(adv_pred == y_test))
    ok = clean_pred == y_test
    asr = float(np.mean(adv_pred[ok] != y_test[ok])) if ok.sum() else None

    report = {
        "model": model_path,
        "attack": attack,
        "eps": eps,
        "norm": f"L{norm}",
        "n_samples": int(X_test.shape[0]),
        "clean_accuracy": round(clean_acc, 4),
        "robust_accuracy": round(robust_acc, 4),
        "attack_success_rate_on_correct": round(asr, 4) if asr is not None else None,
        "accuracy_drop": round(clean_acc - robust_acc, 4),
        "interpretation": ("Model is highly vulnerable." if robust_acc < 0.5 else
                           "Moderate robustness." if robust_acc < 0.8 else
                           "Relatively robust under this threat model."),
        "assessed_at": _now(),
        "library": "adversarial-robustness-toolbox",
    }
    return _json(report)


def robustness_metrics_from_predictions(clean_predictions: list[int],
                                        adversarial_predictions: list[int],
                                        true_labels: list[int]) -> str:
    """Robustness metrics from pre-computed clean/adv predictions (no libraries)."""
    clean = np.asarray(clean_predictions).reshape(-1)
    adv = np.asarray(adversarial_predictions).reshape(-1)
    y = np.asarray(true_labels).reshape(-1)
    if not (clean.size == adv.size == y.size) or clean.size == 0:
        return _json({"error": "all three arrays must be non-empty and equal length"})

    clean_acc = float(np.mean(clean == y))
    robust_acc = float(np.mean(adv == y))
    ok = clean == y
    asr = float(np.mean(adv[ok] != y[ok])) if ok.sum() else None

    return _json({
        "n_samples": int(y.size),
        "clean_accuracy": round(clean_acc, 4),
        "robust_accuracy": round(robust_acc, 4),
        "attack_success_rate_on_correct": round(asr, 4) if asr is not None else None,
        "absolute_drop": round(clean_acc - robust_acc, 4),
        "relative_drop": round((clean_acc - robust_acc) / clean_acc, 4)
                        if clean_acc > 0 else None,
        "assessed_at": _now(),
    })


def adversarial_robustness_checklist(model_type: str, data_modality: str = "tabular",
                                     high_stakes: bool = False) -> str:
    """Generate a tailored adversarial robustness evaluation checklist."""
    base = [
        "Define the threat model (who is the adversary, what can they modify, "
        "what knowledge do they have)",
        "Measure clean performance on a held-out test set",
        "Run at least one strong white-box attack (PGD / AutoAttack) if gradients "
        "are available",
        "Report both robust accuracy and attack success rate",
        "Test multiple perturbation budgets (eps values)",
        "Check whether robustness holds across data subgroups",
    ]
    if data_modality == "tabular":
        base.append("Prefer feature-constrained attacks that respect domain "
                    "validity (e.g. valid lab ranges)")
    if data_modality == "image":
        base.append("Evaluate under L-inf and L2 norms; consider common "
                    "corruptions as well")
    if model_type.lower() in {"llm", "language", "transformer"}:
        base.extend([
            "Test prompt injection and jailbreak-style adversarial inputs",
            "Evaluate character / token-level perturbations and semantic "
            "paraphrases",
        ])
    if high_stakes:
        base.extend([
            "Require human review of failure cases",
            "Document residual risk and decision thresholds",
            "Consider certified robustness methods if available",
        ])
    return _json({
        "model_type": model_type,
        "data_modality": data_modality,
        "high_stakes": high_stakes,
        "checklist": base,
        "recommended_metrics": [
            "Clean accuracy / F1",
            "Robust accuracy under chosen attack",
            "Attack Success Rate (ASR)",
            "Accuracy drop (absolute & relative)",
        ],
        "generated_at": _now(),
    })


def simple_fgsm_perturbation(values: list[float], gradient: list[float],
                             eps: float = 0.1) -> str:
    """Basic Fast Gradient Sign Method-style perturbation (L-inf budget)."""
    x = np.asarray(values, dtype=float)
    g = np.asarray(gradient, dtype=float)
    if x.size != g.size or x.size == 0:
        return _json({"error": "values and gradient must be non-empty and equal length"})
    x_adv = x + eps * np.sign(g)
    return _json({
        "eps": eps,
        "original": values,
        "adversarial": x_adv.tolist(),
        "perturbation": (x_adv - x).tolist(),
        "l_inf_norm": float(np.max(np.abs(x_adv - x))),
        "assessed_at": _now(),
    })


if mcp is not None:
    _TOOLS = [
        evaluate_sklearn_robustness, robustness_metrics_from_predictions,
        adversarial_robustness_checklist, simple_fgsm_perturbation,
    ]
    for _fn in _TOOLS:
        mcp.tool(annotations=RO)(_fn)
    __all__ = [f.__name__ for f in _TOOLS] + ["mcp"]
else:
    __all__ = [
        "evaluate_sklearn_robustness", "robustness_metrics_from_predictions",
        "adversarial_robustness_checklist", "simple_fgsm_perturbation",
    ]

if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("mcp package not installed; run: pip install mcp")
    mcp.run(transport="stdio")
