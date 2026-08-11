"""Simulation engines (Pillar 1/2): Monte Carlo loss, scenario sets, stress,
version comparison and sensitivity — all deterministic under a seed.

The Monte Carlo engine is the standard one-factor Vasicek portfolio model:
each path draws a systematic factor ``Z``, obligors default when
``W_i = sqrt(rho)Z + sqrt(1-rho)eps_i < Phi^-1(pd_i)``, and portfolio loss is
``sum(ead_i * lgd_i * 1{default_i})``. Scenario sets overlay PD multipliers and
asset-correlation shocks (baseline / mild / severe / systemic / upside).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .core import _now

REQUIRED_PORTFOLIO_COLS = ("pd", "ead", "lgd")


def load_portfolio(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"portfolio file not found: {path}")
    try:
        df = pd.read_csv(p)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not read portfolio '{path}': {e}") from e
    missing = [c for c in REQUIRED_PORTFOLIO_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"portfolio must contain columns {list(REQUIRED_PORTFOLIO_COLS)}; "
            f"missing {missing}")
    df["pd"] = df["pd"].astype(float).clip(1e-6, 0.9999)
    df["lgd"] = df["lgd"].astype(float).clip(0.0, 1.0)
    df["ead"] = df["ead"].astype(float).clip(lower=0.0)
    return df


def _ppf(p: np.ndarray) -> np.ndarray:
    try:
        from scipy.stats import norm as _norm
        return _norm.ppf(p)
    except ImportError:  # noqa: BLE001
        return -np.sqrt(2.0) * np.sqrt(-np.log(np.clip(p, 1e-12, 1.0)))


def monte_carlo_loss(portfolio_path: str, n_paths: int = 5000, seed: int = 42,
                     correlation: float = 0.12, pd_mult: float = 1.0,
                     horizon: float = 1.0) -> dict:
    """Simulate portfolio credit loss under a one-factor model.

    Returns the full loss distribution summary (mean, std, percentiles, VaR 99,
    ES 97.5) plus a histogram so results can be charted.
    """
    df = load_portfolio(portfolio_path)
    n = len(df)
    if n_paths < 100:
        raise ValueError("n_paths must be >= 100")
    rng = np.random.default_rng(seed)
    pd_ = np.clip(df["pd"].to_numpy() * pd_mult, 1e-6, 0.9999)
    threshold = _ppf(pd_)
    ead = df["ead"].to_numpy()
    lgd = df["lgd"].to_numpy()
    rho = float(np.clip(correlation, 0.0, 0.5))

    z = rng.normal(0.0, 1.0, size=(int(n_paths),))
    eps = rng.normal(0.0, 1.0, size=(int(n_paths), n))
    w = np.sqrt(rho) * z[:, None] + np.sqrt(1.0 - rho) * eps
    losses = np.where(w < threshold[None, :], ead[None, :] * lgd[None, :], 0.0)
    path_loss = losses.sum(axis=1)
    default_counts = (w < threshold[None, :]).sum(axis=1)

    total_ead = float(ead.sum())
    loss = {
        "model": "one-factor Vasicek",
        "portfolio_size": n,
        "total_exposure": round(total_ead, 2),
        "n_paths": int(n_paths),
        "seed": seed,
        "horizon_years": float(horizon),
        "expected_loss": round(float(path_loss.mean()), 2),
        "expected_loss_pct": round(float(path_loss.mean() / total_ead), 6),
        "std_loss": round(float(path_loss.std(ddof=1)), 2),
        "p50_loss": round(float(np.percentile(path_loss, 50)), 2),
        "p95_loss": round(float(np.percentile(path_loss, 95)), 2),
        "var_99": round(float(np.percentile(path_loss, 99)), 2),
        "es_97_5": round(float(np.mean(path_loss[path_loss >= np.percentile(
            path_loss, 97.5)])), 2),
        "mean_default_rate": round(float(default_counts.mean() / n), 6),
        "loss_histogram": {
            "bins": np.linspace(0.0, float(np.percentile(path_loss, 99.9)), 30).tolist(),
            "counts": np.histogram(path_loss, bins=30)[0].tolist(),
        },
        "assessed_at": _now(),
    }
    return loss


SCENARIO_PROFILES = {
    "baseline": {"pd_mult": 1.0, "correlation": 0.12, "label": "Baseline / soft landing"},
    "mild_recession": {"pd_mult": 1.8, "correlation": 0.2, "label": "Mild recession"},
    "severe_recession": {"pd_mult": 3.2, "correlation": 0.3, "label": "Severe recession"},
    "systemic_stress": {"pd_mult": 4.5, "correlation": 0.4, "label": "Systemic / 2008-like stress"},
    "upside": {"pd_mult": 0.6, "correlation": 0.08, "label": "Upside / expansion"},
}


def run_scenario_set(portfolio_path: str, scenarios: list[str] | None = None,
                     n_paths: int = 3000, seed: int = 42) -> dict:
    """Run the named scenario suite (or a subset) over the portfolio.

    Each scenario overlays a PD multiplier and asset-correlation shock on the
    one-factor engine and reports expected loss, VaR 99 and default rate.
    """
    names = scenarios or list(SCENARIO_PROFILES)
    unknown = [s for s in names if s not in SCENARIO_PROFILES]
    if unknown:
        raise ValueError(f"unknown scenarios {unknown} — available: "
                         f"{list(SCENARIO_PROFILES)}")
    results = []
    for i, name in enumerate(names):
        prof = SCENARIO_PROFILES[name]
        res = monte_carlo_loss(portfolio_path, n_paths=n_paths,
                               seed=seed + i * 101,  # distinct stream per scenario
                               correlation=prof["correlation"],
                               pd_mult=prof["pd_mult"])
        results.append({"scenario": name, "label": prof["label"], **res})
    results.sort(key=lambda r: r["expected_loss"])
    return {
        "portfolio_path": str(Path(portfolio_path).resolve()),
        "scenarios": results,
        "ordering": [r["scenario"] for r in results],
        "assessed_at": _now(),
    }


def stress_test_portfolio(portfolio_path: str, severity: float = 3.2,
                          n_paths: int = 3000, seed: int = 42) -> dict:
    """Stress the portfolio at ``severity`` (a PD multiplier).

    Reports the stressed loss distribution alongside the baseline delta and a
    relative impact read-out for board/senior-management reporting.
    """
    base = monte_carlo_loss(portfolio_path, n_paths=n_paths, seed=seed)
    stress = monte_carlo_loss(portfolio_path, n_paths=n_paths, seed=seed + 1,
                              pd_mult=float(severity),
                              correlation=0.3)
    delta_var = stress["var_99"] - base["var_99"]
    return {
        "severity": float(severity),
        "baseline": base,
        "stressed": stress,
        "impact": {
            "expected_loss_multiple": round(stress["expected_loss"] / base["expected_loss"], 2),
            "var99_delta": round(delta_var, 2),
            "var99_delta_pct": round(delta_var / base["var_99"], 4),
            "read_out": (f"Stressing PD by {severity:.2f}x raises portfolio "
                         f"VaR(99) by {delta_var / base['var_99']:.1%} — "
                         f"capital buffer impact requires model-level review."),
        },
        "assessed_at": _now(),
    }


def compare_simulation_versions(version_a: dict, version_b: dict) -> dict:
    """Delta between two simulation results (e.g. engine v1 vs v2)."""
    keys = ("expected_loss", "var_99", "es_97_5", "mean_default_rate",
            "expected_loss_pct")
    deltas = {}
    for k in keys:
        deltas[k] = round(float(version_b.get(k, 0.0)) - float(version_a.get(k, 0.0)), 6)
    drift = abs(deltas["expected_loss"]) > max(0.01 * abs(version_a.get("expected_loss", 1.0)), 1e-6)
    return {
        "version_a": {"expected_loss": version_a.get("expected_loss"),
                      "var_99": version_a.get("var_99")},
        "version_b": {"expected_loss": version_b.get("expected_loss"),
                      "var_99": version_b.get("var_99")},
        "deltas": deltas,
        "material_difference": bool(drift),
        "note": ("Material differences between simulation versions must be "
                 "reviewed before re-baselining." if drift else
                 "Versions agree within tolerance — safe to treat as equivalent."),
        "assessed_at": _now(),
    }


def sensitivity_analysis(portfolio_path: str, parameter: str = "pd_mult",
                         values: list[float] | None = None,
                         n_paths: int = 2000, seed: int = 42) -> dict:
    """One-at-a-time sensitivity of portfolio loss to ``parameter``.

    Currently supports ``pd_mult`` (PD multiplier): expected loss and VaR 99 are
    reported at each value so the validation team can challenge tail risk.
    """
    if parameter != "pd_mult":
        raise ValueError("sensitivity_analysis currently supports parameter='pd_mult'")
    vals = values or [0.5, 1.0, 2.0, 3.0, 4.5]
    out = []
    for i, v in enumerate(vals):
        res = monte_carlo_loss(portfolio_path, n_paths=n_paths,
                               seed=seed + i * 31, pd_mult=float(v))
        out.append({"value": float(v), "expected_loss": res["expected_loss"],
                    "var_99": res["var_99"], "es_97_5": res["es_97_5"],
                    "mean_default_rate": res["mean_default_rate"]})
    return {
        "parameter": parameter,
        "portfolio_path": str(Path(portfolio_path).resolve()),
        "points": out,
        "monotonic_in_pd": all(
            out[i]["expected_loss"] <= out[i + 1]["expected_loss"]
            for i in range(len(out) - 1)),
        "assessed_at": _now(),
    }
