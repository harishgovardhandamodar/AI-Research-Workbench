"""Banking-domain MRM profiles: pre-baked model risk profiles that map a
business area (credit risk, market risk, CECL, stress testing, fraud/AML,
pricing) to its generators, validation suite, metrics and SR 11-7 pillars.

A profile gives the validation team a consistent, regulator-aligned checklist
for any simulation-backed model in that domain and drives which tools a coding
agent should chain together (see ``server.list_profiles``).
"""

from __future__ import annotations

BANKING_PROFILES = {
    "credit_risk": {
        "display": "Retail / SME Credit Risk (PD-LGD)",
        "tier_default": 1,
        "models": ["PD model", "LGD model", "EAD model", "Counterparty credit risk"],
        "generators": ["loan_portfolio"],
        "validation": ["fidelity", "tstr", "drift", "challenger", "sensitivity",
                       "stress", "monte_carlo"],
        "metrics": ["roc_auc", "ks", "brier", "calibration", "coverage"],
        "controls": ["tier", "approval", "effective_challenge"],
        "synthetic_data": "mandatory_tstr",
        "privacy": "dp_or_equivalent",
        "doc_refs": ["SR 11-7", "2026 interagency risk-based updates", "Basel IRB"],
        "pillars": {
            "p1_development": "Document conceptual soundness of PD/LGD models AND the "
                               "synthetic portfolio generator (copula, calibration).",
            "p2_validation": "Independent validation + TSTR on hold-out real data; "
                             "challenger models and generator stress.",
            "p3_governance": "Tier-1 maker-checker; loss-data lineage; board reporting.",
        },
    },
    "market_risk": {
        "display": "Market Risk (VaR / ES / pricing)",
        "tier_default": 1,
        "models": ["Market risk VaR", "Expected Shortfall", "Derivative pricing"],
        "generators": ["loan_portfolio"],
        "validation": ["monte_carlo", "scenario", "stress", "sensitivity", "fidelity"],
        "metrics": ["var_99", "es_97_5", "backtest_exceptions", "p95_loss"],
        "controls": ["tier", "approval"],
        "synthetic_data": "stress_simulations",
        "privacy": "market_data_not_individual",
        "doc_refs": ["SR 11-7", "Basel FRTB", "2026 interagency updates"],
        "pillars": {
            "p1_development": "Document VaR engine assumptions, correlation "
                              "structure and tail behaviour of simulated returns.",
            "p2_validation": "Backtesting + challenger engines; scenario overlays "
                             "including systemic stress.",
            "p3_governance": "Model inventory tiering; escalation of backtest failures.",
        },
    },
    "stress_testing": {
        "display": "Enterprise Stress Testing (CCAR / BESTS-like)",
        "tier_default": 1,
        "models": ["Balance-sheet stress model", "Revenue/expense scenario model"],
        "generators": ["loan_portfolio", "transaction_stream"],
        "validation": ["scenario", "stress", "monte_carlo", "tstr", "sensitivity"],
        "metrics": ["expected_loss", "var_99", "default_rate", "impact_ratios"],
        "controls": ["tier", "approval", "effective_challenge"],
        "synthetic_data": "scenario_simulations",
        "privacy": "aggregate_only",
        "doc_refs": ["SR 11-7", "Federal Reserve CCAR", "ECB ST guidelines"],
        "pillars": {
            "p1_development": "Document severity mapping (PD multipliers, "
                              "correlation shocks) for each scenario.",
            "p2_validation": "Independent replication; challenger scenario sets; "
                             "sensitivity of capital impact.",
            "p3_governance": "Senior-management sign-off of scenario design.",
        },
    },
    "cecl": {
        "display": "CECL / IFRS 9 Expected Credit Loss",
        "tier_default": 2,
        "models": ["CECL lifetime loss model", "IFRS 9 staging model"],
        "generators": ["loan_portfolio"],
        "validation": ["tstr", "fidelity", "drift", "sensitivity"],
        "metrics": ["expected_loss", "brier", "roc_auc", "stage_transitions"],
        "controls": ["tier", "approval"],
        "synthetic_data": "mandatory_tstr",
        "privacy": "dp_or_equivalent",
        "doc_refs": ["SR 11-7", "ASC 326 (CECL)", "IFRS 9"],
        "pillars": {
            "p1_development": "Document lifetime-loss estimation approach and "
                              "economic-scenario dependence.",
            "p2_validation": "TSTR + vintage analysis against real defaults; "
                             "macro-scenario sensitivity.",
            "p3_governance": "Materiality-based tiering; disclosure-ready evidence.",
        },
    },
    "fraud_aml": {
        "display": "Fraud / AML detection",
        "tier_default": 2,
        "models": ["Transaction fraud model", "AML suspicious activity scoring"],
        "generators": ["transaction_stream"],
        "validation": ["tstr", "fidelity", "challenger", "drift"],
        "metrics": ["pr_auc", "roc_auc", "precision", "recall"],
        "controls": ["tier", "approval", "effective_challenge"],
        "synthetic_data": "benchmark_generation",
        "privacy": "pii_masking_mandatory",
        "doc_refs": ["SR 11-7", "FinCEN AML guidance", "ECB TRIM"],
        "pillars": {
            "p1_development": "Document fraud-injection assumptions and label "
                              "construction on synthetic transaction streams.",
            "p2_validation": "TSTR + challenger detection models; drift on "
                             "evolving fraud patterns.",
            "p3_governance": "Suspicious-activity evidence chain; model "
                             "retirement when pattern shifts.",
        },
    },
    "pricing": {
        "display": "Pricing / Treasury simulation",
        "tier_default": 3,
        "models": ["Behavioral pricing model", "Deposit/liability simulation"],
        "generators": ["transaction_stream", "loan_portfolio"],
        "validation": ["fidelity", "tstr", "sensitivity"],
        "metrics": ["brier", "accuracy", "auc"],
        "controls": ["tier"],
        "synthetic_data": "development_support",
        "privacy": "aggregate_only",
        "doc_refs": ["SR 11-7"],
        "pillars": {
            "p1_development": "Document behavioral assumptions and elasticity "
                              "parameters in the simulation.",
            "p2_validation": "Light-touch validation; sensitivity of pricing "
                             "outcomes to assumptions.",
            "p3_governance": "Tier-3 oversight with annual review.",
        },
    },
}


def list_profiles() -> list[dict]:
    out = []
    for key, p in BANKING_PROFILES.items():
        out.append({"category": key, "display": p["display"],
                    "tier_default": p["tier_default"], "models": p["models"],
                    "generators": p["generators"]})
    return out


def get_profile(category: str) -> dict:
    if category not in BANKING_PROFILES:
        raise ValueError(f"unknown profile '{category}' — available: "
                         f"{list(BANKING_PROFILES)}")
    p = BANKING_PROFILES[category]
    return {"category": category, **p}
