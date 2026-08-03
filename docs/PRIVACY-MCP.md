# Expanded Privacy MCP Server + Workbench Integration

Local-first privacy tooling for the Fox AI Science Workbench. Everything runs on
the user's machine — no data leaves home.

- **Privacy assessment** — PII detection, dataframe privacy assessment
- **Red-teaming / evaluation** — membership-inference attacks, re-identification
  scenarios, corner-case hunting, adversarial checklists
- **Differential privacy** — Laplace / Gaussian mechanisms, ε/δ tracking,
  privacy-budget accounting, guarantee visualization data
- **Synthetic data** — schema-preserving generation + quality/utility metrics

---

## 1. Architecture

```
Privacy MCP Server (mcp_servers/privacy_tools.py)
├── Detection & Assessment
│   ├── privacy__detect_pii_in_text
│   └── privacy__assess_dataframe_privacy
├── Red-Teaming / Evaluation
│   ├── privacy__membership_inference_eval
│   ├── privacy__reidentification_scenario
│   └── privacy__privacy_redteam_checklist
├── Differential Privacy
│   ├── privacy__apply_laplace_dp
│   ├── privacy__apply_gaussian_dp
│   ├── privacy__dp_privacy_budget_report
│   └── privacy__dp_guarantee_summary
└── Synthetic Data
    ├── privacy__generate_synthetic_tabular
    └── privacy__synthetic_data_quality_report
```

Registered in `backend/mcp.py` as the **`privacy`** MCP server. The agent calls
tools as `privacy__<tool>` (namespaced, provenance-preserving). Read-only tools
run freely; `privacy__generate_synthetic_tabular` writes a file, so it asks the
user once (matching the workbench permission model).

Dependencies: the heavy optional libraries (`presidio`, `sdv`, `opendp`) are
**used when installed**; otherwise built-in implementations provide the same
capability (regex PII scanning, native Laplace/Gaussian mechanisms,
schema-preserving generation). Only `mcp` and `faker` are required.

## 2. Tool reference

### Detection & assessment

`privacy__detect_pii_in_text(text)` — regex scan for emails, phones, credit
cards, US SSNs, IBANs, IPs, postal codes. Returns findings with positions,
counts by type, and a risk level.

`privacy__assess_dataframe_privacy(file_path, quasi_identifier_columns=None)` —
loads a local CSV, classifies columns (sensitive / quasi-identifier / other),
and estimates k-anonymity-style re-identification risk (min k, median k,
% unique records, risk level, recommendation).

### Red-teaming / evaluation

`privacy__membership_inference_eval(model_predictions, is_member_labels,
threshold=0.5)` — attack accuracy, AUC, and membership advantage
(advantage > 0.15 ⇒ significant leakage risk).

`privacy__reidentification_scenario(quasi_identifiers, population_size,
equivalence_class_sizes)` — classic k-anonymity re-id risk from equivalence
class sizes.

`privacy__privacy_redteam_checklist(data_type, has_model=False,
public_release=False)` — adversarial evaluation checklist (adds model- and
release-specific checks when applicable).

### Differential privacy

`privacy__apply_laplace_dp(values, epsilon, sensitivity=1.0, seed=42)` — pure
ε-DP (δ=0) via Laplace noise; returns original/noisy values and the scale.

`privacy__apply_gaussian_dp(values, epsilon, delta=1e-6, sensitivity=1.0,
seed=42)` — approximate (ε, δ)-DP via Gaussian noise.

`privacy__dp_privacy_budget_report(operations)` — sequential-composition ledger
of ε/δ across operations; returns total budget and a `visualization_hint`
(budget bar, recommended max ε=1.0).

`privacy__dp_guarantee_summary(epsilon, delta=0.0)` — plain-English explanation
of the (ε, δ) guarantee plus a `visualization` object (ε-gauge with zones) for
the UI/artifacts.

### Synthetic data

`privacy__generate_synthetic_tabular(file_path, num_rows=1000, method="basic",
seed=42)` — schema-preserving generation (per-column distributions; `smoothed`
adds noise to numeric columns; `sdv` uses Synthetic Data Vault if installed).
Writes `synthetic_<stem>_<rows>.csv` next to the input.

`privacy__synthetic_data_quality_report(real_path, synthetic_path)` — utility
comparison: numeric mean/std per column, categorical top-category match.

## 3. Example experiments

| Notebook | Covers |
|----------|--------|
| `20_privacy_assessment` | PII detection, dataframe assessment, membership-inference & re-id red-teaming on a small clinical cohort |
| `21_differential_privacy` | Laplace/Gaussian mechanisms, privacy budget, ε-gauge figure |
| `22_synthetic_data` | Generate synthetic cohort + quality report + distribution comparison |
| `23_privacy_peer_workflow` | End-to-end workflow (below) |

Runnable scripts:

```bash
# red-team + DP + synthetic-data evaluation on a small clinical cohort
.venv/bin/python examples/privacy/run_privacy_eval.py

# peer-in-distribution exploitation -> red-team -> DP robustness + audit trail
.venv/bin/python examples/privacy/run_peer_exploitation.py
```

## 4. The privacy workflow (auto-triggered)

When the researcher asks for the workflow — *exploit privacy as a peer in the
distribution, run red-team corner-case analysis, apply DP and check robustness,
and document everything as an audit trail* — the backend detects the prompt and
**automatically runs** `examples/privacy/run_peer_exploitation.py`
(deterministic, no LLM required), then registers every report and figure as an
artifact.

**Stage 1 — Exploiting privacy as a peer in the distribution**
The attacker holds their own data drawn from the same population distribution.
Coverage is varied (5% → 50% of the population); linkage success and
attribute-inference error are measured. Findings: linkage rises steeply with
coverage, so rare QI profiles must be suppressed before release.

**Stage 2 — Corner cases / red-team analysis**
Singleton equivalence classes, rare conditions, extreme-amount outliers, plus
the red-team MCP tools. Findings: rarity is the attacker's friend — singletons,
rare categories and outliers are the exploitable corner cases that masking
cannot protect.

**Stage 3 — Differential-privacy robustness**
The exploited aggregate is released under Laplace DP at
ε ∈ {0.1, 0.5, 1.0, 2.0, 5.0}; the attack is re-run against the noisy outputs.
Attacker prediction RMSE inflates as ε shrinks (protection index up to ~85% at
ε=0.1). A privacy-budget ledger is maintained.

**Reports** (all become artifacts):
- `examples/privacy/reports/1_peer_exploitation.md`
- `examples/privacy/reports/2_redteam_findings.md`
- `examples/privacy/reports/3_dp_robustness.md`
- `examples/privacy/reports/audit_trail.md` — full ideation → methodology →
  actions → decisions → conclusions → limitations audit trail

**Figures:** `fig_peer_coverage.png`, `fig_corner_cases.png`,
`fig_dp_robustness.png`.

**Fresh rerun flag.** The workflow is deterministic (fixed seed) so re-running
it yields the same reproducible results. To force a genuinely new run, include a
fresh/rerun marker in the prompt — e.g. *"… rerun with fresh results"* or
*"… force rerun, new seed, different results"*. The backend then runs the
workflow with a new random seed and serves back new numbers, figures and
reports (a "Fresh rerun" note is added to the chat message). Standalone:

```bash
.venv/bin/python examples/privacy/run_peer_exploitation.py --fresh   # new seed each run
.venv/bin/python examples/privacy/run_peer_exploitation.py --seed 7  # specific seed
```

## 5. Workbench integration guidance

| Capability | How the workbench uses it |
|------------|---------------------------|
| Detection tools | Suggest `privacy__detect_pii_in_text` / `privacy__assess_dataframe_privacy` when a dataset is loaded or the user mentions sharing/publishing |
| Red-teaming | `privacy__privacy_redteam_checklist` + membership/re-id scenarios for a "Privacy Red Team" pass |
| DP tools | Offer DP versions of aggregate queries and surface the ε-gauge |
| Privacy budget | Track a session ledger; call `privacy__dp_privacy_budget_report` after each DP query |
| Synthetic data | "Generate safe version" → `privacy__generate_synthetic_tabular` → stored as a first-class artifact with provenance |
| Visualization | `visualization` / `visualization_hint` objects (ε-gauge, budget bar) are emitted for the artifact panel |
| Audit trail | Every privacy tool result is attached as an artifact linked to the conversation |

## 6. Recommended agent workflow

```
When handling potentially sensitive scientific data:
1. Always start with privacy__detect_pii_in_text + privacy__assess_dataframe_privacy.
2. If the user wants to share or publish -> run the red-team checklist and
   privacy__reidentification_scenario.
3. Prefer synthetic data or DP aggregates over releasing microdata.
4. Track privacy budget when DP is used and surface the guarantee visually.
5. Attach every assessment as a provenance-linked artifact.
```
