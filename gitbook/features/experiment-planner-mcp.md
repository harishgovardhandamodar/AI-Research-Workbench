# Deterministic experiment planner

The workbench ships a **deterministic experiment planner**: a catalog of pure,
reproducible experiments (pandas/numpy only — no LLM loop) that follow a strict
**plan → propose → confirm → execute** lifecycle. Given the same seed and
dataset, the same plan always produces the same result.

## The catalog

| id | Experiment | Goal metric | Direction |
|---|---|---|---|
| `eda` | Dataset overview (stats, nulls, duplicates, histograms) | `duplicates` | lower |
| `clean` | Cleaning plan (dedupe + nulls + outlier impact) | `affected_rows` | lower |
| `pii_scan` | PII / identifier scan | `pii_columns` | lower |
| `reid_risk` | Re-identification risk (k-anonymity) | `k_anonymity_1` | lower |
| `dp_privacy` | Differential privacy mean estimation | `min_mae` | lower (seed-sensitive) |
| `correlation` | Pearson correlation analysis | `max_abs_corr` | — |
| `anomaly` | IQR outlier detection | `outlier_cols` | lower |
| `peer` | Bank peer identification & market-share | `identification_accuracy` | higher (seed-sensitive) |

Experiments that use randomness carry the `seed_sensitive` flag, so the planner
suggests verifying single runs with a second seed.

## Lifecycle

```
DRAFT → WAITING_APPROVAL → APPROVED → RUNNING → DONE / FAILED / REJECTED
```

- **Plan** — pick an experiment + dataset (+ optional seed/request). A plan is
  built: steps, expected outputs, and a content-derived seed (or your explicit
  one). Nothing runs yet.
- **Propose** — the plan is proposed in chat (a plan card with a dataset preview
  and step list). You must confirm before anything executes.
- **Confirm / execute** — on approval the plan runs in the background;
  figures + a markdown report are persisted under `plans/<id>/` and registered
  as artifacts; a run is recorded. You can cancel an in-flight run.

Plans are stored per project in `experiment_plans.json` and managed in the
Experiments tab's **Plans** section (approve / run / result / re-propose /
clone / delete). Rejected or failed plans can be **re-proposed** (keeps the id
and history); clones record `parent_id` + `lineage` so run chains are
traceable.

## Incremental suggestions

`GET …/experiment-plans/suggestions` turns your previous runs into ranked,
explainable next steps:

- **New dataset** → start with EDA.
- **PII found** → re-identification + DP.
- **High re-id risk** → DP protection.
- **Strong correlation** → check anomalies.
- **Anomalies** → run the cleaning plan, then re-run the affected experiments to
  confirm (deltas are direction-aware and only trusted when the dataset didn't
  change).
- **High DP error** → anomaly / clean first.
- **Seed-sensitive, single run** → clone with a new seed to verify.
- **Unstable across seeds** → investigate the sensitivity.
- **Cross-dataset** → surface the dataset where a metric deviates most.

Every suggestion carries a stable `suggestion_id` you can **dismiss** (per
project) so it stops nagging, and failed experiments are surfaced once with a
**↻ Retry** action instead of being re-suggested for coverage.

## Endpoints

- `GET /api/experiments/catalog` — the deterministic experiment catalog
- `GET/POST /api/projects/{name}/experiment-plans` — list / create + propose
- `GET …/{id}` · `POST …/{id}/decide|run|cancel|repropose|clone` — lifecycle
- `GET …/result` — persisted figures + report for a DONE plan
- `GET …/suggestions` · `POST …/suggestions/{sid}/dismiss` — suggestions
