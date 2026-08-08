# Round 10 — Research reports & project export

Rounds 2–9 produced a deep, verifiable, comparable record of autonomous research
(experiments, campaigns, evals, learnings, audit). Round 10 turns it into
**communication**: a comprehensive project report (a shareable research
write-up) and a **portable export bundle**. Everything needed already lives in
the per-project store — this round aggregates and packages it.

## Design

### 1. `backend/report.py` — `build_project_report(rt) -> str`
Deterministic markdown aggregation (sections):
- Header: project, generated-at, run/experiment/campaign/artifact counts.
- **Experiments**: the `compare_experiments` leaderboard + per-experiment goal,
  best run + id, plan.
- **Campaigns**: `compare_campaigns` + status + a link to each campaign report.
- **Model benchmarks**: eval leaderboard (`eval.report` summaries).
- **Learnings**: all recorded learnings (the project's accumulated knowledge).
- **Recent runs**: the latest N runs with label/kind/metrics + integrity status
  (`verify_run_integrity`) + git commit short hash.
- **Audit summary**: event counts, open deviations, chain-verification status.
Optional LLM **executive summary** prepended (reuses the `_summarize_run`-style
prompt); best-effort, skipped on failure.

### 2. `backend/export.py` — `export_project(rt) -> Path`
Build a zip in a temp dir (stdlib `zipfile`):
- `report.md`, `experiments.json`, `runs/<id>.json`, `learnings.json`,
  `campaigns.json`, `evals.json`, `suggestions.json`, `audit-summary.json`
- `artifacts/*` (byte copies of the artifact files)
- `provenance.json` — store schema/version + integrity info.
Returns the zip path; caller streams it as a download.

### 3. Routes (`backend/routers/runs.py`)
- `GET /api/projects/{name}/report` → `{"report": markdown}`.
- `POST /api/projects/{name}/report` → generate, save as a `kind="text"`
  artifact + post to chat (tags `["report"]`), return `{report, artifact_id}`.
- `POST /api/projects/{name}/export` → `FileResponse` of the zip
  (`application/zip`).

### 4. Frontend
- Experiments tab header: **📄 Project report** and **📦 Export** buttons.
- Report → shown as an assistant message (markdown renders natively) + artifact.
- Export → triggers a browser download of the zip.

## Files touched
- `backend/report.py` (new), `backend/export.py` (new), `backend/routers/runs.py`,
  `frontend/app.js`, `frontend/index.html`, `frontend/styles.css`,
  `docs/round10-reports.md`, `tests/test_round10.py`.

## Out of scope
- HTML/PDF rendering of the report (markdown is the interchange format; the UI
  already renders markdown, and HTML could reuse the artifact viewer later).
- Git-history export (covered by the management repo).
