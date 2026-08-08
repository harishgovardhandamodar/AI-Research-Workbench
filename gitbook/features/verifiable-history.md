# Verifiable run history

Every run is traceable and tamper-evident: git-backed lineage, full code +
environment capture, content hashes, and a linked audit trail.

## Run ↔ git lineage

- After each experiment run, the management repo auto-commits a snapshot, and
  the **commit hash is written back on the run** (`runs.git_commit`).
- `GET /runs/{id}/commits` resolves a run's commit (self-healing legacy runs via
  `git log -- <snapshot path>`).
- **Restore** (`POST /runs/{id}/restore`) checks the run's artifacts out of its
  commit and forks a `restore` child run (branch graph shows the restoration).

## Full code + environment

- **Full executed code** per tool call is kept (`runs.code`, index-aligned with
  the tool sequence) — so run **diffs** show real code changes, not 200-char
  snippets.
- **Environment snapshot** (`runs.env`) records python/platform/package versions
  at run time; run reports use the run-time env.

## Integrity hashes

- `runs.integrity_hash` = sha256 over the canonical run record (prompt, reply,
  config, metrics, tool sequence, code, env, lineage, model, git commit).
- `GET /runs/{id}/verify` recomputes it → `verified` / `MISMATCH` (tamper
  detected) / not recorded (pre-round-8).

## Per-run audit

- `runs.message_id` links each run to its audit `trace_id`.
- `GET /runs/{id}/audit` returns the run's tool events, any deviations touching
  them, and the audit-chain verification status.

## UI

Run rows show ✓ hash chips + git commit short hashes; branch detail has
**🔒 Verify integrity** and **🛡 Audit trail**; the project report includes
recent runs with integrity + commits.
