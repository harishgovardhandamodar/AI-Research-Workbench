"""Local simplified replication of arXiv 2409.12642.

Paper: "Deep generative models as an adversarial attack strategy for tabular
machine learning" (Dyrmishi et al., 2024) — https://arxiv.org/pdf/2409.12642

The paper adapts tabular Deep Generative Models (WGAN/TableGAN/CTGAN/OCT-GAN)
into adversarial generators and reports up to ~95% Attack Success Rate (ASR) on
HELOC. A faithful replication requires training DGMs (heavy). This script runs a
tractable, LOCAL approximation with full provenance:

    synthetic tabular task -> logistic classifier -> PGD attack
        -> ASR measured vs eps -> compared against the authors' reported ASR
        -> a comparison report is printed

It reuses the `robustness` MCP functions (adversarial checklist, robustness
metrics) and the `arxiv` MCP `compare_results` tool, so the numbers plug
straight into the rest of the workbench.

Run it in the workbench kernel (figures/artifacts) or standalone:

    .venv/bin/python examples/arxiv/run_local_replication.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402

from examples.adversarial import perturb_batch  # noqa: E402
from mcp_servers import robustness_tools as rt  # noqa: E402
from mcp_servers.arxiv_replication import compare_results  # noqa: E402

PAPER_ID = "2409.12642"
PAPER_URL = "https://arxiv.org/pdf/2409.12642"
AUTHOR_REPORTED_ASR = 0.95      # best ASR reported by the paper (HELOC, AdvDGM)
TOLERANCE = 0.20                # relative tolerance for "match"
EPSILONS = [0.5, 1.0, 2.0]


def _synthetic_tabular_task(seed: int = 1):
    """A cleanly separable 2-class tabular task where PGD demonstrably flips."""
    rng = np.random.default_rng(seed)
    X = np.vstack([rng.normal([0, 0], 1, (600, 2)),
                   rng.normal([3, 3], 1, (600, 2))])
    y = np.concatenate([np.zeros(600), np.ones(600)])
    return X, y


def main():
    print("=" * 66)
    print(f"LOCAL REPLICATION — arXiv {PAPER_ID}")
    print(PAPER_URL)
    print("=" * 66)

    print("\nStep 1 — threat-model checklist (robustness MCP):")
    print(rt.adversarial_robustness_checklist("sklearn", "tabular", high_stakes=False))

    print("\nStep 2 — tabular task + logistic classifier (tractable proxy for "
          "the paper's DGM-based attack):")
    X, y = _synthetic_tabular_task()
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.3, random_state=1)
    model = LogisticRegression(max_iter=2000).fit(Xtr, ytr)
    clean = model.predict(Xte)
    print(f"  samples={len(X)}  features={X.shape[1]}  clean accuracy="
          f"{(clean == yte).mean():.3f}")

    print("\nStep 3 — run a PGD attack (robustness MCP) and measure ASR vs eps:")
    rows = []
    for eps in EPSILONS:
        Xa = perturb_batch(model, Xte, yte, eps)
        adv = model.predict(Xa)
        r = json.loads(rt.robustness_metrics_from_predictions(
            clean.tolist(), adv.tolist(), yte.tolist()))
        rows.append({"eps": eps, "clean": r["clean_accuracy"],
                     "robust": r["robust_accuracy"],
                     "asr": r["attack_success_rate_on_correct"]})
    sweep = pd.DataFrame(rows)
    print(sweep.to_string(index=False))

    own_asr = float(sweep["asr"].iloc[-1])          # ASR at the strongest eps
    print("\nStep 4 — compare local ASR vs the authors' reported ASR "
          "(arxiv MCP compare_results):")
    cmp = json.loads(compare_results(
        json.dumps({"ASR": AUTHOR_REPORTED_ASR}),
        json.dumps({"ASR": round(own_asr, 3)}),
        tolerance=TOLERANCE))
    print(json.dumps(cmp, indent=2))

    print("\n" + "=" * 66)
    print("VERDICT")
    print("=" * 66)
    s = cmp["summary"]
    print(f"  Paper reported ASR {AUTHOR_REPORTED_ASR} vs local {own_asr:.3f} "
          f"@ eps={sweep['eps'].iloc[-1]} (tolerance {TOLERANCE:.0%}):")
    print(f"  matches={s['matches']}  discrepancies={s['discrepancies']}  "
          f"missing={s['missing']}")
    print("  NOTE: this is a tractable proxy — the paper trains tabular DGMs, "
          "this run uses a logistic regression + PGD attack on a synthetic "
          "tabular task. Treat it as a methodology demonstration of the "
          "ingest -> replicate -> compare -> report loop, not a faithful "
          "reproduction of the paper's numbers.")


if __name__ == "__main__":
    main()
