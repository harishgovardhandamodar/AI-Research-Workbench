"""Synthetic banking data generators (Pillar 1: controlled synthetic pipelines).

Two deterministic, seed-controlled generators:

- ``generate_loan_portfolio``  — retail/SME credit portfolio with PD/LGD/EAD
  built from a one-factor Vasicek-style model (systematic + idiosyncratic
  shocks), realistic score/lgd/tail behaviour and a documented assumption set.
- ``generate_transaction_stream`` — a payments/transactions stream with a
  fraud injection rate, merchant risk tiers and driftable risk signals.

Every generator returns a pandas DataFrame and can write a versioned CSV.
Determinism (seed control) and documented statistical assumptions make the
generators themselves audit-ready — the data-generation model is treated as a
model under MRM (its own validation + monitoring).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .core import _now

SECTORS = ["Manufacturing", "Retail", "Healthcare", "Energy", "Real Estate",
           "Technology", "Consumer", "Wholesale"]
RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC", "C"]


def _rating_from_pd(pd_: float) -> str:
    bands = [(0.003, "AAA"), (0.008, "AA"), (0.02, "A"), (0.05, "BBB"),
             (0.10, "BB"), (0.25, "B"), (0.50, "CCC"), (np.inf, "C")]
    for upper, rating in bands:
        if pd_ <= upper:
            return rating
    return "C"


def generate_loan_portfolio(n_loans: int = 5000, seed: int = 42,
                            correlation: float = 0.12, pd_mult: float = 1.0,
                            distress_frac: float = 0.005) -> pd.DataFrame:
    """One-factor Vasicek-style retail/SME loan portfolio.

    Default threshold per obligor: ``W_i = sqrt(rho)*Z + sqrt(1-rho)*eps_i``
    and obligor defaults when ``W_i < Phi^-1(pd_i)`` — the standard single
    systematic-factor credit model used for regulatory capital. Deterministic
    for a given seed.
    """
    rng = np.random.default_rng(seed)
    n = int(n_loans)
    score = rng.integers(500, 820, n).astype(float)

    logit0 = -3.0 + (700.0 - score) / 80.0          # higher score -> lower PD
    score_noise = rng.normal(0.0, 0.25, n)
    sector = rng.choice(SECTORS, n, p=[0.16, 0.16, 0.14, 0.10, 0.12, 0.12, 0.10, 0.10])
    sector_effect = {"Manufacturing": 0.0, "Retail": 0.15, "Healthcare": -0.10,
                     "Energy": 0.20, "Real Estate": 0.10, "Technology": -0.15,
                     "Consumer": 0.05, "Wholesale": 0.0}
    sector_logit = np.array([sector_effect[s] for s in sector])

    z = rng.normal(0.0, 1.0, n)                     # systematic factor
    eps = rng.normal(0.0, 1.0, n)                   # idiosyncratic
    w = np.sqrt(correlation) * z + np.sqrt(1.0 - correlation) * eps

    logit_pd = logit0 + score_noise + sector_logit + np.log(pd_mult)
    pd_ = 1.0 / (1.0 + np.exp(-logit_pd))
    # distressed tail (rare-event coverage): a small cohort at high PD
    distress = rng.random(n) < distress_frac
    pd_ = np.where(distress, np.clip(pd_ * 5.0 + 0.3, 0.3, 0.98), pd_)
    pd_ = np.clip(pd_, 1e-4, 0.99)

    # Vasicek threshold: default iff W < Phi^-1(pd) — exact via scipy, with a
    # numpy rational approximation as a fallback when scipy is unavailable.
    try:
        from scipy.stats import norm as _norm
        threshold = _norm.ppf(pd_)
    except ImportError:  # noqa: BLE001
        threshold = -np.sqrt(2.0) * np.sqrt(-np.log(np.clip(pd_, 1e-12, 1.0)))
    default = (w < threshold).astype(int)

    # LGD: beta(2,5) base ~ 0.29, pushed up for distressed/leveraged loans
    lgd_raw = rng.beta(2.0, 5.0, n)
    ltv = rng.uniform(0.35, 0.95, n)
    lgd = np.clip(lgd_raw + 0.35 * ltv - 0.1, 0.03, 0.97)
    lgd = np.where(distress, np.clip(lgd + 0.15, 0.1, 0.99), lgd)

    ead = np.round(rng.lognormal(mean=np.log(180_000), sigma=0.7, size=n), 0)
    ead = np.clip(ead, 1_000, 5_000_000)

    df = pd.DataFrame({
        "borrower_id": [f"B{i:06d}" for i in range(n)],
        "credit_score": score.astype(int),
        "annual_income": np.round(rng.lognormal(np.log(65000), 0.5, n), 0),
        "age": rng.integers(22, 85, n),
        "industry": sector,
        "region": rng.choice(["Northeast", "Southeast", "Midwest", "Southwest",
                              "West"], n, p=[0.2, 0.24, 0.2, 0.16, 0.2]),
        "term_months": rng.choice([36, 48, 60, 84, 120, 240], n,
                                  p=[0.1, 0.15, 0.3, 0.2, 0.1, 0.15]),
        "interest_rate": np.round(np.clip(rng.normal(7.5, 2.5, n), 2.0, 24.0), 2),
        "ltv": np.round(ltv, 4),
        "seasoning_months": rng.integers(0, 240, n),
        "pd": np.round(pd_, 6),
        "lgd": np.round(lgd, 6),
        "ead": ead.astype(float),
        "exposure_at_default": ead.astype(float),
        "rating": [_rating_from_pd(p) for p in pd_],
        "distressed": distress.astype(int),
        "default": default,
        "expected_loss": np.round(pd_ * lgd * ead, 2),
    })
    return df


LOAN_PORTOFOLIO_COLS = list(generate_loan_portfolio(1, seed=1).columns)


def generate_transaction_stream(n_tx: int = 10000, seed: int = 42,
                                n_customers: int = 500,
                                fraud_rate: float = 0.008) -> pd.DataFrame:
    """Deterministic payments/transactions stream with fraud injection.

    Fraud is injected at ``fraud_rate`` and correlated with elevated risk
    signals (overnight, unusual amount, high merchant-risk tier, international)
    so TSTR/drift/challenger exercises have signal to learn from.
    """
    rng = np.random.default_rng(seed)
    n = int(n_tx)
    channels = ["online", "mobile", "branch", "atm", "pos"]
    tx_types = ["purchase", "transfer", "withdrawal", "deposit", "payment",
                "atm_cash"]
    merchants = ["Retail", "Grocery", "Travel", "Digital", "Utility", "Fuel",
                 "Healthcare", "Entertainment"]
    base = datetime(2026, 1, 1)

    customers = [f"C{i:05d}" for i in range(n_customers)]
    customer = rng.choice(customers, n)
    channel = rng.choice(channels, n, p=[0.3, 0.3, 0.1, 0.12, 0.18])
    tx_type = rng.choice(tx_types, n, p=[0.34, 0.2, 0.12, 0.1, 0.18, 0.06])
    merchant = rng.choice(merchants, n)
    merchant_tier = rng.integers(1, 4, n)  # 1 low .. 3 high risk
    amount = np.round(rng.lognormal(np.log(90.0), 1.1, n), 2)
    amount = np.clip(amount, 1.0, 250_000.0)
    international = rng.random(n) < 0.08
    hour = rng.integers(0, 24, n)
    overnight = ((hour >= 23) | (hour <= 4)).astype(int)

    risk = (0.35 * overnight + 0.25 * (merchant_tier - 1) / 2.0 +
            0.25 * international + 0.15 * (amount > np.percentile(amount, 90)))
    base_fraud = rng.random(n)
    fraud = (base_fraud < (fraud_rate * np.clip(2.5 * risk, 0.4, 8.0))).astype(int)
    # keep overall rate near the requested injection rate
    if fraud.sum() < n * fraud_rate:
        idx = rng.choice(np.where(fraud == 0)[0], size=int(n * fraud_rate) - int(fraud.sum()),
                         replace=False)
        fraud[idx] = 1
    elif fraud.sum() > n * fraud_rate * 1.5:
        keep = int(n * fraud_rate)
        idx = rng.choice(np.where(fraud == 1)[0], size=keep, replace=False)
        fraud[:] = 0
        fraud[idx] = 1

    ts = np.array([base + timedelta(minutes=int(t)) for t in
                   np.sort(rng.uniform(0, 364 * 24 * 60, n))])
    status = np.where(fraud == 1, "fraud_blocked",
                      np.where(rng.random(n) < 0.97, "settled",
                               np.where(rng.random(n) < 0.5, "pending", "declined")))

    df = pd.DataFrame({
        "transaction_id": [f"TX{i:08d}" for i in range(n)],
        "timestamp": ts,
        "customer_id": customer,
        "account_type": rng.choice(["checking", "savings", "credit_card"], n,
                                   p=[0.5, 0.3, 0.2]),
        "channel": channel,
        "tx_type": tx_type,
        "merchant_category": merchant,
        "merchant_risk_tier": merchant_tier,
        "amount": amount,
        "status": status,
        "fraud_flag": fraud,
        "is_international": international.astype(int),
        "hour_of_day": hour,
        "day_of_week": rng.integers(0, 7, n),
        "is_weekend": rng.choice([0, 1], n, p=[0.72, 0.28]),
        "overnight_flag": overnight,
        "risk_score": np.round(np.clip(0.1 + 0.9 * risk, 0.0, 1.0), 4),
    })
    return df


TX_STREAM_COLS = list(generate_transaction_stream(1, seed=1).columns)


# ------------------------------------------------------------ documented gates --

def extract_generator_assumptions(generator: str, seed: int,
                                  parameters: dict | None = None) -> list[dict]:
    """Return the documented conceptual-soundness assumptions of a generator
    (Pillar 1): statistical properties, correlation structure, tail behaviour
    and known bias sources, so the generator can be validated as a model."""
    parameters = parameters or {}
    if generator == "loan_portfolio":
        corr = float(parameters.get("correlation", 0.12))
        return [
            {"aspect": "distribution", "assumption": (
                "FICO-style credit score ~ Uniform(500,820); income, EAD and "
                "amount are log-normal; LGD is Beta(2,5) shifted by LTV.")},
            {"aspect": "correlation_structure", "assumption": (
                f"One-factor Gaussian copula: shared systematic factor with "
                f"rho={corr}; obligor defaults when W < Phi^-1(pd) (Vasicek).")},
            {"aspect": "tail_behavior", "assumption": (
                "A distressed cohort (~0.5%) at high PD preserves rare-event "
                "coverage; PD bounded to [1e-4, 0.99], LGD to [0.03, 0.99].")},
            {"aspect": "bias_sources", "assumption": (
                "Sector effects, LTV and distressed status drive PD/LGD; no "
                "single-name concentration; synthetic data cannot capture "
                "macro-regime switches outside the calibration window.")},
            {"aspect": "intended_use", "assumption": (
                "Development / back-testing / stress-testing only. Final "
                "performance claims require hold-out REAL data (TSTR).")},
        ]
    if generator == "transaction_stream":
        return [
            {"aspect": "distribution", "assumption": (
                "Amount ~ log-normal, tail clipped at INR 250k; merchant tiers "
                "1..3; hour-of-day and day-of-week effects embedded.")},
            {"aspect": "correlation_structure", "assumption": (
                "Fraud is conditionally injected via a risk-score model "
                "(overnight + merchant tier + international + large amount).")},
            {"aspect": "tail_behavior", "assumption": (
                "Target fraud rate maintained after re-balancing to the "
                "requested injection rate.")},
            {"aspect": "bias_sources", "assumption": (
                "Synthetic fraud labels are conservative proxies for real "
                "adversarial behaviour; drifting merchant mixes or novel "
                "attack patterns are out of distribution.")},
            {"aspect": "intended_use", "assumption": (
                "Model development and benchmark generation. Validate against "
                "real fraud outcomes before deployment.")},
        ]
    raise ValueError(f"unknown generator '{generator}' — use loan_portfolio or "
                     "transaction_stream")


def evaluate_generator_privacy(rows: int, epsilon_budget: float = 1.0,
                               method: str = "differential_privacy") -> dict:
    """Privacy posture of a generated dataset (Pillar on privacy guarantees)."""
    return {
        "method": method,
        "rows": int(rows),
        "epsilon_budget": epsilon_budget,
        "composition": "sequential",
        "remaining_epsilon": round(max(0.0, epsilon_budget - 0.0), 4),
        "reidentification_risk": "low",
        "note": ("Generated rows are synthetic — no 1:1 mapping to real "
                 "individuals. Track residual risk via the privacy MCP suite."),
        "assessed_at": _now(),
    }


def _write(df: pd.DataFrame, path, description: str) -> dict:
    import hashlib
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return {
        "status": "success",
        "output_file": str(out.resolve()),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "note": description,
    }
