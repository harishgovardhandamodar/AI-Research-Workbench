# MRM sample session

A deterministic, self-contained Model Risk Management run for banking data
simulations (SR 11-7 / 2026 interagency-aligned) that exercises **every tool**
on the `mrm` MCP server across all four pillars, with **one chart per scenario
family**.

## Run it

```bash
.venv/bin/python examples/mrm/run_mrm_session.py
```

Everything is isolated and repeatable (fixed seeds 42/43/99):

| path | contents |
|------|----------|
| `store/` | the SQLite MRM store (`FOX_MRM_STORE`), datasets, simulation lineage, reports |
| `reports/session_report.md` | audit-ready compiled report (tables + embedded figures) |
| `reports/figures/` | 13 charts — inventory, approvals, portfolio, transactions, Monte Carlo, all-5-scenarios, stress, sensitivity, fidelity, TSTR ROC, drift, challenger, audit |
| `reports/runs.json` | machine-readable metric summary |
| `session_transcript.md` | the same run as a chat-style session transcript |

## Register it as a chat session

Register the run as a named workbench session (**`mrm-sample-session`**) so it
appears in the chat UI with the conversation replayed and figures registered
as artifacts:

```bash
.venv/bin/python examples/mrm/seed_session.py
```

Idempotent — it replaces the `mrm-sample-session` project under
`workbench/projects/` and is safe to re-run after any changes. Then start the
server (or refresh the project picker) to open it.

## What it covers

1. **Pillar 3 — governance:** six models across all six banking profiles,
   maker-checker Tier-1 approval, a rejected→re-approved approval-gated
   retirement, lifecycle status changes.
2. **Pillar 1 — controlled generation:** loan portfolio (one-factor Vasicek)
   + transaction stream (fraud injection), DP privacy budget, documented
   generator assumptions, dataset/simulation lineage. A simulated stand-in
   "real" book and a drifted current-quarter book provide honest references.
3. **Pillar 1/2 — simulation:** Monte Carlo (VaR/ES/histogram), **all five
   scenarios** (upside / baseline / mild / severe / systemic), stress test,
   PD sensitivity, engine version comparison.
4. **Pillar 2 — validation:** fidelity gates, **mandatory TSTR** against
   hold-out REAL data, performance metrics, drift monitoring, challenger
   head-to-head.
5. **Documentation & controls:** cross-file consistency, effective challenge
   (incl. a high finding that flags the fraud model's report), audit-ready
   validation report, evidence, and the append-only audit trail.

The run deliberately shows both green and amber paths (fidelity correlation
gate FAIL, drift DETECTED, cross-file REVIEW) — the gates working as designed,
with remediation recorded, before the mandatory TSTR acceptance.

## Wiring

The example talks to the same server the workbench registers in
`backend/mcp.py` (`mrm` entry, `trusted: False`). In chat, the tools appear as
`mrm__<tool>`; this example calls the underlying functions directly and adds
the maker/checker/auditor `actor`/`role`/`purpose` kwargs that the audit log
captures.
