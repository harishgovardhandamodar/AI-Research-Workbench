# MRM — sample session transcript

> Registered as the workbench chat session **`mrm-sample-session`** by
> `examples/mrm/seed_session.py` (replays these turns + registers the figures
> as artifacts).

A realistic chat-style session in the Fox workbench against the `mrm`
simulation MCP server. Every turn maps to a tool call (`mrm__<tool>`) and the
figures/metrics quoted below are the exact outputs produced by
[`run_mrm_session.py`](run_mrm_session.py) (deterministic, fixed seeds). Full
tables and charts: [`reports/session_report.md`](reports/session_report.md).

Actors used for the RBAC/maker-checker demo:

| actor | role | line |
|-------|------|------|
| `alice.1l@example.bank` | developer | 1st line (maker) |
| `bob.2l@example.bank` | validator | 2nd line (checker) |
| `carol.3l@example.bank` | auditor | 3rd line (audit) |

---

## Turn 1 — user

> Inventory our banking models under MRM. Show the available profiles, then
> register one model per banking domain and flag which ones rely on synthetic
> data.

**assistant** — `mrm__health` · `mrm__list_profiles` · `mrm__register_model` × 6

- Server `mrm-simulation-mcp` v0.1.0, **39 tools** registered.
- Registered six models across all six profiles (credit_risk, market_risk,
  stress_testing, cecl, fraud_aml, pricing); all flagged `synthetic_used`,
  with documented limitations and generator assumptions mirrored onto each
  record.

## Turn 2 — user

> The retail PD model is material and synthetic-reliant — raise it to Tier 1.
> Also the legacy VaR engine is being replaced by the CCAR projection; retire
> it. Use maker-checker so 2nd line signs off.

**assistant** — `mrm__request_approval` · `mrm__pending_approvals` ·
`mrm__decide_approval` · `mrm__tier_model` · `mrm__update_model_status`

- Raised `tier` approval as `alice` (maker); `bob` (validator) approved →
  `mrm__tier_model` moved **Retail PD Model → Tier 1**; status advanced to
  `validation`.
- Retirement went through **two rounds**: the first request was *rejected*
  (missing remediation artifacts), the re-raise with remediation documented
  was approved and the **Treasury VaR Engine retired**.
- The approval funnel (approved / rejected / pending) is charted in
  `fig_approvals.png`.

## Turn 3 — user

> Generate the synthetic credit portfolio for the Tier-1 model and a fraud/AML
> transaction stream. Register lineage, attach a privacy budget, and document
> the generator assumptions.

**assistant** — `mrm__generate_synthetic_portfolio` ·
`mrm__generate_transaction_stream` · `mrm__register_dataset` ·
`mrm__apply_privacy_budget` · `mrm__extract_generator_assumptions`

- `loan_portfolio_n2000_seed42` (2,000 loans, one-factor Vasicek, ρ=0.12) and
  `transaction_stream_n10000_seed42` — both deterministic, versioned CSVs, each
  auto-registering its dataset + simulation lineage.
- A **simulated stand-in for the real book** (`seed43`) and a drifted
  current-quarter book (`seed99`, PD ×2.0, ρ=0.30) were registered as `real`
  references, so fidelity / drift / TSTR have an honest baseline.
- DP budget ε=1.0 (sequential) attached to the synthetic portfolio;
  `reidentification_risk=low`.
- `fig_portfolio.png` shows rating mix, PD/LGD/EAD profiles; `fig_transactions.png`
  shows fraud rate by merchant risk tier and the risk-score separation.

## Turn 4 — user

> Run the full simulation suite on the Tier-1 portfolio: Monte Carlo, all five
> scenarios, a stress test, PD sensitivity, and compare engine v1 vs v2.

**assistant** — `mrm__run_monte_carlo` · `mrm__run_scenario_set` ·
`mrm__stress_test_portfolio` · `mrm__sensitivity_analysis` ·
`mrm__compare_simulation_versions`

- **Monte Carlo** (3,000 paths, seed 42): EL $23.96M · VaR(99) $59.77M ·
  ES(97.5) $60.23M · default rate 12.53% (`fig_monte_carlo.png`).
- **All five scenarios** (`fig_scenarios.png`), ascending EL: upside $14.1M →
  baseline $23.9M → mild $41.7M → severe $71.2M → systemic $89.9M.
- **Stress PD ×3.2:** EL 2.99×, VaR(99) +124.9% — capital-buffer impact needs
  model-level review (`fig_stress.png`).
- **Sensitivity:** EL/VaR monotonic in the PD multiplier (`fig_sensitivity.png`);
  engine v1 vs v2 shows **no material difference** → safe to re-baseline.

## Turn 5 — user

> Validate before deployment: fidelity gates, then mandatory TSTR against
> hold-out REAL data, then drift monitoring and an independent challenger.

**assistant** — `mrm__evaluate_fidelity` · `mrm__tstr_evaluate` × 2 ·
`mrm__detect_drift` · `mrm__run_challenger`

- **Fidelity gates → FAIL** on correlation structure (distance 0.43 > 0.15);
  KS and business rules all pass. The gate caught that the two independently
  drawn books are structurally different; remediation ("improve correlation
  structure (copula fit / sampling)") recorded. TSTR is the deployment gate
  (`fig_fidelity.png`).
- **TSTR (credit)** — trained on 2,000 synthetic rows, evaluated on **600
  hold-out REAL rows**: ROC-AUC **0.7342** · KS 0.3945 · F1 0.044
  (`fig_tstr.png`). The model's `tstr_completed` flag is now set.
- **TSTR (fraud/AML)** on the transaction stream: compliant.
- **Drift → DRIFT DETECTED** (`pd` shifted; `expected_loss` warning) —
  the current-quarter book has moved; monitoring / redevelopment is triggered
  (`fig_drift.png`).
- **Challenger** on the credit book: logistic 0.7311 vs random-forest 0.7181
  (gain −0.0130) → *no material improvement, baseline adequate*
  (`fig_challenger.png`).

## Turn 6 — user

> Close out governance: cross-file consistency, log effective challenges,
> compile the validation report, attach evidence, and give me the audit trail.

**assistant** — `mrm__check_cross_file_consistency` ·
`mrm__log_effective_challenge` × 2 · `mrm__generate_validation_report` ·
`mrm__attach_evidence` · `mrm__audit_log`

- **Cross-file consistency → REVIEW**: `default` diverges between the real
  reference and the drifted current book — consistent with the drift finding.
- **Effective challenges logged:** `[medium/open]` LGD sensitivity on
  distressed loans; `[high/open]` synthetic fraud labels understate novel
  attack patterns (flags the fraud model's report as challenged).
- **Validation report** compiled to `store/reports/<model>_validation.md`
  (Pillar 3) and attached as evidence; the real reference was attached as a
  data evidence item.
- **Audit trail:** 53 append-only events for every MCP call
  (`fig_audit.png`); the model-attributed trail covers
  register → approve → tier → TSTR → challenge → report end to end.

## Session summary

| scenario family | tool | outcome |
|-----------------|------|---------|
| Governance | register / approve / tier / retire | 6 models, Tier-1 credit, retired VaR engine |
| Generation | generate portfolio + tx stream | deterministic, lineage + ε budget |
| Simulation | MC / 5 scenarios / stress / sensitivity / compare | EL $23.96M · VaR $59.77M · systemic EL $89.9M |
| Validation | fidelity / TSTR / drift / challenger | corr gate FAIL, TSTR AUC 0.7342, drift on `pd` |
| Controls | consistency / challenge / report / audit | 2 challenges, validation report, 53 audit events |

Reproduce everything: `.venv/bin/python examples/mrm/run_mrm_session.py`
