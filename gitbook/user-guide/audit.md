# Audit trail

Every agent tool call, MCP request, permission decision, network access, and
filesystem touch is captured, **redacted**, and **hash-chained** (SHA-256,
tamper-evident) into SQLite + append-only JSONL per project.

## The 🛡 Audit view

- **Event timeline** — severity-colored events (info / warning / critical).
- **KPI cards** — Events / Critical / Overrides / Denials / Data access /
  Network / Filesystem / Open deviations / Active agents; clicking filters the
  list.
- **Per-agent history** — tool usage, data classes, network destinations.
- **Permissions drift** — granted permissions vs observed usage.
- **Investigation** — full-text search across events.
- **Deviations** — flags for novel tools, network destinations, and data classes,
  with a scan/review/false-positive workflow.

![Audit trail](../assets/screenshots/audit.png)

## Per-run audit (round 8)

Each run records the turn's message id (its audit `trace_id`). `GET
/runs/{id}/audit` returns the run's tool events, any deviations touching them,
and the audit-chain verification status; **Verify integrity** checks the run's
content hash. Branch-detail shows both.

## Standalone tooling

- **`agent-audit`** CLI — inspect/export the audit trail.
- **MCP proxy** — a transparent proxy for Claude Desktop / Cursor / custom hosts.
- **Streamlit dashboard** and a **hash-chain integrity check**.

## Verifiability

The audit log is a hash chain: each event references the previous event's hash,
so any tampering is detectable (`verify_chain`). Run integrity hashes make each
recorded run independently verifiable.
