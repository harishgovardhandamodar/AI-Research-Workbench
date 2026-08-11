"""Sample MRM session — a full Model Risk Management run for banking simulations.

Drives every tool on the ``mrm`` MCP server end-to-end across the four SR 11-7
pillars the server implements:

  Pillar 3 (governance)   inventory six models across all banking profiles,
                          maker-checker tiering + approval-gated retirement,
                          effective challenge, evidence, validation report.
  Pillar 1 (controlled
    synthetic generation) loan portfolio + transaction stream generators,
                          documented assumptions, privacy budget, lineage.
  Pillar 1/2 (simulation) Monte Carlo, ALL five scenarios (baseline / mild /
                          severe / systemic / upside), stress test, sensitivity,
                          engine version comparison.
  Pillar 2 (validation)   fidelity gates, mandatory TSTR on hold-out REAL data,
                          performance metrics, drift monitoring, challenger.

It also charts every scenario family (matplotlib/Agg) and compiles an audit-ready
``session_report.md`` + ``runs.json`` under ``examples/mrm/reports/``. The
SQLite store is isolated to ``examples/mrm/store`` via ``FOX_MRM_STORE`` so the
run is fully self-contained and repeatable (fixed seeds).

Run it standalone:

    .venv/bin/python examples/mrm/run_mrm_session.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXAMPLES = Path(__file__).resolve().parent
REPORTS = EXAMPLES / "reports"
FIG_DIR = REPORTS / "figures"

os.environ["FOX_MRM_STORE"] = str(EXAMPLES / "store")
shutil.rmtree(EXAMPLES / "store", ignore_errors=True)

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from mcp_servers.mrm import core, server  # noqa: E402

# Actors (roles drive the maker-checker / RBAC demo in the audit trail).
MAKER = ("alice.1l@example.bank", "developer")   # 1st line — proposes
CHECKER = ("bob.2l@example.bank", "validator")   # 2nd line — approves
AUDITOR = ("carol.3l@example.bank", "auditor")   # 3rd line — audits

SEED = 42
BLUE, TEAL, RED, AMBER, GRAY = "#4f8cff", "#35c4b6", "#e05b5b", "#d9a441", "#8a94a6"
plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.25})


def L(out: str) -> dict:
    return json.loads(out)


def _save(fig, name: str) -> Path:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = FIG_DIR / name
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out


def _hist_xy(bins: list | np.ndarray, counts: list | np.ndarray):
    """Normalise a histogram payload (edges+counts or centres+counts) to
    (x_centres, widths, counts)."""
    hb = np.asarray(bins, dtype=float)
    hc = np.asarray(counts, dtype=float)
    if len(hb) == len(hc) + 1:          # edges
        x = (hb[:-1] + hb[1:]) / 2.0
        w = np.diff(hb)
    else:                               # centres
        x = hb
        w = np.full_like(x, np.median(np.diff(hb)) if len(hb) > 1 else 1.0)
    return x, w, hc


# ------------------------------------------------------------------ Pillar 3 --

def session_governance() -> dict:
    """Inventory models across every profile + maker-checker lifecycle."""
    print("=" * 66)
    print("1) PILLAR 3 — GOVERNANCE & INVENTORY (maker-checker RBAC)")
    print("=" * 66)
    info = {"approvals": [], "audit": []}

    health = L(server.health())
    profiles = L(server.list_profiles())["profiles"]

    # --- register six models across all banking profiles ---------------------
    specs = [
        ("Retail PD Model", "credit_risk", 3, "development", "default"),
        ("Treasury VaR Engine", "market_risk", 2, "monitoring", "var"),
        ("CCAR Loss Projection", "stress_testing", 1, "validation", "stress"),
        ("CECL Lifetime Loss Model", "cecl", 2, "validation", "cecl"),
        ("Transaction Fraud Model", "fraud_aml", 2, "development", "fraud"),
        ("Retail Pricing Model", "pricing", 3, "proposed", "pricing"),
    ]
    models = []
    for name, cat, tier, status, tag in specs:
        m = L(server.register_model(
            name, cat, tier=tier, status=status, synthetic_used=True,
            description=f"{name} — {tag} workflow model",
            owner="1st-line credit risk", validator="2nd-line validation",
            use_limitations="Development / back-testing only. TSTR against "
                            "hold-out real data is mandatory before deployment.",
            assumptions=assumptions_for(tag),
            actor=MAKER[0], role=MAKER[1],
            purpose="Inventory banking models under MRM (Pillar 3)."))
        models.append(m["model_id"])
        info[f"model_{tag}"] = m["model_id"]

    # --- maker-checker: Tier 3 -> Tier 1 for the credit model ----------------
    mid = info["model_default"]
    apv = L(server.request_approval(
        mid, "tier", "Material retail exposure + synthetic-data reliance",
        requested_by=MAKER[0], requested_role=MAKER[1],
        actor=MAKER[0], role=MAKER[1],
        purpose="Raise Tier-1 designation for maker-checker review."))
    info["approvals"].append(apv["approval"])
    info["pending_before_decision"] = L(server.pending_approvals())["count"]
    decided = L(server.decide_approval(
        apv["approval"]["id"], "approve", decided_by=CHECKER[0],
        decided_role=CHECKER[1],
        actor=CHECKER[0], role=CHECKER[1],
        purpose="Approve Tier-1 designation after 2nd-line review."))
    info["approvals"].append(decided["approval"])
    tiered = L(server.tier_model(mid, 1, rationale="approved by 2nd line",
                                 actor=MAKER[0], role=MAKER[1]))
    info["tiered"] = tiered["tier"]
    # move to validation (free transition), then approval-gated -> monitoring
    L(server.update_model_status(mid, "validation", rationale="start validation",
                                 actor=MAKER[0], role=MAKER[1]))
    info["approvals"].append(L(server.request_approval(
        mid, "status", "Promote to monitoring after TSTR passed",
        requested_by=MAKER[0], requested_role=MAKER[1],
        actor=MAKER[0], role=MAKER[1],
        purpose="Status change requires maker-checker."))["approval"])

    # --- maker-checker retirement with a rejected round (market risk model) ---
    mkt = info["model_var"]
    rej = L(server.request_approval(
        mkt, "retire", "Engine superseded by CCAR loss projection",
        requested_by=MAKER[0], requested_role=MAKER[1],
        actor=MAKER[0], role=MAKER[1],
        purpose="Request retirement of the legacy VaR engine."))
    info["approvals"].append(rej["approval"])
    info["pending_after_request"] = L(server.pending_approvals())["count"]
    info["approvals"].append(L(server.decide_approval(
        rej["approval"]["id"], "reject",
        decided_by=CHECKER[0], decided_role=CHECKER[1],
        actor=CHECKER[0], role=CHECKER[1],
        purpose="Reject: remediation artifacts missing."))["approval"])
    info["approvals"].append(L(server.request_approval(
        mkt, "retire", "Engine superseded — remediation documented",
        requested_by=MAKER[0], requested_role=MAKER[1],
        actor=MAKER[0], role=MAKER[1],
        purpose="Re-raise retirement with remediation evidence."))["approval"])
    info["approvals"].append(L(server.decide_approval(
        info["approvals"][-1]["id"], "approve", decided_by=CHECKER[0],
        decided_role=CHECKER[1], actor=CHECKER[0], role=CHECKER[1],
        purpose="Retirement approved with remediation on record."))["approval"])
    retired = L(server.retire_model(mkt, rationale="approved retirement",
                                    actor=MAKER[0], role=MAKER[1]))
    info["retired"] = retired["status"]

    info["models"] = L(server.list_models())["models"]
    info["profiles"] = profiles
    info["health"] = health
    return info


def assumptions_for(tag: str) -> list[dict]:
    """Mirror the generator's documented assumptions onto the model record."""
    if tag in ("default", "stress"):
        return L(server.extract_generator_assumptions(
            "loan_portfolio", SEED, {"correlation": 0.12}))["assumptions"]
    if tag == "fraud":
        return L(server.extract_generator_assumptions(
            "transaction_stream", SEED))["assumptions"]
    return [{"aspect": "intended_use",
             "assumption": "Model is documented for governance demos."}]


# ------------------------------------------------------ Pillar 1: generation --

def session_generation() -> dict:
    """Controlled synthetic generation + lineage + privacy posture."""
    print("\n" + "=" * 66)
    print("2) PILLAR 1 — CONTROLLED SYNTHETIC GENERATION")
    print("=" * 66)
    gen = {}

    # main Tier-1 credit portfolio (n=2,000, deterministic)
    syn = L(server.generate_synthetic_portfolio(
        n_loans=2000, seed=SEED, correlation=0.12, pd_mult=1.0,
        actor=MAKER[0], role=MAKER[1],
        purpose="Generate Tier-1 credit portfolio for simulation."))
    # "real" reference (simulated stand-in for historical bank data) + drifted
    # current book, so fidelity / drift have an honest reference to compare to.
    real = L(server.generate_synthetic_portfolio(
        n_loans=2000, seed=SEED + 1, correlation=0.12, pd_mult=1.0,
        actor=MAKER[0], role=MAKER[1],
        purpose="Stand-in for real historical credit book (simulated)."))
    current = L(server.generate_synthetic_portfolio(
        n_loans=2000, seed=SEED + 57, correlation=0.30, pd_mult=2.0,
        actor=MAKER[0], role=MAKER[1],
        purpose="Current-quarter book with elevated PDs (drift candidate)."))
    tx = L(server.generate_transaction_stream(
        n_tx=10000, seed=SEED, n_customers=500, fraud_rate=0.008,
        actor=MAKER[0], role=MAKER[1],
        purpose="Generate fraud/AML transaction stream."))
    tx_real = L(server.generate_transaction_stream(
        n_tx=10000, seed=SEED + 1, n_customers=500, fraud_rate=0.008,
        actor=MAKER[0], role=MAKER[1],
        purpose="Stand-in real transaction stream (simulated)."))

    # lineage: real datasets registered explicitly (kind='real')
    real_ds = L(server.register_dataset(
        "real_credit_reference", path=real["output_file"], kind="real",
        source="simulated stand-in for real bank book", rows=real["rows"],
        actor=MAKER[0], role=MAKER[1],
        purpose="Register real reference for TSTR / drift."))
    tx_real_ds = L(server.register_dataset(
        "real_transaction_reference", path=tx_real["output_file"], kind="real",
        source="simulated stand-in for real tx history", rows=tx_real["rows"],
        actor=MAKER[0], role=MAKER[1],
        purpose="Register real reference for fraud TSTR."))

    # privacy budget on the synthetic portfolio
    privacy = L(server.apply_privacy_budget(
        syn["output_file"].split("/")[-1], epsilon=1.0,
        source="differential_privacy", path=syn["output_file"], rows=syn["rows"],
        actor=MAKER[0], role=MAKER[1],
        purpose="Attach DP epsilon budget to synthetic portfolio."))["privacy"]

    gen.update(syn=syn, real=real, current=current, tx=tx, tx_real=tx_real,
               privacy=privacy, real_ds=real_ds, tx_real_ds=tx_real_ds)
    gen["datasets"] = L(server.list_datasets())["datasets"]
    gen["sims"] = L(server.list_simulations())["simulations"]
    return gen


# ---------------------------------------------------- Pillar 1/2: simulation --

def session_simulation(portfolio_path: str) -> dict:
    """Monte Carlo, ALL five scenarios, stress, sensitivity, version compare."""
    print("\n" + "=" * 66)
    print("3) PILLAR 1/2 — SIMULATION (MC + all 5 scenarios + stress + "
          "sensitivity)")
    print("=" * 66)
    sim = {}

    sim["mc"] = L(server.run_monte_carlo(
        portfolio_path, n_paths=3000, seed=SEED, correlation=0.12, pd_mult=1.0))
    sim["mc_v2"] = L(server.run_monte_carlo(
        portfolio_path, n_paths=3000, seed=SEED, correlation=0.25, pd_mult=1.0))

    sim["scenarios"] = L(server.run_scenario_set(
        portfolio_path, scenarios=None, n_paths=1500, seed=SEED))
    sim["stress"] = L(server.stress_test_portfolio(
        portfolio_path, severity=3.2, n_paths=1500, seed=SEED))
    sim["sensitivity"] = L(server.sensitivity_analysis(
        portfolio_path, parameter="pd_mult",
        values=[0.5, 1.0, 2.0, 3.0, 4.5], n_paths=1500, seed=SEED))
    sim["compare"] = L(server.compare_simulation_versions(
        sim["mc"], sim["mc_v2"]))
    return sim


# --------------------------------------------------------- Pillar 2: validate --

def session_validation(gen: dict, model_id: str, fraud_model_id: str) -> dict:
    """Fidelity gates, mandatory TSTR, metrics, drift, challenger."""
    print("\n" + "=" * 66)
    print("4) PILLAR 2 — INDEPENDENT VALIDATION (fidelity / TSTR / drift / "
          "challenger)")
    print("=" * 66)
    val = {}

    val["fidelity"] = L(server.evaluate_fidelity(
        gen["real"]["output_file"], gen["syn"]["output_file"]))

    val["tstr"] = L(server.tstr_evaluate(
        gen["syn"]["output_file"], gen["real"]["output_file"], target="default",
        seed=SEED, test_size=0.3, positive=1.0, model_id=model_id,
        actor=CHECKER[0], role=CHECKER[1],
        purpose="Mandatory Train-Synthetic-Test-Real on hold-out real data."))

    val["tstr_fraud"] = L(server.tstr_evaluate(
        gen["tx"]["output_file"], gen["tx_real"]["output_file"],
        target="fraud_flag", seed=SEED, test_size=0.3, positive=1.0,
        model_id=fraud_model_id, actor=CHECKER[0], role=CHECKER[1],
        purpose="Mandatory TSTR for the fraud detection model."))

    val["drift"] = L(server.detect_drift(
        gen["real"]["output_file"], gen["current"]["output_file"]))

    val["challenger"] = L(server.run_challenger(
        gen["real"]["output_file"], target="default",
        baseline="logistic", challenger="random_forest", seed=SEED))

    # ROC curve for the credit TSTR (mirrors validation.tstr_evaluate exactly:
    # same features, split seed, pipeline) so the chart matches the tool output.
    val["roc"] = _tstr_roc(gen["syn"]["output_file"], gen["real"]["output_file"])
    return val


def _tstr_roc(synthetic_path: str, real_path: str) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, roc_curve
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    syn = pd.read_csv(synthetic_path)
    real = pd.read_csv(real_path)
    feats = [c for c in syn.columns if c != "default"
             and syn[c].dtype.kind in "fiu" and c in real.columns
             and real[c].dtype.kind in "fiu"]
    X, y = syn[feats].fillna(0.0), (syn["default"] == 1.0).astype(int)
    Xr, yr = real[feats].fillna(0.0), (real["default"] == 1.0).astype(int)
    _, Xr_test, _, yr_test = train_test_split(Xr, yr, test_size=0.3,
                                              random_state=SEED)
    model = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=1000, random_state=SEED))
    model.fit(X, y)
    proba = model.predict_proba(Xr_test)[:, 1]
    fpr, tpr, _ = roc_curve(yr_test, proba)
    return {"fpr": fpr, "tpr": tpr,
            "auc": round(float(roc_auc_score(yr_test, proba)), 4),
            "feats": feats}


# --------------------------------------------------- documentation & controls --

def session_documentation(gen: dict, model_id: str, fraud_model_id: str,
                          sim: dict) -> dict:
    """Cross-file consistency, effective challenge, reports, evidence, audit."""
    print("\n" + "=" * 66)
    print("5) DOCUMENTATION & CONTROLS (report / challenge / evidence / audit)")
    print("=" * 66)
    doc = {}

    doc["consistency"] = L(server.check_cross_file_consistency([
        gen["real"]["output_file"], gen["syn"]["output_file"],
        gen["current"]["output_file"]]))

    doc["challenge"] = L(server.log_effective_challenge(
        model_id, "LGD sensitivity on distressed loans needs re-estimation",
        severity="medium", disposition="open", logged_by=CHECKER[0],
        actor=CHECKER[0], role=CHECKER[1],
        purpose="Log medium effective challenge on credit model."))
    doc["challenge_high"] = L(server.log_effective_challenge(
        fraud_model_id,
        "Synthetic fraud labels understate novel attack patterns",
        severity="high", disposition="open", logged_by=CHECKER[0],
        actor=CHECKER[0], role=CHECKER[1],
        purpose="High-severity finding flags the fraud model for review."))

    doc["report"] = L(server.generate_validation_report(
        model_id, profile="credit_risk",
        validation_data={"monte_carlo": sim["mc"]}, author=CHECKER[0],
        actor=CHECKER[0], role=CHECKER[1],
        purpose="Compile audit-ready validation report (Pillar 3)."))

    doc["evidence"] = L(server.attach_evidence(
        model_id, kind="data", description="Real credit reference (simulated)",
        path=gen["real"]["output_file"], actor=CHECKER[0], role=CHECKER[1],
        purpose="Attach the validation dataset as evidence."))

    doc["challenges"] = L(server.list_challenges())["challenges"]
    doc["evidence_list"] = L(server.list_evidence(model_id))["evidence"]
    doc["reports"] = L(server.list_validation_reports(model_id))["validation_reports"]

    doc["audit_all"] = L(server.audit_log(limit=500, role=AUDITOR[1],
                                          purpose="Post-run audit sample."))
    doc["audit_model"] = L(server.audit_log(limit=500, role=AUDITOR[1],
                                            model_id=model_id))
    return doc


# ---------------------------------------------------------------- figures ----

def render_figures(info: dict, gen: dict, sim: dict, val: dict,
                   doc: dict) -> dict:
    """One chart per scenario family — saved under reports/figures/."""
    print("\n6) VISUALIZATION — rendering figures")
    figs = {}

    # --- inventory: models by category, coloured by tier --------------------
    models = info["models"]
    cats = sorted({m["category"] for m in models})
    fig, ax = plt.subplots(figsize=(8, 3.4))
    bottom = np.zeros(len(cats))
    for tier, color in ((1, RED), (2, AMBER), (3, BLUE)):
        vals = [sum(1 for m in models if m["category"] == c and m["tier"] == tier)
                for c in cats]
        ax.bar(cats, vals, bottom=bottom, label=f"Tier {tier}", color=color)
        bottom += vals
    ax.set_ylabel("models"); ax.set_title("Model inventory by category & tier")
    ax.legend(ncol=3, fontsize=8)
    figs["inventory"] = _save(fig, "fig_inventory.png")

    # --- maker-checker approval funnel --------------------------------------
    apvs = info["approvals"]
    actions = sorted({a["action"] for a in apvs})
    statuses = ["approved", "rejected", "pending"]
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    bottom = np.zeros(len(actions))
    for st, color in zip(statuses, (TEAL, RED, GRAY)):
        vals = [sum(1 for a in apvs if a["action"] == ac and a["status"] == st)
                for ac in actions]
        ax.bar(actions, vals, bottom=bottom, label=st, color=color)
        bottom += vals
    ax.set_ylabel("requests")
    ax.set_title("Maker-checker approvals (tier / status / retire)")
    ax.legend(fontsize=8)
    figs["approvals"] = _save(fig, "fig_approvals.png")

    # --- synthetic loan portfolio composition --------------------------------
    syn_df = pd.read_csv(gen["syn"]["output_file"])
    fig, axes = plt.subplots(2, 2, figsize=(10, 6.6))
    order = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC", "C"]
    counts = syn_df["rating"].value_counts().reindex(order).fillna(0)
    axes[0][0].bar(counts.index, counts.values, color=BLUE)
    axes[0][0].set_title("Rating mix"); axes[0][0].set_ylabel("loans")
    axes[0][1].hist(syn_df["pd"], bins=40, color=BLUE)
    axes[0][1].set_title("PD distribution"); axes[0][1].set_xlabel("PD")
    axes[1][0].scatter(syn_df["pd"], syn_df["lgd"], s=3, alpha=0.25, color=TEAL)
    axes[1][0].set_title("LGD vs PD"); axes[1][0].set_xlabel("PD")
    axes[1][0].set_ylabel("LGD")
    axes[1][1].hist(syn_df["ead"], bins=40, color=AMBER)
    axes[1][1].set_title("Exposure (EAD)"); axes[1][1].set_xlabel("USD")
    fig.suptitle("Synthetic loan portfolio — generated, seed=42")
    fig.tight_layout()
    figs["portfolio"] = _save(fig, "fig_portfolio.png")

    # --- transaction stream fraud signal -------------------------------------
    tx = pd.read_csv(gen["tx"]["output_file"])
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4))
    rate = tx.groupby("merchant_risk_tier")["fraud_flag"].mean()
    axes[0].bar([str(t) for t in rate.index], rate.values, color=BLUE)
    axes[0].set_title("Fraud rate by merchant risk tier")
    axes[0].set_ylabel("fraud rate")
    axes[1].hist(tx.loc[tx["fraud_flag"] == 0, "risk_score"], bins=30,
                 alpha=0.6, color=BLUE, label="not fraud")
    axes[1].hist(tx.loc[tx["fraud_flag"] == 1, "risk_score"], bins=30,
                 alpha=0.6, color=RED, label="fraud")
    axes[1].set_title("Risk score by fraud label"); axes[1].legend(fontsize=8)
    fig.tight_layout()
    figs["transactions"] = _save(fig, "fig_transactions.png")

    # --- Monte Carlo loss distribution ----------------------------------------
    mc = sim["mc"]
    x, w, hc = _hist_xy(mc["loss_histogram"]["bins"],
                        mc["loss_histogram"]["counts"])
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.bar(x, hc, width=w, color=BLUE, alpha=0.75, edgecolor="white")
    for x, c, lab in ((mc["expected_loss"], TEAL, "EL"),
                      (mc["var_99"], AMBER, "VaR 99"),
                      (mc["es_97_5"], RED, "ES 97.5")):
        ax.axvline(x, color=c, ls="--", lw=1.2)
        ax.text(x, max(hc) * 0.95, f" {lab}", color=c, fontsize=8)
    ax.set_title(f"Monte Carlo loss distribution (n={mc['n_paths']:,} paths, "
                 f"seed={mc['seed']})")
    ax.set_xlabel("portfolio loss (USD)"); ax.set_ylabel("paths")
    figs["monte_carlo"] = _save(fig, "fig_monte_carlo.png")

    # --- scenario suite (all five scenarios) -----------------------------------
    sc = sim["scenarios"]["scenarios"]
    names = [s["scenario"] for s in sc]
    el = [s["expected_loss"] for s in sc]
    var = [s["var_99"] for s in sc]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    bars = ax.bar(names, el, color=BLUE, alpha=0.8, label="Expected loss")
    ax.scatter(names, var, color=AMBER, zorder=3, label="VaR 99")
    for b, v in zip(bars, var):
        ax.annotate(f"{v/1e6:.1f}M", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 4),
                    ha="center", fontsize=7, color=AMBER)
    ax.set_title("Scenario suite — expected loss & VaR 99 (ascending stress)")
    ax.set_ylabel("USD"); ax.legend(fontsize=8)
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    figs["scenarios"] = _save(fig, "fig_scenarios.png")

    # --- stress: baseline vs stressed loss distributions -----------------------
    base, st = sim["stress"]["baseline"], sim["stress"]["stressed"]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for res, c, lab in ((base, BLUE, "Baseline"), (st, RED, f"Stressed "
                                                   f"(PD x{sim['stress']['severity']:.1f})")):
        x, _, hc = _hist_xy(res["loss_histogram"]["bins"],
                            res["loss_histogram"]["counts"])
        ax.step(x, hc / max(hc), where="post", color=c, lw=1.6, label=lab)
        ax.axvline(res["var_99"], color=c, ls=":", lw=1.2)
    ax.set_title("Stress test — loss distribution shift (VaR 99 marked)")
    ax.set_xlabel("portfolio loss (USD)"); ax.set_ylabel("normalised density")
    ax.legend(fontsize=8)
    figs["stress"] = _save(fig, "fig_stress.png")

    # --- sensitivity to PD multiplier -------------------------------------------
    pts = sim["sensitivity"]["points"]
    xs = [p["value"] for p in pts]
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    ax.plot(xs, [p["expected_loss"] for p in pts], "-o", color=BLUE,
            label="Expected loss")
    ax.plot(xs, [p["var_99"] for p in pts], "-o", color=AMBER, label="VaR 99")
    ax.set_title("Sensitivity — one-at-a-time PD multiplier")
    ax.set_xlabel("PD multiplier"); ax.set_ylabel("USD")
    ax.legend(fontsize=8)
    figs["sensitivity"] = _save(fig, "fig_sensitivity.png")

    # --- fidelity: real vs synthetic distributions ------------------------------
    real_df = pd.read_csv(gen["real"]["output_file"])
    fid = val["fidelity"]
    ks_pd = next((r["d_stat"] for r in fid["gates"]["distributional_match"]
                  ["ks_tests"] if r["column"] == "pd"), None)
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.5))
    for c in ("pd", "credit_score"):
        ax = axes[0] if c == "pd" else axes[1]
        ax.hist(real_df[c], bins=30, alpha=0.6, color=BLUE, label="real")
        ax.hist(syn_df[c], bins=30, alpha=0.6, color=TEAL, label="synthetic")
        ax.set_title(c)
    axes[0].legend(fontsize=8)
    axes[0].set_title(f"PD (KS D={ks_pd})")
    axes[1].set_title("credit score")
    fig.suptitle(f"Fidelity gates — verdict {fid['verdict']} "
                 f"(corr dist "
                 f"{fid['gates']['correlation_structure'].get('frobenius')})")
    fig.tight_layout()
    figs["fidelity"] = _save(fig, "fig_fidelity.png")

    # --- TSTR ROC curve ----------------------------------------------------------
    roc = val["roc"]
    fig, ax = plt.subplots(figsize=(6.4, 4.6))
    ax.plot(roc["fpr"], roc["tpr"], color=BLUE, lw=2,
            label=f"TSTR logistic (AUC={roc['auc']:.3f})")
    ax.plot([0, 1], [0, 1], color=GRAY, ls="--", lw=1, label="random")
    ax.set_title("Mandatory Train-Synthetic-Test-Real — ROC on hold-out REAL")
    ax.set_xlabel("false positive rate"); ax.set_ylabel("true positive rate")
    ax.legend(fontsize=8)
    figs["tstr"] = _save(fig, "fig_tstr.png")

    # --- drift PSI / TV by column -------------------------------------------------
    det = val["drift"]
    details = det["details"]
    labels = [d["column"] for d in details]
    values = [d.get("psi", d.get("total_variation", 0.0)) for d in details]
    colors = [{"ok": BLUE, "warn": AMBER, "shift": RED}[d["level"]] for d in details]
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.bar(labels, values, color=colors)
    ax.axhline(0.1, color=AMBER, ls="--", lw=1, label="warn")
    ax.axhline(0.25, color=RED, ls="--", lw=1, label="shift")
    ax.set_title(f"Drift monitoring — {det['verdict']} (PSI / total variation)")
    ax.set_ylabel("PSI / TV"); ax.set_xticklabels(labels, rotation=30,
                                                  ha="right")
    ax.legend(fontsize=8)
    figs["drift"] = _save(fig, "fig_drift.png")

    # --- challenger head-to-head ----------------------------------------------------
    ch = val["challenger"]
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    bars = ax.bar(["baseline", "challenger"],
                  [ch["baseline_metrics"]["roc_auc"],
                   ch["challenger_metrics"]["roc_auc"]],
                  color=[BLUE, TEAL])
    ax.set_ylim(0, 1)
    ax.set_ylabel("ROC-AUC"); ax.set_title(
        f"Challenger {ch['challenger']} vs baseline {ch['baseline']} "
        f"(gain {ch['auc_gain']:+.4f})")
    for b in bars:
        ax.annotate(f"{b.get_height():.4f}",
                    (b.get_x() + b.get_width() / 2, b.get_height()),
                    textcoords="offset points", xytext=(0, 3), ha="center",
                    fontsize=8)
    ax.text(0.5, 0.12, ch["verdict"], transform=ax.transAxes, fontsize=8,
            color=GRAY, ha="center")
    figs["challenger"] = _save(fig, "fig_challenger.png")

    # --- audit trail: events per tool ------------------------------------------------
    events = doc["audit_all"]["events"]
    per_tool = Counter(e["tool"] for e in events)
    tools = sorted(per_tool, key=per_tool.get)
    fig, ax = plt.subplots(figsize=(8, 4.6))
    ax.barh(tools, [per_tool[t] for t in tools], color=BLUE)
    ax.set_title(f"Audit trail — {len(events)} tool events "
                 f"(append-only, INSERT-only)")
    ax.set_xlabel("events"); ax.invert_yaxis()
    figs["audit"] = _save(fig, "fig_audit.png")

    return figs


# ------------------------------------------------------------------ report ----

def write_report(info: dict, gen: dict, sim: dict, val: dict, doc: dict,
                 figs: dict) -> Path:
    models = L(server.list_models())["models"]  # post-run snapshot (TSTR flags)
    mid = info["model_default"]
    mc = sim["mc"]
    fid = val["fidelity"]
    tstr = val["tstr"]
    drift = val["drift"]
    ch = val["challenger"]

    lines = [
        "# MRM Sample Session — audit-ready report",
        "",
        "A deterministic end-to-end Model Risk Management run for banking data "
        "simulations (SR 11-7 / 2026 interagency-aligned), covering all "
        "scenarios with one chart per scenario family. Run with "
        "`.venv/bin/python examples/mrm/run_mrm_session.py`.",
        "",
        f"- **Server:** {info['health']['server']} v{info['health']['version']} "
        f"· **tools registered:** {info['health']['tools']}",
        f"- **Models inventoried:** {len(models)} across "
        f"{len(info['profiles'])} banking profiles",
        f"- **Store (isolated):** `examples/mrm/store` · "
        f"**seeds:** all fixed (42/43/99)",
        "",
    ]

    lines += ["## 1. Pillar 3 — governance & inventory", ""]
    lines.append("| name | category | tier | status | synth | tstr |")
    lines.append("|------|----------|------|--------|-------|------|")
    for m in models:
        lines.append(f"| {m['name']} | {m['category']} | {m['tier']} | "
                     f"{m['status']} | {m['synthetic_used']} | "
                     f"{m['tstr_completed']} |")
    lines += [
        "",
        f"Maker-checker: credit model raised to **Tier {info['tiered']}** "
        f"(request → validator approve); market-risk engine **{info['retired']}** "
        f"after a rejected + re-approved retirement round. "
        f"{len(doc['audit_all']['events'])} audit events recorded.",
        "",
        f"![Model inventory](figures/{figs['inventory'].name})",
        f"![Approvals](figures/{figs['approvals'].name})",
        "",
    ]

    lines += ["## 2. Pillar 1 — controlled synthetic generation", ""]
    ds = gen["datasets"]
    lines.append("| dataset | kind | source | rows | ε budget |")
    lines.append("|---------|------|--------|------|----------|")
    for d in ds:
        lines.append(f"| {d['name']} | {d['kind']} | {d['source']} | {d['rows']} "
                     f"| {d['privacy_epsilon'] or '—'} |")
    lines += [
        "",
        f"Privacy posture: `{gen['privacy']['method']}` with "
        f"ε={gen['privacy']['epsilon_budget']} (sequential composition, "
        f"re-identification risk `{gen['privacy']['reidentification_risk']}`).",
        "",
        f"![Loan portfolio](figures/{figs['portfolio'].name})",
        f"![Transactions](figures/{figs['transactions'].name})",
        "",
    ]

    lines += ["## 3. Pillar 1/2 — simulation (all five scenarios)", ""]
    lines.append(f"**Monte Carlo (seed {mc['seed']}, {mc['n_paths']:,} paths, "
                 f"rho=0.12):** EL ${mc['expected_loss']:,.0f} · "
                 f"VaR(99) ${mc['var_99']:,.0f} · ES(97.5) ${mc['es_97_5']:,.0f} "
                 f"· default rate {mc['mean_default_rate']:.2%}")
    lines += ["", "| scenario | label | EL | VaR 99 | default rate |",
              "|----------|-------|----|--------|--------------|"]
    for s in sim["scenarios"]["scenarios"]:
        lines.append(f"| {s['scenario']} | {s['label']} | "
                     f"${s['expected_loss']:,.0f} | ${s['var_99']:,.0f} | "
                     f"{s['mean_default_rate']:.2%} |")
    st = sim["stress"]["impact"]
    lines += [
        "",
        f"**Stress (PD x{sim['stress']['severity']}):** EL multiple "
        f"{st['expected_loss_multiple']}x, VaR(99) delta ${st['var99_delta']:,.0f} "
        f"({st['var99_delta_pct']:.1%}) — {st['read_out']}",
        f"**Sensitivity:** monotonic in PD = "
        f"{sim['sensitivity']['monotonic_in_pd']} · "
        f"**Engine v1 vs v2:** material difference = "
        f"{sim['compare']['material_difference']} "
        f"({sim['compare']['note']})",
        "",
        f"![Monte Carlo](figures/{figs['monte_carlo'].name})",
        f"![Scenario suite](figures/{figs['scenarios'].name})",
        f"![Stress test](figures/{figs['stress'].name})",
        f"![Sensitivity](figures/{figs['sensitivity'].name})",
        "",
    ]

    lines += ["## 4. Pillar 2 — independent validation", ""]
    ks = fid["gates"]["distributional_match"]
    lines.append(
        f"**Fidelity gates:** verdict **{fid['verdict']}** — KS fail count "
        f"{fid['gates']['distributional_match']['fail_count']}, correlation "
        f"distance {fid['gates']['correlation_structure'].get('frobenius')}, "
        f"business-rule failures "
        f"{sum(1 for r in fid['gates']['business_rules'] if not r['passed'])}, "
        f"remediation: {fid['remediation'] or 'none'}. "
        f"The gate correctly flags the two independently drawn books as "
        f"structurally different; remediation is recorded and the mandatory "
        f"TSTR below is the deployment gate.")
    lines.append("")
    lines.append("| column | real mean | synth mean | KS D | KS p |")
    lines.append("|--------|-----------|------------|------|------|")
    for r in ks["ks_tests"]:
        lines.append(f"| {r['column']} | {r['real_mean']} | "
                     f"{r['synth_mean']} | {r['d_stat']} | {r['p_value']} |")
    t = tstr["metrics"]
    lines += [
        "",
        f"**Mandatory TSTR ({tstr['protocol']}):** trained on "
        f"{tstr['synthetic_train_rows']:,} synthetic rows, evaluated on "
        f"{tstr['real_eval_rows']:,} hold-out REAL rows. "
        f"ROC-AUC {t['roc_auc']} · PR-AUC {t['pr_auc']} · KS {t['ks']} · "
        f"F1 {t['f1']}. {tstr['statement']}",
        "",
        f"**Drift monitoring:** {drift['verdict']} — shifted columns "
        f"{drift['shifted_columns'] or 'none'}, warnings "
        f"{drift['warning_columns'] or 'none'}. {drift['action']}",
        f"**Challenger:** {ch['challenger']} AUC {ch['challenger_metrics']['roc_auc']} "
        f"vs {ch['baseline']} {ch['baseline_metrics']['roc_auc']} (gain "
        f"{ch['auc_gain']:+.4f}) on the Tier-1 credit book (real reference) — "
        f"{ch['verdict']}",
        "",
        f"![Fidelity](figures/{figs['fidelity'].name})",
        f"![TSTR ROC](figures/{figs['tstr'].name})",
        f"![Drift](figures/{figs['drift'].name})",
        f"![Challenger](figures/{figs['challenger'].name})",
        "",
    ]

    lines += ["## 5. Documentation & controls", ""]
    lines.append(f"**Cross-file consistency:** {doc['consistency']['verdict']} "
                 f"across {len(doc['consistency']['files'])} files "
                 f"({doc['consistency']['files']}).")
    lines += ["", "**Effective challenges:**", ""]
    for c in doc["challenges"]:
        lines.append(f"- `[{c['severity']}/{c['disposition']}]` {c['finding']} "
                     f"(model `{c['model_id']}`, logged by {c['logged_by']})")
    lines += [
        "",
        f"**Validation report:** `{doc['report']['report_path']}` "
        f"(evidence `{doc['report']['evidence_id']}`) — model `{mid}` marked "
        f"TSTR-completed.",
        f"**Evidence on model:** {len(doc['evidence_list'])} items · "
        f"**Validation reports:** {len(doc['reports'])}",
        "",
        "## 6. Appendices", "",
        f"![Audit trail](figures/{figs['audit'].name})",
        "",
        "Full conversation-style narrative: see "
        "[`session_transcript.md`](../session_transcript.md).",
        "",
    ]

    out = REPORTS / "session_report.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_runs_json(info: dict, sim: dict, val: dict) -> Path:
    runs = {
        "models": len(info["models"]),
        "approvals": {"pending": sum(1 for a in info["approvals"]
                                     if a["status"] == "pending"),
                      "approved": sum(1 for a in info["approvals"]
                                      if a["status"] == "approved"),
                      "rejected": sum(1 for a in info["approvals"]
                                      if a["status"] == "rejected")},
        "monte_carlo": {k: sim["mc"][k] for k in
                        ("expected_loss", "var_99", "es_97_5",
                         "mean_default_rate")},
        "scenarios": {s["scenario"]: {"expected_loss": s["expected_loss"],
                                      "var_99": s["var_99"]}
                      for s in sim["scenarios"]["scenarios"]},
        "stress": sim["stress"]["impact"],
        "sensitivity_monotonic_in_pd": sim["sensitivity"]["monotonic_in_pd"],
        "engine_versions_material_difference":
            sim["compare"]["material_difference"],
        "fidelity": {"verdict": val["fidelity"]["verdict"]},
        "tstr": {k: v for k, v in val["tstr"]["metrics"].items()
                 if k in ("roc_auc", "pr_auc", "ks", "f1")},
        "drift": {"verdict": val["drift"]["verdict"],
                  "shifted": val["drift"]["shifted_columns"]},
        "challenger": {"baseline": val["challenger"]["baseline_metrics"]["roc_auc"],
                       "challenger": val["challenger"]["challenger_metrics"]["roc_auc"],
                       "gain": val["challenger"]["auc_gain"],
                       "wins": val["challenger"]["challenger_wins"]},
    }
    out = REPORTS / "runs.json"
    out.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    return out


def main():
    print("MRM sample session — deterministic run (fixed seeds)\n")

    info = session_governance()
    gen = session_generation()
    sim = session_simulation(gen["syn"]["output_file"])
    val = session_validation(gen, info["model_default"],
                             info["model_fraud"])
    doc = session_documentation(gen, info["model_default"],
                                info["model_fraud"], sim)
    figs = render_figures(info, gen, sim, val, doc)

    report = write_report(info, gen, sim, val, doc, figs)
    runs = write_runs_json(info, sim, val)

    print("\n" + "=" * 66)
    print(f"Done. Report:  {report}")
    print(f"Runs:         {runs}")
    print(f"Figures:      {FIG_DIR}")
    print(f"Store:        {EXAMPLES / 'store'}")


if __name__ == "__main__":
    main()
