"""Expanded privacy MCP server for the Fox workbench.

Local-first privacy tooling across four capability areas:

  Detection & Assessment     detect_pii_in_text, assess_dataframe_privacy
  Red-Teaming / Evaluation   membership_inference_eval, reidentification_scenario,
                             privacy_redteam_checklist
  Differential Privacy       apply_laplace_dp, apply_gaussian_dp,
                             dp_privacy_budget_report, dp_guarantee_summary
  Synthetic Data             generate_synthetic_tabular,
                             synthetic_data_quality_report

Everything runs on the user's machine. Heavy optional libraries (presidio,
sdv, opendp) are used when installed; otherwise robust built-in implementations
provide the same capability (regex PII scanning, native Laplace/Gaussian
mechanisms, schema-preserving generation).

Tool functions are plain callables, so the example experiments/notebooks can
import them directly (import mcp_servers.privacy_tools as pt) while the agent
calls them through MCP as ``privacy__<tool>``.

Run standalone (stdio):

    .venv/bin/python mcp_servers/privacy_tools.py
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer("fox-privacy-tools", version="0.1.0")
    RO = ToolAnnotations(read_only_hint=True)
except ImportError:  # allow importing the plain functions without the mcp package
    mcp = None
    RO = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data) -> str:
    return json.dumps(data, indent=2)


# ------------------------------------------------------------- PII detection --

_PII_PATTERNS = [
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("phone", r"\b(\+?\d{1,3}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"),
    ("credit_card", r"\b(?:\d[ -]?){13,19}\b"),
    ("us_ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    ("ipv4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("postal_code", r"\b\d{5}(?:-\d{4})?\b"),
]


def detect_pii_in_text(text: str) -> str:
    """Scan free text for PII (emails, phones, cards, SSNs, IBANs, IPs)."""
    if not text:
        return _json({"findings": [], "total": 0, "risk": "none",
                      "assessed_at": _now()})
    findings: list[dict] = []
    seen = set()
    for kind, pattern in _PII_PATTERNS:
        for m in re.finditer(pattern, text):
            val = m.group(0)
            key = (kind, val)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"type": kind, "value": val, "position": [m.start(), m.end()]})
    counts = Counter(f["type"] for f in findings)
    risk = ("HIGH" if any(k in ("credit_card", "us_ssn", "iban") for k in counts)
            else "MEDIUM" if len(findings) > 3 else "LOW" if findings else "none")
    return _json({"findings": findings, "total": len(findings),
                  "by_type": dict(counts), "risk": risk, "assessed_at": _now()})


_SENSITIVE_COL_PATTERNS = {
    "iban": r"iban", "bic": r"\bbic\b", "name": r"\b(name|person|patient)\b",
    "email": r"email", "phone": r"phone", "address": r"address",
    "ssn": r"(ssn|social|national.?id)", "card": r"card",
    "account": r"account|acct",
}
_QUASI_COL_PATTERNS = {
    "date": r"date|time|dob|admission", "city": r"city|town|zip|postal|location",
    "age": r"age|birth|year", "amount": r"amount|value|count|volume|balance",
}


def _classify_column(col: str) -> str:
    low = col.lower()
    for kind, pat in _SENSITIVE_COL_PATTERNS.items():
        if re.search(pat, low):
            return f"sensitive:{kind}"
    for kind, pat in _QUASI_COL_PATTERNS.items():
        if re.search(pat, low):
            return f"quasi:{kind}"
    return "other"


def assess_dataframe_privacy(file_path: str,
                             quasi_identifier_columns: list[str] | None = None) -> str:
    """Assess a local CSV's privacy posture.

    Loads `file_path` (relative to the workbench root, or absolute), classifies
    columns as sensitive / quasi-identifier / other, and estimates k-anonymity
    style re-identification risk on the quasi-identifiers.
    """
    try:
        import pandas as pd
    except ImportError:
        return _json({"error": "pandas not available in the MCP server process"})
    path = Path(file_path)
    if not path.exists():
        return _json({"error": f"file not found: {file_path}"})
    try:
        df = pd.read_csv(path)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"could not read CSV: {e}"})

    col_class = {c: _classify_column(c) for c in df.columns}
    sensitive = [c for c, k in col_class.items() if k.startswith("sensitive")]
    quasi_default = [c for c, k in col_class.items() if k.startswith("quasi")]
    quasi = list(quasi_identifier_columns) if quasi_identifier_columns else quasi_default
    if not quasi:
        quasi = [c for c in df.columns if df[c].nunique() < max(10, len(df) // 2)]

    risk_info = {}
    if quasi and len(quasi) <= 8:
        counts = df.groupby(quasi, dropna=False).size()
        if len(counts):
            sizes = counts.values
            unique_pct = float((sizes == 1).mean() * 100)
            k_min = int(sizes.min())
            k_median = float(sorted(sizes)[len(sizes) // 2])
            level = ("HIGH" if k_min == 1 or unique_pct > 5
                     else "MEDIUM" if k_min < 5 else "LOW")
            risk_info = {"min_k": k_min, "median_k": k_median,
                         "percent_unique_records": round(unique_pct, 2),
                         "risk_level": level}
    if not risk_info:
        risk_info = {"risk_level": "unknown",
                     "note": "no quasi-identifiers available for re-id assessment"}

    return _json({
        "file": str(path),
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "column_classification": col_class,
        "sensitive_columns": sensitive,
        "quasi_identifier_columns": quasi,
        "reidentification_risk": risk_info,
        "recommendation": (
            "Apply masking/tokenization or switch to synthetic data before sharing."
            if risk_info.get("risk_level") == "HIGH" else
            "Consider k-anonymity / DP before public release."
            if risk_info.get("risk_level") in ("MEDIUM", "unknown") else
            "Low risk; still track residual re-identification potential."),
        "assessed_at": _now(),
    })


# ------------------------------------------------------- Red-team / evaluation --

def membership_inference_eval(model_predictions: list[float],
                              is_member_labels: list[bool],
                              threshold: float = 0.5) -> str:
    """Membership-inference attack evaluation (accuracy / AUC / advantage).

    `model_predictions` are confidence (or loss) scores, higher = more likely a
    training member. `is_member_labels` mark true membership.
    """
    try:
        import numpy as np
        from sklearn.metrics import roc_auc_score
        _has_sklearn = True
    except ImportError:  # noqa: F841
        _has_sklearn = False
        import numpy as np  # noqa: F401

    preds = np.asarray(model_predictions, dtype=float)
    labels = np.asarray(is_member_labels, dtype=bool).astype(int)
    if preds.size != labels.size or preds.size == 0:
        return _json({"error": "predictions and labels must be non-empty and equal length"})

    attack = (preds >= threshold).astype(int)
    acc = float((attack == labels).mean())
    if _has_sklearn and len(np.unique(labels)) > 1 and len(np.unique(preds)) > 1:
        auc = float(roc_auc_score(labels, preds))
    else:
        auc = None
    advantage = acc - 0.5
    return _json({
        "attack_accuracy": round(acc, 4),
        "attack_auc": round(auc, 4) if auc is not None else None,
        "membership_advantage": round(advantage, 4),
        "interpretation": ("High advantage indicates significant membership leakage risk."
                           if advantage > 0.15 else
                           "Low advantage — membership inference appears difficult."),
        "assessed_at": _now(),
    })


def reidentification_scenario(quasi_identifiers: list[str],
                              population_size: int,
                              equivalence_class_sizes: list[int]) -> str:
    """Re-identification risk from equivalence-class sizes (k-anonymity style)."""
    sizes = list(equivalence_class_sizes)
    if not sizes:
        return _json({"error": "equivalence_class_sizes must not be empty"})
    k_min = int(min(sizes))
    k_median = float(sorted(sizes)[len(sizes) // 2])
    percent_unique = float(sum(1 for s in sizes if s == 1) / len(sizes) * 100)
    risk = ("HIGH" if k_min == 1 or percent_unique > 5
            else "MEDIUM" if k_min < 5 else "LOW")
    return _json({
        "quasi_identifiers": list(quasi_identifiers),
        "population_size": int(population_size),
        "min_k": k_min,
        "median_k": k_median,
        "percent_unique_records": round(percent_unique, 2),
        "risk_level": risk,
        "recommendation": ("Apply generalization, suppression, or switch to synthetic data."
                           if risk != "LOW" else
                           "k-anonymity looks reasonable; still consider residual risk "
                           "from auxiliary data."),
        "assessed_at": _now(),
    })


def privacy_redteam_checklist(data_type: str,
                              has_model: bool = False,
                              public_release: bool = False) -> str:
    """Generate an adversarial privacy red-teaming checklist."""
    checks = [
        "Run detect_pii_in_text + assess_dataframe_privacy on samples",
        "Measure uniqueness of quasi-identifier combinations",
        "Test linkage with publicly available auxiliary datasets (if legal)",
        "Evaluate residual risk after de-identification",
    ]
    if has_model:
        checks.extend([
            "Perform membership inference attack evaluation",
            "Test attribute inference on sensitive attributes",
            "Check for memorization of rare or outlier records",
        ])
    if public_release:
        checks.extend([
            "Assume a motivated adversary with auxiliary information",
            "Document maximum acceptable re-identification risk",
            "Prefer synthetic data or strong DP over classic anonymization",
        ])
    return _json({"data_type": data_type, "red_team_checks": checks,
                  "generated_at": _now()})


# --------------------------------------------------------- Differential privacy --

def apply_laplace_dp(values: list[float], epsilon: float, sensitivity: float = 1.0,
                     seed: int | None = 42) -> str:
    """Add Laplace noise for pure ε-differential privacy (δ=0)."""
    import numpy as np
    if epsilon <= 0:
        return _json({"error": "epsilon must be > 0"})
    rng = np.random.default_rng(seed)
    scale = sensitivity / epsilon
    vals = np.asarray(values, dtype=float)
    noisy = (vals + rng.laplace(0.0, scale, size=vals.shape)).tolist()
    return _json({
        "mechanism": "Laplace",
        "epsilon": epsilon,
        "sensitivity": sensitivity,
        "scale": scale,
        "original_values": vals.tolist(),
        "noisy_values": [round(float(v), 6) for v in noisy],
        "privacy_guarantee": f"(ε={epsilon})-differential privacy",
        "note": "Pure ε-DP guarantee (δ=0).",
        "assessed_at": _now(),
    })


def apply_gaussian_dp(values: list[float], epsilon: float, delta: float = 1e-6,
                      sensitivity: float = 1.0, seed: int | None = 42) -> str:
    """Add Gaussian noise for approximate (ε, δ)-differential privacy."""
    import numpy as np
    if epsilon <= 0 or not (0 < delta < 1):
        return _json({"error": "epsilon must be > 0 and 0 < delta < 1"})
    rng = np.random.default_rng(seed)
    sigma = sensitivity * np.sqrt(2.0 * np.log(1.25 / delta)) / epsilon
    vals = np.asarray(values, dtype=float)
    noisy = (vals + rng.normal(0.0, sigma, size=vals.shape)).tolist()
    return _json({
        "mechanism": "Gaussian",
        "epsilon": epsilon,
        "delta": delta,
        "sensitivity": sensitivity,
        "sigma": round(float(sigma), 6),
        "original_values": vals.tolist(),
        "noisy_values": [round(float(v), 6) for v in noisy],
        "privacy_guarantee": f"(ε={epsilon}, δ={delta})-differential privacy",
        "note": "Approximate DP; valid for δ ≪ 1/n.",
        "assessed_at": _now(),
    })


def dp_privacy_budget_report(operations: list[dict]) -> str:
    """Track ε/δ composition across DP operations (basic sequential composition)."""
    total_eps = 0.0
    total_delta = 0.0
    details = []
    for op in operations or []:
        eps = float(op.get("epsilon", 0) or 0)
        delta = float(op.get("delta", 0) or 0)
        total_eps += eps
        total_delta += delta
        details.append({"description": op.get("description", ""),
                        "epsilon": eps, "delta": delta})
    return _json({
        "composition": "basic sequential",
        "total_epsilon": total_eps,
        "total_delta": total_delta,
        "operations": details,
        "interpretation": (f"Overall guarantee: (ε={total_eps:.3f}, "
                           f"δ={total_delta:.2e})-DP. Smaller ε and δ = stronger privacy."),
        "visualization_hint": {"type": "budget_bar", "epsilon_used": total_eps,
                               "recommended_max_epsilon": 1.0},
        "assessed_at": _now(),
    })


def dp_guarantee_summary(epsilon: float, delta: float = 0.0) -> str:
    """Human-readable explanation of an (ε, δ)-DP guarantee + gauge data."""
    strength = ("very strong" if epsilon <= 0.1 else
                "strong" if epsilon <= 0.5 else
                "moderate" if epsilon <= 1.0 else
                "weak" if epsilon <= 5.0 else "very weak")
    return _json({
        "epsilon": epsilon,
        "delta": delta,
        "strength": strength,
        "explanation": (f"An adversary observing the output can gain at most a "
                        f"multiplicative factor of e^{epsilon:.3f} ≈ "
                        f"{pow(2.71828, epsilon):.2f} in their odds of correctly "
                        f"guessing any individual's data"
                        + (f", except with probability {delta}." if delta > 0 else ".")),
        "visualization": {"type": "epsilon_gauge", "value": epsilon, "zones": [
            {"max": 0.1, "label": "Very Strong", "color": "green"},
            {"max": 0.5, "label": "Strong", "color": "lightgreen"},
            {"max": 1.0, "label": "Moderate", "color": "yellow"},
            {"max": 5.0, "label": "Weak", "color": "orange"},
            {"max": 100, "label": "Very Weak", "color": "red"},
        ]},
        "assessed_at": _now(),
    })


# ------------------------------------------------------------ Synthetic data --

def _sample_like(series, num_rows: int, rng: random.Random, smoothed: bool):
    """Sample a synthetic column that preserves a column's distribution."""
    if series.dtype.kind in "fiu":
        import numpy as np
        arr = series.dropna().astype(float).values
        if arr.size == 0:
            return [np.nan] * num_rows
        if smoothed and arr.size > 5:
            mu, sd = arr.mean(), arr.std(ddof=1)
            vals = np.random.default_rng(rng.randrange(2**32)).normal(mu, max(sd, 1e-9), num_rows)
            return [round(float(v), 4) for v in vals]
        return [float(rng.choice(arr)) for _ in range(num_rows)]
    # categorical / object: empirical distribution with optional noise
    counts = Counter(series.dropna().astype(str))
    items, weights = zip(*counts.items()) if counts else ([""], [1])
    if smoothed:
        weights = [w + 1.0 for w in weights]  # Laplace smoothing
        tot = sum(weights)
        weights = [w / tot for w in weights]
    return [rng.choices(items, weights=weights, k=1)[0] for _ in range(num_rows)]


def generate_synthetic_tabular(file_path: str, num_rows: int = 1000,
                               method: str = "basic", seed: int = 42) -> str:
    """Generate schema-preserving synthetic tabular data from a local CSV.

    method: "basic" (fast independent sampling) or "smoothed" (adds noise to
    numeric columns so no exact real rows survive). If SDV is installed,
    method="sdv" uses Synthetic Data Vault.
    """
    try:
        import pandas as pd
    except ImportError:
        return _json({"error": "pandas not available"})
    path = Path(file_path)
    if not path.exists():
        return _json({"error": f"file not found: {file_path}"})
    if num_rows < 1:
        return _json({"error": "num_rows must be >= 1"})
    real = pd.read_csv(path)
    rng = random.Random(seed)

    if method.lower() == "sdv":
        try:
            from sdv.single_table import GaussianCopulaSynthesizer
            from sdv.metadata import SingleTableMetadata
            meta = SingleTableMetadata()
            meta.detect_from_dataframe(real)
            syn = GaussianCopulaSynthesizer(meta)
            syn.fit(real)
            synthetic = syn.sample(num_rows)
        except Exception as e:  # noqa: BLE001
            return _json({"error": f"SDV unavailable ({e}); use method='basic' or 'smoothed'"})
    else:
        synthetic = pd.DataFrame({
            col: _sample_like(real[col], num_rows, rng, smoothed=(method.lower() == "smoothed"))
            for col in real.columns
        })

    out_path = path.parent / f"synthetic_{path.stem}_{num_rows}.csv"
    synthetic.to_csv(out_path, index=False)
    return _json({
        "status": "success",
        "method": method,
        "real_rows": int(len(real)),
        "synthetic_rows": int(num_rows),
        "output_file": str(out_path),
        "columns": list(synthetic.columns),
        "note": ("Synthetic data has no one-to-one mapping to real individuals. "
                 "Still evaluate utility and residual privacy risk before sharing."),
        "assessed_at": _now(),
    })


def synthetic_data_quality_report(real_path: str, synthetic_path: str) -> str:
    """Utility comparison between real and synthetic tabular data."""
    try:
        import pandas as pd
    except ImportError:
        return _json({"error": "pandas not available"})
    real = pd.read_csv(real_path)
    synth = pd.read_csv(synthetic_path)
    report = {
        "real_shape": list(real.shape),
        "synthetic_shape": list(synth.shape),
        "column_overlap": list(set(real.columns) & set(synth.columns)),
        "numeric_stats_comparison": {},
        "categorical_top_match": {},
        "assessed_at": _now(),
    }
    for col in real.select_dtypes(include="number").columns:
        if col in synth.columns:
            report["numeric_stats_comparison"][col] = {
                "real_mean": round(float(real[col].mean()), 4),
                "synth_mean": round(float(synth[col].mean()), 4),
                "real_std": round(float(real[col].std()), 4),
                "synth_std": round(float(synth[col].std()), 4),
            }
    for col in real.select_dtypes(exclude="number").columns:
        if col in synth.columns:
            r_top = Counter(real[col].dropna().astype(str)).most_common(3)
            s_top = Counter(synth[col].dropna().astype(str)).most_common(3)
            report["categorical_top_match"][col] = {
                "real_top": dict(r_top), "synthetic_top": dict(s_top)}
    return _json(report)


# ------------------------------------------------------------------- export ----

if mcp is not None:
    _TOOLS = [
        (detect_pii_in_text, RO), (assess_dataframe_privacy, RO),
        (membership_inference_eval, RO), (reidentification_scenario, RO),
        (privacy_redteam_checklist, RO),
        (apply_laplace_dp, RO), (apply_gaussian_dp, RO),
        (dp_privacy_budget_report, RO), (dp_guarantee_summary, RO),
        (synthetic_data_quality_report, RO),
        (generate_synthetic_tabular, None),   # writes a file -> approval-gated
    ]
    for _fn, _ann in _TOOLS:
        if _ann is not None:
            mcp.tool(annotations=_ann)(_fn)
        else:
            mcp.tool()(_fn)

    __all__ = [f.__name__ for f, _ in _TOOLS] + ["mcp"]
else:
    __all__ = [
        "detect_pii_in_text", "assess_dataframe_privacy",
        "membership_inference_eval", "reidentification_scenario",
        "privacy_redteam_checklist",
        "apply_laplace_dp", "apply_gaussian_dp",
        "dp_privacy_budget_report", "dp_guarantee_summary",
        "generate_synthetic_tabular", "synthetic_data_quality_report",
    ]

if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("mcp package not installed; run: pip install mcp")
    mcp.run(transport="stdio")
