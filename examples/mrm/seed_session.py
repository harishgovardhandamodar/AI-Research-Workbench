"""Seed the MRM sample run as a named workbench session (chat project).

A workbench "session" is a project directory under ``PROJECTS_DIR`` with a
``workbench.db`` (messages + artifacts). This script recreates the
``mrm-sample-session`` project, replays the sample conversation from
``session_transcript.md`` as chat messages, and registers the generated
figures, the audit-ready report and ``runs.json`` as artifacts so the session
appears in the chat UI with charts embedded.

Run AFTER ``run_mrm_session.py`` (needs ``reports/`` populated):

    .venv/bin/python examples/mrm/seed_session.py

Idempotent: an existing ``mrm-sample-session`` project is replaced. Start the
server afterwards (or use the project picker) to open the session.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXAMPLES = Path(__file__).resolve().parent
REPORTS = EXAMPLES / "reports"
FIG_DIR = REPORTS / "figures"
SESSION_NAME = "mrm-sample-session"

from backend.artifacts.store import Artifact, ArtifactStore  # noqa: E402
from backend.paths import PROJECTS_DIR  # noqa: E402
from backend.store import ProjectStore, close_project_db  # noqa: E402


def _img(fig_name: str, description: str, artifact_store: ArtifactStore) -> str:
    """Register a figure PNG as an artifact and return its chat-inline link."""
    data = (FIG_DIR / f"{fig_name}.png").read_bytes()
    art = artifact_store.add_artifact(
        Artifact(kind="figure", name=fig_name, description=description,
                 data_type="png"),
        data=data, data_type="png")
    return f"![{fig_name}](/artifacts/{art.id})"


def main() -> None:
    if not (REPORTS / "session_report.md").exists():
        raise SystemExit(
            "reports/session_report.md not found — run run_mrm_session.py first.")

    proj = PROJECTS_DIR / SESSION_NAME
    close_project_db(proj)
    shutil.rmtree(proj, ignore_errors=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    store = ProjectStore(proj)
    arts = ArtifactStore(proj)

    figures = [
        ("fig_inventory", "Model inventory by category & tier"),
        ("fig_approvals", "Maker-checker approval funnel"),
        ("fig_portfolio", "Synthetic loan portfolio composition"),
        ("fig_transactions", "Fraud signal in the transaction stream"),
        ("fig_monte_carlo", "Monte Carlo loss distribution (EL / VaR / ES)"),
        ("fig_scenarios", "All five scenarios — EL & VaR 99"),
        ("fig_stress", "Stress test — baseline vs stressed"),
        ("fig_sensitivity", "PD-multiplier sensitivity"),
        ("fig_fidelity", "Fidelity gates — real vs synthetic"),
        ("fig_tstr", "TSTR ROC on hold-out REAL"),
        ("fig_drift", "Drift monitoring (PSI / total variation)"),
        ("fig_challenger", "Challenger head-to-head"),
        ("fig_audit", "Audit trail — events per tool"),
    ]

    turns = [
        ("user", "Inventory our banking models under MRM. Show the available "
                 "profiles, then register one model per banking domain and flag "
                 "which ones rely on synthetic data."),
        ("assistant", "Server `mrm-simulation-mcp` v0.1.0, **39 tools** "
                      "registered. Inventoried six models across all six banking "
                      "profiles (credit_risk, market_risk, stress_testing, cecl, "
                      "fraud_aml, pricing), all flagged `synthetic_used`, with "
                      "use-limitations and generator assumptions on record.\n\n"
                      f"{_img('fig_inventory', figures[0][1], arts)}"),
        ("user", "The retail PD model is material and synthetic-reliant — raise "
                 "it to Tier 1. Also retire the legacy VaR engine (replaced by "
                 "the CCAR projection). Use maker-checker so 2nd line signs off."),
        ("assistant", "Maker-checker completed: **Retail PD Model → Tier 1** "
                      "(request by 1st line, approved by validator) and status "
                      "advanced to `validation`. The VaR engine retirement went "
                      "through **two rounds** — first request **rejected** "
                      "(missing remediation artifacts), re-raise approved, then "
                      "**retired**.\n\n"
                      f"{_img('fig_approvals', figures[1][1], arts)}"),
        ("user", "Generate the synthetic credit portfolio for the Tier-1 model "
                 "and a fraud/AML transaction stream. Register lineage, attach "
                 "a privacy budget, and document the generator assumptions."),
        ("assistant", "Generated deterministic portfolios: "
                      "`loan_portfolio_n2000_seed42` (one-factor Vasicek, "
                      "ρ=0.12) + `transaction_stream_n10000_seed42`, each "
                      "auto-registering dataset + simulation lineage. A "
                      "simulated stand-in for the real book (seed43) and a "
                      "drifted current-quarter book (seed99, PD ×2.0) give "
                      "fidelity / drift / TSTR an honest reference. DP budget "
                      "ε=1.0 attached (re-identification risk `low`).\n\n"
                      f"{_img('fig_portfolio', figures[2][1], arts)}\n\n"
                      f"{_img('fig_transactions', figures[3][1], arts)}"),
        ("user", "Run the full simulation suite on the Tier-1 portfolio: Monte "
                 "Carlo, all five scenarios, a stress test, PD sensitivity, and "
                 "compare engine v1 vs v2."),
        ("assistant", "Simulation suite complete. MC (3,000 paths, seed 42): "
                      "EL $23.96M · VaR(99) $59.77M · ES(97.5) $60.23M · "
                      "default rate 12.5%. **All five scenarios** ascending: "
                      "upside $14.1M → baseline $23.9M → mild $41.7M → severe "
                      "$71.2M → systemic $89.9M. Stress PD ×3.2: EL 2.99×, "
                      "VaR +124.9% (capital-buffer review required). Sensitivity "
                      "monotonic in PD; engine v1 vs v2 shows no material "
                      "difference.\n\n"
                      f"{_img('fig_monte_carlo', figures[4][1], arts)}\n\n"
                      f"{_img('fig_scenarios', figures[5][1], arts)}\n\n"
                      f"{_img('fig_stress', figures[6][1], arts)}\n\n"
                      f"{_img('fig_sensitivity', figures[7][1], arts)}"),
        ("user", "Validate before deployment: fidelity gates, then mandatory "
                 "TSTR against hold-out REAL data, then drift monitoring and an "
                 "independent challenger."),
        ("assistant", "Validation done. Fidelity gates: **FAIL** on correlation "
                      "structure (0.43 > 0.15) — KS and business rules pass; "
                      "remediation recorded, and **mandatory TSTR is the "
                      "deployment gate**. TSTR (credit): trained on 2,000 "
                      "synthetic rows, evaluated on 600 hold-out REAL rows — "
                      "ROC-AUC **0.7342**, KS 0.3945, `tstr_completed` set. "
                      "Fraud TSTR compliant. Drift: **DRIFT DETECTED** on `pd`. "
                      "Challenger: logistic 0.7311 vs random-forest 0.7181 "
                      "(gain −0.013) — baseline adequate.\n\n"
                      f"{_img('fig_fidelity', figures[8][1], arts)}\n\n"
                      f"{_img('fig_tstr', figures[9][1], arts)}\n\n"
                      f"{_img('fig_drift', figures[10][1], arts)}\n\n"
                      f"{_img('fig_challenger', figures[11][1], arts)}"),
        ("user", "Close out governance: cross-file consistency, log effective "
                 "challenges, compile the validation report, attach evidence, "
                 "and give me the audit trail."),
        ("assistant", "Controls closed out. Cross-file consistency: **REVIEW** "
                      "(`default` diverges in the drifted current book — "
                      "consistent with the drift finding). Effective challenges "
                      "logged: `[medium/open]` LGD sensitivity on distressed "
                      "loans; `[high/open]` synthetic fraud labels understate "
                      "novel attack patterns (flags the fraud model's report). "
                      "Validation report compiled (Pillar 3) + evidence "
                      "attached. **Audit trail: 53 append-only events** covering "
                      "every MCP call.\n\n"
                      f"{_img('fig_audit', figures[12][1], arts)}"),
    ]

    for role, content in turns:
        store.add_message(role, content, {"tags": ["mrm", "sample-session"]})

    # attach the compiled report + runs.json as artifacts
    report_art = arts.add_artifact(
        Artifact(kind="text", name="session_report",
                 description="MRM sample session — audit-ready report",
                 data_type="text"),
        data=(REPORTS / "session_report.md").read_bytes(), data_type="text")
    runs_art = arts.add_artifact(
        Artifact(kind="data", name="runs", description="MRM sample session — "
                 "machine-readable metrics", data_type="text"),
        data=(REPORTS / "runs.json").read_bytes(), data_type="text")

    # final summary message linking the artifacts
    store.add_message("assistant", (
        "## Session summary\n\n"
        "| scenario family | tool | outcome |\n"
        "|-----------------|------|---------|\n"
        "| Governance | register / approve / tier / retire | 6 models, "
        "Tier-1 credit, retired VaR engine |\n"
        "| Generation | generate portfolio + tx stream | deterministic, "
        "lineage + ε budget |\n"
        "| Simulation | MC / 5 scenarios / stress / sensitivity / compare | "
        "EL $23.96M · VaR $59.77M · systemic EL $89.9M |\n"
        "| Validation | fidelity / TSTR / drift / challenger | corr gate "
        "FAIL, TSTR AUC 0.7342, drift on `pd` |\n"
        "| Controls | consistency / challenge / report / audit | 2 "
        "challenges, validation report, 53 audit events |\n\n"
        "Full audit-ready report: [session_report](/artifacts/{report_id}) · "
        "metrics: [runs.json](/artifacts/{runs_id})".format(
            report_id=report_art.id, runs_id=runs_art.id)),
        {"tags": ["mrm", "sample-session", "report", "summary"]})

    n_msgs = len(store.list_messages())
    n_arts = len(arts.list())
    print(f"Seeded session '{SESSION_NAME}' at {proj}")
    print(f"  messages: {n_msgs} · artifacts: {n_arts}")
    print("Start the server (or refresh the project picker) to open it.")


if __name__ == "__main__":
    main()
