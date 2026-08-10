# Round 14 — Deterministic experiment planner + MCP management & orchestration

This round hardens the **deterministic experiment planner** (plan → propose →
confirm → execute) into a robust, reproducible engine with incremental
suggestions driven by prior runs, and makes the workbench's **MCP servers**
first-class — manageable and orchestratable from both the Chat and Experiments
tabs.

## Part A — Robust deterministic experiment planner

### Determinism & reproducibility
- **Derived seeds** — a plan with no explicit seed derives one from
  `(experiment, dataset, request)` via CRC32, so identical requests reproduce
  identically; explicit seeds always win (`seed_source` records which).
- **Dataset fingerprints** — every plan records the SHA-256 of its dataset at
  creation and re-pins it at execution, so metric deltas are never confounded
  by silent data edits.
- **Stable tie-breaks** — latest-run selection and suggestion ordering key on
  `(updated_at, created_at, id)` / catalog order, never iteration order.

### Robustness
- **Atomic + cross-process locking** — `PlanStore` writes are atomic (rename)
  and guarded by an OS-level `flock`, so the REST host and the MCP process
  share one JSON store without lost updates.
- **Timeouts & recovery** — `execute_plan` runs on a worker thread with a
  configurable timeout (default 300 s); plans a restart left `RUNNING` are
  recovered to `FAILED` at startup; a racing cancel flips the persisted status
  to `REJECTED` so results are never double-registered.
- **Dataset I/O** — CSV / Parquet / XLSX loading (`load_dataset`) and cheap
  first-row peeks for column validation and proposal previews.

### Incremental suggestions
`GET /api/projects/{name}/experiment-plans/suggestions` derives ranked next
steps from prior plans — deterministic and explainable:
- **Cold-start onboarding** — a freshly uploaded dataset with no plans gets an
  EDA suggestion; a dataset present but never planned is discovered by scanning
  the project dir.
- **Finding-driven follow-ups** — PII found → re-identification + DP; high re-id
  risk → DP; strong correlation → anomaly; anomalies → cleaning plan; high DP
  error → anomaly/clean first.
- **Failure-aware coverage** — an experiment whose latest attempt failed is not
  re-suggested for coverage; a low-score "repropose" notice points at the error.
- **Remediation tracking** — after a DONE `clean`, affected experiments are
  re-run to confirm; pre/post metrics are compared *direction-aware*
  (higher/lower better) and only when the dataset hash is unchanged (otherwise
  the delta is flagged as confounded).
- **Seed sensitivity** — single-run stochastic experiments (DP, peer) get a
  "clone with a new seed to verify"; two or more runs with a large metric span
  get an "unstable across seeds" flag.
- **Cross-dataset insight** — for direction-scoped experiments on ≥ 2 datasets,
  the dataset that deviates most is surfaced for investigation.
- **Dismissable state** — each suggestion has a stable content-addressed
  `suggestion_id`; the user can dismiss it (persisted per project) so it stops
  nagging.

### Goals & experiments integration
- Catalog experiments carry a **goal metric** and an explicit **higher/lower
  better** direction; the Experiments-tab experiment inherits both (no more
  hardcoded higher-is-better ranking).
- The catalog grew a **`clean`** experiment (dedupe / null / outlier impact) so
  "clean first" suggestions are actionable.

## Part B — MCP management & orchestration

### Registry & management
- **Per-server enable/disable** (persisted) — disabled servers are neither
  probed nor offered to the agent; **add / edit / remove** servers from
  Settings; **refresh** clears the status cache.
- **Status + tool catalog** — `GET /api/mcp` reports health, transport, trust,
  enabled, and a full tool catalog (name, description, read-only flag, and the
  JSON-schema **params**), cached for 5 s.
- **Secret-safe config** — `GET /api/config` masks MCP `env`/`headers` tokens
  and kaggle keys; saving a redacted config merges the live secrets back rather
  than clobbering them.
- **Lifecycle** — the app closes the MCP registry (stdio subprocesses) on
  shutdown; connections timeout (120 s) and reset cleanly.

### Orchestration from Chat
- **`@mcp <server>__<tool> [json]`** — deterministically invoke any MCP tool
  with no LLM round-trip. Read-only tools run freely; writable tools go through
  the same approval broker as the agent. Results are JSON pretty-printed into a
  code block, and oversized results point at the recorded run.
- **`@mcp bg …`** — background mode: returns a `running` run id immediately and
  completes the call in a task, posting a notice when it finishes.
- **Discovery** — a bare `@mcp` lists every connected server's tools (with
  required-arg signatures); a "not found" error appends the same listing.
- **Slash commands** — `/complete`, `/cancel`, `/activate <name|id>` change an
  experiment's lifecycle status from chat; completing also publishes its
  aggregate report.

### Orchestration from the Experiments tab
A dedicated **MCP** section shows each server (health, trust, enable/disable),
a filterable tool catalog, and per-tool actions:
- **▶ Call** — schema-driven form for scalar-param tools (typed inputs) or a
  JSON box for complex tools; sync or **⏳ background**; results render inline.
- **📈 track as experiment** — attach the call to (or create) an experiment and
  parse flat numeric metrics from a JSON reply into the run.
- **🔓 Allow** — grant-on-demand for writable tools; **💾 Save as artifact**;
  **📋 Copy / ↻ Re-run**.
- **Recent MCP calls** — the last direct calls with one-click re-run.

## Part C — Experiment status, results & reports

- **Inline results** — run detail renders the run's reply (markdown + figures)
  and its registered figure artifacts.
- **Card liveness** — experiment cards show last-run status + time and a
  pulsing live marker while the agent is working on them.
- **Reports hub** — a **Reports** section consolidates published reports (run
  lab-notebooks, planner `report.md`, EDA) with open / preview / copy /
  regenerate / publish-to-chat.
- **Experiment reports** — `POST /experiments/{eid}/report` builds an aggregate
  report (goal, best vs target + %, run table, learnings, review highlights);
  marking an experiment **completed** auto-publishes it.

## Endpoints added

- Planner: `GET/POST …/experiment-plans`, `…/{id}/decide|run|cancel|repropose|clone`,
  `…/suggestions`, `POST …/suggestions/{sid}/dismiss`
- MCP: `POST /api/mcp/refresh`, `POST /api/mcp/servers/{name}/enabled`,
  `PATCH /api/mcp/servers/{name}`, `POST /api/mcp/tools/{server}/{tool}`
  (sync/background/experiment), `GET|POST /api/projects/{name}/mcp/grants`,
  `POST …/mcp/artifacts`, `GET …/mcp/activity`
- Reports: `GET /api/projects/{name}/reports`,
  `POST …/reports/{artifact_id}/publish`,
  `GET|POST …/experiments/{eid}/report`

## Files touched
`backend/experiment_planner.py`, `backend/exp_catalog/*`, `backend/mcp.py`,
`backend/routers/{system,runs,experiment_planner}.py`, `backend/main.py`,
`backend/store.py`, `mcp_servers/experiment_planner/server.py`,
`frontend/{index.html,app.js,styles.css}`, plus
`tests/test_experiment_planner.py`, `tests/test_mcp_management.py`,
`tests/test_reports.py` (603 tests green).
