# MRM Simulation MCP Server

Model Risk Management (MRM) for **banking data simulations**, aligned with
**SR 11-7** and the **2026 interagency risk-based updates** (tiering, lifecycle
lineage, effective challenge, proportionality). The server exposes the MRM
framework to any AI agent (coding, validation, challenger, documentation) over
the Model Context Protocol as `mrm__<tool>`.

It is fully local-first and deterministic: storage is a single SQLite file,
every tool call is written to an **append-only audit log**, and writable tools
are approval-gated by the MCP host.

## Tool map

| Category | Tools | Purpose |
|---|---|---|
| Inventory & Governance | `list_models`, `get_model_metadata`, `register_model`, `register_simulation`, `register_dataset`, `tier_model`, `update_model_status`, `retire_model` | Authoritative model/sim/dataset inventory with SR 11-7 lifecycle + tiering |
| Synthetic Data | `generate_synthetic_portfolio`, `generate_transaction_stream`, `apply_privacy_budget`, `evaluate_fidelity`, `extract_generator_assumptions` | Controlled, deterministic generation with fidelity + privacy gates |
| Simulation | `run_monte_carlo`, `run_scenario_set`, `stress_test_portfolio`, `sensitivity_analysis`, `compare_simulation_versions` | One-factor Vasicek loss simulation, macro scenarios, stress |
| Validation | `tstr_evaluate`, `compute_performance_metrics`, `detect_drift`, `run_challenger` | Mandatory TSTR, PSI drift, challenger head-to-head |
| Documentation & Evidence | `generate_validation_report`, `check_cross_file_consistency`, `attach_evidence` | Audit-ready Markdown reports + evidence chain |
| Controls | `request_approval`, `decide_approval`, `pending_approvals`, `log_effective_challenge`, `audit_log` | Maker-checker, effective challenge, immutable audit |
| Profiles | `list_profiles`, `get_profile` | Banking-domain profiles (credit, market, CECL, stress, fraud/AML, pricing) |

Plus `health`.

## The three pillars

1. **Robust development & use** — every generator ships documented assumptions
   (`extract_generator_assumptions`: statistical properties, correlation
   structure, tail behaviour, bias sources) and the generator itself is treated
   as a model under MRM.
2. **Effective validation** — fidelity gates (`evaluate_fidelity`) must pass
   before material use; **TSTR is mandatory** (`tstr_evaluate` trains on
   synthetic, evaluates on hold-out REAL data); drift (`detect_drift`) and
   challengers (`run_challenger`) are continuous.
3. **Governance** — centralized inventory, risk tiering, RACI (1st line owns /
   2nd line validates / 3rd line audits), **maker-checker approvals**
   (`request_approval` → `decide_approval`) for Tier-1 changes, and an
   append-only audit log of every MCP call.

## Security model

- **RBAC roles**: `developer` (1st line), `validator` (2nd line), `auditor` /
  `admin` (3rd line). Tier changes and retirement are approval-gated for
  non-validator roles; only `validator`/`auditor`/`admin` may decide approvals
  or read the audit log.
- **Maker-checker**: raising a tier/status/retirement change without a checker
  role raises `PermissionError`; the maker must first
  `request_approval` and a checker `decide_approval`.
- **Purpose binding**: writable tools accept `actor` / `role` / `purpose`;
  every event records who/what/when + a params hash.
- **Audit trail**: append-only (INSERT-only) `audit_log`; queried via
  `audit_log` by 2nd/3rd-line roles.
- **Approval gating at the host**: writable tools are not marked read-only, so
  the workbench's permission layer prompts the user before they run.

## Determinism & lineage

- All generators and simulations are **seed-controlled**; re-running the same
  seed reproduces the same dataset / loss distribution.
- Generated CSVs are content-addressed (sha256 in the tool reply) and
  registered with dataset + simulation IDs, giving full lineage from generator
  parameters → dataset → model → validation results.
- Storage root: `$FOX_MRM_STORE` (injected by the Fox host as
  `<projects>/.mrm`), default `~/.fox/mrm`. One `mrm.db` holds models,
  simulations, datasets, approvals, challenges, evidence, reports and the
  audit log.

## Deployment

Run standalone over stdio (any MCP client can connect):

```bash
.venv/bin/python -m mcp_servers.mrm.server
```

In the Fox workbench the server is pre-registered (`mrm` server in
`backend/mcp.py::DEFAULT_SERVERS`) and appears in **Settings → MCP** where it
can be enabled/disabled and its tools granted per-project. It supports the same
stdio transport contract as the other bundled servers (Python package →
`-m mcp_servers.mrm.server`).

For a remote deployment, wrap the same tool functions in an SSE/HTTP MCP
server and place it behind OAuth2/mTLS with per-agent quotas — the tool layer is
transport-agnostic.

## Example agent prompt (Tier-1 credit-risk workflow)

> Register a Tier-1 credit-risk model, run a controlled synthetic portfolio,
> stress it, prove TSTR on hold-out real data, and produce an audit-ready
> report — all through your MRM tools.

Suggested tool sequence for the coding agent:

1. `mrm__get_profile(category="credit_risk")` — load the domain checklist.
2. `mrm__register_model(name="PD v1", category="credit_risk", synthetic_used=true)`
3. Maker-checker for Tier-1: `mrm__request_approval(model_id, action="tier",
   rationale=…)` then `mrm__decide_approval(approval_id, decision="approve")`,
   then `mrm__tier_model(model_id, tier=1)`.
4. `mrm__generate_synthetic_portfolio(n_loans=10000, seed=42)` → write the
   resulting CSV path into every downstream call.
5. `mrm__evaluate_fidelity(real_path, synthetic_path)` — must PASS.
6. `mrm__run_monte_carlo`, `mrm__run_scenario_set`,
   `mrm__stress_test_portfolio`, `mrm__sensitivity_analysis`.
7. `mrm__tstr_evaluate(synthetic_path, real_path, target="default",
   model_id=…)` — mandatory TSTR evidence.
8. `mrm__detect_drift(reference_path, current_path)`,
   `mrm__run_challenger(data_path, target="default")`.
9. `mrm__log_effective_challenge(model_id, finding=…, severity=…)` then
   `mrm__generate_validation_report(model_id, profile="credit_risk")`.
10. `mrm__audit_log(role="validator")` — verify every call is on record.

## Testing

```bash
.venv/bin/python -m pytest tests/test_mrm_mcp.py -q
```

Covers the full inventory → approval → generate → simulate → validate →
report → audit lifecycle, maker-checker RBAC, fidelity gates, TSTR, drift,
challenger, banking profiles and MCP-host registration (`mrm` in
`DEFAULT_SERVERS`).

## Sample session

`examples/mrm/run_mrm_session.py` is a deterministic end-to-end session that
exercises every tool across all four pillars (six-model inventory, maker-checker
tiering + retirement, generation + privacy budget, Monte Carlo + all five
scenarios + stress + sensitivity, fidelity/TSTR/drift/challenger, and audit)
and renders one chart per scenario family into `examples/mrm/reports/`:

```bash
.venv/bin/python examples/mrm/run_mrm_session.py
.venv/bin/python examples/mrm/seed_session.py   # register it as chat session "mrm-sample-session"
```

See `examples/mrm/session_transcript.md` for the chat-style walkthrough and
`examples/mrm/reports/session_report.md` for the compiled audit-ready report.

## Non-functional properties

- **Graceful degradation**: unknown generators/portfolios/approvals return
  clear errors with remediation; fidelity failures return remediation steps.
- **Extensible**: new generators or metrics are plain callables registered in
  `server.py` — no changes to the core registry or store schema.
- **No real PII**: everything is synthetic by construction; privacy posture is
  tracked via `apply_privacy_budget` (ε-DP composition).
