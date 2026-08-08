# Round 8 — Verifiable run history & per-run audit

Rounds 4–7 made runs reproducible (git lineage, full code, env), orchestrated
(campaigns), and knowledgeable (learnings). Round 8 makes the record **trustworthy**:
every run is (a) linked to its audit trail (tool events, deviations) via the
turn's trace id, and (b) covered by a **content hash** so the recorded history is
tamper-evident and verifiable. The per-project audit store
(`audit.store.LocalAuditStore`) already keeps trace-keyed events, a hash-chained
log, and deviations — this round wires runs to it.

## Design

### 1. Run ↔ trace + integrity (`backend/store.py`)
- Migrations: `runs.message_id INTEGER` (the turn's user-message id = audit
  `trace_id`) and `runs.integrity_hash TEXT`.
- `add_run(..., message_id=None)` computes `integrity_hash` = sha256 over the
  canonical run record (prompt, reply, status, kind, label, experiment_id,
  parent_run_id, model, git_commit, config, metrics, tool_sequence, code, env —
  normalized to the shapes `_row_run` returns, so recompute round-trips).
- `_row_run` exposes `message_id` + `integrity_hash`.
- `verify_run_integrity(rid)` recomputes and compares → `{ok, hash, message}`
  (legacy rows: `ok=None`, "no integrity hash recorded").

### 2. Record the trace id
- `Coordinator._record_run`: include `message_id = getattr(self.ctx,
  "message_id", "") or ""` in the record dict.
- `main.py` `_record_run` and the background runner pass it through.

### 3. REST
- `GET /api/projects/{name}/runs/{rid}/audit` → the run's audit events
  (`audit_store.query(trace_id=...)` via `public_event`), any deviations whose
  `event_ids` intersect the run's events, and the audit-chain verification
  status (`verify_chain`).
- `GET /api/projects/{name}/runs/{rid}/verify` → integrity check result.

### 4. UI
- Run rows + branch detail: an **integrity chip** (`✓ verified` / `✗ mismatch` /
  `—` when unrecorded).
- Branch detail: an **Audit trail** section (fetch `/runs/{rid}/audit`): tool
  events (name, severity, duration, network/fs/data flags), deviations, chain
  status, and the integrity hash.

## Files touched
- `backend/store.py`, `backend/agents/coordinator.py`, `backend/main.py`,
  `backend/project_runtime.py` (background runner), `backend/routers/runs.py`,
  `frontend/app.js`, `frontend/styles.css`, `docs/round8-audit.md`,
  `tests/test_round8.py`.

## Out of scope
- Git-commit-anchored integrity (the hash is over the run record, not git).
- Per-run audit for rows recorded before this round (message_id is NULL).
