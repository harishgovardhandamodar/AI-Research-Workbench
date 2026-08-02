"""Experiment 01 — Simple: exponential decay kinetics and half-life estimation.

A classic first experiment. We simulate a radioactive-decay / enzyme-inactivation
time course, fit a single exponential, and report the half-life with confidence
bounds. Produces one figure (data + fit + residuals) and a printed results table.

Run it in the Fox workbench kernel (figures auto-become artifacts) or standalone:

    python examples/experiments/01_simple_decay_fit.py
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import curve_fit
from scipy import stats

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ------------------------------------------------------------------ settings --
SEED = 42
N_POINTS = 24
TRUE_DECAY_RATE = 0.12          # per hour
TRUE_INITIAL = 100.0            # arbitrary units
NOISE_FRACTION = 0.06           # relative measurement noise
TIMES_HOURS = np.linspace(0.0, 40.0, N_POINTS)

# -------------------------------------------------------------------- model ---
def exponential_decay(t, A0, k):
    """A(t) = A0 * exp(-k*t)"""
    return A0 * np.exp(-k * t)


def simulate_data():
    rng = np.random.default_rng(SEED)
    signal = exponential_decay(TIMES_HOURS, TRUE_INITIAL, TRUE_DECAY_RATE)
    noise = rng.normal(0.0, NOISE_FRACTION * TRUE_INITIAL, size=TIMES_HOURS.size)
    return signal + noise


def fit_decay(measurement: np.ndarray):
    """Fit the exponential and return parameters + covariance."""
    popt, pcov = curve_fit(exponential_decay, TIMES_HOURS, measurement,
                           p0=(80.0, 0.1))
    A0_fit, k_fit = popt
    perr = np.sqrt(np.diag(pcov))
    # Propagate to half-life: t1/2 = ln(2)/k
    t_half = np.log(2.0) / k_fit
    t_half_err = np.log(2.0) * perr[1] / k_fit**2
    ci = stats.t.ppf(0.975, df=N_POINTS - 2)
    return {
        "A0": A0_fit, "A0_err": perr[0],
        "k": k_fit, "k_err": perr[1],
        "t_half": t_half, "t_half_err": t_half_err,
        "t_half_ci": ci * t_half_err,
        "fit_pcov": pcov,
    }


def plot_results(measurement, fit, path: str | None = None):
    t_fine = np.linspace(TIMES_HOURS.min(), TIMES_HOURS.max(), 300)
    fig, (ax, axr) = plt.subplots(
        2, 1, figsize=(7.0, 5.6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})

    ax.errorbar(TIMES_HOURS, measurement, yerr=NOISE_FRACTION * TRUE_INITIAL,
                fmt="o", ms=4, color="#4f8cff", label="measurement")
    ax.plot(t_fine, exponential_decay(t_fine, fit["A0"], fit["k"]),
            color="#35c4b6", lw=2, label="fit  A0=%.1f, k=%.4f/h" % (fit["A0"], fit["k"]))
    ax.axvline(fit["t_half"], color="#d9a441", ls="--", lw=1.2,
               label="t1/2 = %.1f h" % fit["t_half"])
    ax.set_ylabel("Activity (a.u.)")
    ax.legend(frameon=False, fontsize=9)
    ax.set_title("Exponential decay kinetics — half-life estimation")

    res = measurement - exponential_decay(TIMES_HOURS, fit["A0"], fit["k"])
    axr.scatter(TIMES_HOURS, res, s=14, color="#e05b5b")
    axr.axhline(0, color="#8b97a5", lw=0.8)
    axr.set_ylabel("Residual")
    axr.set_xlabel("Time (hours)")
    return fig


def main():
    measurement = simulate_data()
    fit = fit_decay(measurement)

    fig = plot_results(measurement, fit)
    # In the Fox kernel the open figure is captured automatically as an artifact.
    # When run as a standalone script, save it to disk instead.
    if __name__ == "__main__":
        fig.savefig("examples/experiments/01_simple_decay_fit.png", dpi=150)

    print("=" * 64)
    print("EXPERIMENT 01 — Exponential decay fit (half-life estimation)")
    print("=" * 64)
    print(f"  True decay rate      : {TRUE_DECAY_RATE:.4f} /h")
    print(f"  Fitted decay rate    : {fit['k']:.4f} ± {fit['k_err']:.4f} /h")
    print(f"  Fitted A0            : {fit['A0']:.2f} ± {fit['A0_err']:.2f}")
    print(f"  Half-life            : {fit['t_half']:.2f} ± {fit['t_half_err']:.2f} h")
    print(f"  95% CI               : [{fit['t_half'] - fit['t_half_ci']:.2f}, "
          f"{fit['t_half'] + fit['t_half_ci']:.2f}] h")
    print("  True half-life       : %.2f h" % (np.log(2.0) / TRUE_DECAY_RATE))
    r2 = 1 - np.sum((measurement - exponential_decay(TIMES_HOURS, fit["A0"], fit["k"]))**2) / \
             np.sum((measurement - measurement.mean())**2)
    print(f"  R^2 (goodness of fit): {r2:.4f}")
    print("=" * 64)


if __name__ == "__main__":
    main()
