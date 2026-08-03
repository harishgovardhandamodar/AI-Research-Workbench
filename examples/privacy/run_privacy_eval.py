"""Privacy red-team + DP + synthetic-data evaluation using the privacy MCP tools.

Exercises the privacy MCP server functions end-to-end on a small clinical
cohort and produces three figures (auto-captured as workbench artifacts):

    Fig 1  equivalence-class / re-identification risk
    Fig 2  original vs Laplace-DP noisy histogram (ε-gauge inset)
    Fig 3  real vs synthetic distributions + utility comparison

Run it in the Fox kernel (figures become artifacts) or standalone:

    .venv/bin/python examples/privacy/run_privacy_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from examples.privacy.clinical_cohort import build_cohort  # noqa: E402
from mcp_servers import privacy_tools as pt  # noqa: E402

COHORT = Path(__file__).resolve().parent / "clinical_cohort.csv"
SYNTH = Path(__file__).resolve().parent / "synthetic_clinical_cohort_1000.csv"
SEED = 7


def main():
    df = build_cohort(200, seed=SEED)
    COHORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(COHORT, index=False)

    print("=" * 66)
    print("1) ASSESSMENT — small clinical cohort (n=200)")
    print("=" * 66)
    assess = pt.assess_dataframe_privacy(str(COHORT))
    print(assess)
    risk = np.array([df.groupby(["age", "zip_prefix", "condition"]).size().values]).ravel()

    print("\n" + "=" * 66)
    print("2) RED-TEAM — checklist + membership inference + re-identification")
    print("=" * 66)
    print(pt.privacy_redteam_checklist("clinical cohort", has_model=False,
                                       public_release=True))
    rng = np.random.default_rng(SEED)
    preds = np.concatenate([rng.uniform(0.55, 0.99, 400), rng.uniform(0.01, 0.45, 400)])
    labels = [True] * 400 + [False] * 400
    print(pt.membership_inference_eval(preds.tolist(), labels, threshold=0.5))
    print(pt.reidentification_scenario(
        ["age", "zip_prefix", "condition"], population_size=50_000,
        equivalence_class_sizes=risk.tolist()))

    # ---- Figure 1: re-identification risk ----------------------------------
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    ax.hist(risk, bins=30, color="#4f8cff", edgecolor="#161c24")
    unique = (risk == 1).mean() * 100
    ax.axvline(1, color="#e05b5b", ls="--", lw=1.2,
               label=f"{unique:.0f}% singleton classes")
    ax.set_xlabel("equivalence class size (age · zip · condition)")
    ax.set_ylabel("classes")
    ax.set_yscale("log")
    ax.legend()
    ax.set_title("Fig 1 — re-identification risk (small clinical cohort)")
    if __name__ == "__main__":
        fig.savefig("examples/privacy/fig1_reidentification.png", dpi=150)

    print("\n" + "=" * 66)
    print("3) DIFFERENTIAL PRIVACY — Laplace on monthly admission counts")
    print("=" * 66)
    counts = df.groupby(df["admission_date"].str[:7]).size().sort_index()
    eps = 0.5
    dp = json.loads(pt.apply_laplace_dp(counts.tolist(), epsilon=eps,
                                        sensitivity=1.0, seed=SEED))
    print(pt.dp_guarantee_summary(eps, 0.0))
    print(pt.dp_privacy_budget_report([
        {"epsilon": 0.5, "delta": 0.0, "description": "admission-count histogram"},
        {"epsilon": 0.3, "delta": 0.0, "description": "mean visit amount"},
    ]))

    # ---- Figure 2: DP histogram comparison + ε gauge ------------------------
    fig, (ax, axg) = plt.subplots(1, 2, figsize=(10.5, 3.8),
                                  gridspec_kw={"width_ratios": [2.6, 1]})
    x = np.arange(len(counts))
    ax.bar(x - 0.2, counts.values, width=0.4, label="original", color="#e05b5b")
    ax.bar(x + 0.2, dp["noisy_values"], width=0.4, label=f"Laplace (ε={eps})",
           color="#35c4b6")
    ax.set_xticks(x)
    ax.set_xticklabels(counts.index, rotation=45, fontsize=7)
    ax.set_ylabel("admissions / month")
    ax.legend()
    ax.set_title("Fig 2 — DP noise on monthly admission counts")
    # ε gauge
    gauge = np.linspace(0, 2.0, 300)
    axg.barh(gauge, 1, color="#d9a441")
    axg.axvline(eps, color="#e05b5b", lw=2.5)
    axg.set_xlim(0, 1.0)
    axg.set_xlabel("ε"); axg.set_yticks([])
    axg.set_title("ε-gauge (marker = this query)")
    if __name__ == "__main__":
        fig.savefig("examples/privacy/fig2_dp_histogram.png", dpi=150)

    print("\n" + "=" * 66)
    print("4) SYNTHETIC DATA — schema-preserving generation + quality")
    print("=" * 66)
    gen = pt.generate_synthetic_tabular(str(COHORT), num_rows=1000,
                                        method="smoothed", seed=SEED)
    print(gen)
    print(pt.synthetic_data_quality_report(str(COHORT), str(SYNTH)))

    # ---- Figure 3: real vs synthetic distributions ---------------------------
    synth = pd.read_csv(SYNTH)
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.8))
    for ax, col in zip(axes, ["age", "visit_amount_usd"]):
        ax.hist(df[col], bins=25, alpha=0.6, label="real", color="#4f8cff")
        ax.hist(synth[col], bins=25, alpha=0.6, label="synthetic", color="#35c4b6")
        ax.set_xlabel(col); ax.set_ylabel("count"); ax.legend()
    axes[0].set_title("Fig 3 — age (real vs synthetic)")
    axes[1].set_title("Fig 3 — visit amount (real vs synthetic)")
    if __name__ == "__main__":
        fig.savefig("examples/privacy/fig3_synthetic.png", dpi=150)

    print("\nDone. Figures + cohort CSV under examples/privacy/")


if __name__ == "__main__":
    main()
