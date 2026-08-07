# Local Agent Audit Trail & Dashboard

A fully local audit trail for AI agents and MCP tools: every tool call, MCP
request, permission decision, network access and filesystem touch made by an
agent is captured, **redacted**, **hash-chained** (tamper-evident) into
SQLite + append-only JSONL, and visualised in a **timeline view** inside the
workbench — plus a standalone Streamlit dashboard, a transparent **MCP proxy**
and a CLI. No cloud services.

```
             ┌──────────────────────────────────────────────┐
  agent ───▶ │  audit_tool() decorator / AuditedSession     │──┐
  workbench ─▶│  Coordinator tool calls (+ approvals + MCP)  │──┤
  Claude/Cursor ─▶ agent-audit proxy (stdio / HTTP)          │──┤
             │                                               │  ▼
             └──────────────────────────────────────────────┘  LocalAuditStore
                                                                 ├─ SQLite  (queryable)
                                                                 └─ events.jsonl (SHA-256
                                                                    hash chain)
                                                                        │
                                          Dashboard (in-app timeline ·      │
                                          Streamlit · `fox audit` CLI)  ◀───┘
```

## Quickstart (inside the workbench)

The workbench hosts one audit store per project (`<project>/audit/`) and
audits the coordinator agent automatically:

```bash
.venv/bin/pip install -e .          # installs `agent-audit` too
./run.sh                             # start the server
# open http://127.0.0.1:8765 → **Audit Trail** tab
```

Ask Fox to run anything in chat (a plot, a notebook, a shell command, an MCP
tool). Every tool call appears in the Audit Trail **timeline** within seconds:
severity-coloured nodes, agent badges, network/filesystem/policy flags,
redacted arguments, durations. Approvals show up as `OVERRIDE`/`DENY` policy
events. Run a **Scan** (or wait — it runs after each turn) to flag
deviations.

### Command line

```bash
fox audit <project>            # KPI overview (server-side data)
fox audit <project> events     # recent events table
fox audit <project> deviations # flagged deviations
fox audit <project> agents     # per-agent activity
fox audit <project> verify     # hash-chain integrity (exit 1 if broken)
```

## Standalone CLI: `agent-audit`

The same store/emitter/proxy work outside the workbench. Events land in
`$AUDIT_DIR` (default `~/.agent-audit`):

```bash
agent-audit query  --agent my-agent --severity critical
agent-audit verify
agent-audit baseline --agent my-agent
agent-audit export --format csv > events.csv
agent-audit dashboard              # optional Streamlit UI (pip install streamlit)
```

## Transparent MCP proxy

Point any MCP client at the proxy and it audits every request before
forwarding it to the real server.

**stdio → stdio** (Claude Desktop / Cursor / VS Code):

```jsonc
// ~/.config/Claude/claude_desktop_config.json
"mcpServers": {
  "my-server": {
    "command": "agent-audit",
    "args": ["proxy", "--server", "python3", "-m", "my_mcp_server"]
  }
}
```

**stdio client → HTTP server**, or expose the proxy itself over HTTP:

```bash
# proxy a remote streamable-HTTP MCP server (from any stdio client)
agent-audit proxy --server-dir ~/.agent-audit --http https://example.com/mcp

# or serve the proxy as its own HTTP endpoint on :8010/mcp
agent-audit proxy --server "python3 -m my_mcp_server" --http-server --port 8010
```

Every `tools/call` / `resources/read` is redacted, risk-tiered and appended to
the chain. Verify with `agent-audit verify`.

## Python middleware for your own agents

```python
import asyncio
from audit import AuditEmitter, LocalAuditStore
from audit.middleware import audit_tool

store = LocalAuditStore("~/.agent-audit")
emitter = AuditEmitter(store)

@audit_tool(emitter, agent_id="researcher")
def lookup_pdb(pdb_id: str, token: str):
    return "1ABC: 123 atoms"

lookup_pdb("1ABC", token="super-secret")     # token is redacted in the log
```

Or wrap an existing `mcp.ClientSession` with `AuditedSession(session, emitter, ...)`
to audit a custom MCP agent without a separate proxy.

## What gets captured

`AuditEvent` fields: event id (ULID), timestamp, trace/session ids, agent id
+ principal, source (`mcp_proxy` / `middleware` / `coordinator` / `approval` /
`system`), MCP server, method, tool name, **redacted arguments**, result
summary (status, data classes, size, error), network destination, filesystem
path+operation, policy decision (ALLOW/DENY/OVERRIDE + risk tier + reason),
duration, severity, tags, and `prev_hash`/`event_hash` for chain integrity.

**Redaction** masks API keys, tokens, passwords, authorization headers,
URL-embedded credentials and long token-like values before anything is stored.

**Risk tiers**: `low` (run_python, read-only tools) → `medium` (notebooks,
science/privacy read tools) → `high` (github, robustness, synthetic-data,
editor writes) → `critical` (run_shell). Configurable via the `PolicyRule`
store.

**Deviation detector** (runs after each agent turn and on demand): flags
novel tools, novel tool sequences, tool-frequency spikes, previously unseen
data classes, filesystem paths outside the baseline roots, unseen network
destinations, high-risk tools outside working hours, and "denial then
override" sequences. Findings are stored as `DeviationRecord`s and appear in
the Deviations tab (mark reviewed / false positive).

## Storage & integrity

- SQLite `audit_events` / `audit_deviations` tables, indexed on timestamp,
  agent, tool, severity and session. WAL mode.
- Append-only `events.jsonl` with `event_hash = sha256(canonical_json + prev_hash)`.
  `agent-audit verify` (or the in-app chain badge) detects any tampering and
  reports exactly which record broke the chain.

## Local MCP proxy notes

The proxy spawns/connects the downstream server **before** serving so the
handshake happens outside the request-handler cancel scope; audit emission is
queued (async) so it never blocks the tool call. Downstream connection
failures are logged at startup and surfaced as tool errors.

## API (used by the in-app view)

All endpoints under `/api/projects/<name>/audit`:

| Endpoint | Purpose |
|---|---|
| `GET /summary` | KPI cards (totals, criticals, overrides, denials, data/network/fs, open deviations) |
| `GET /timeline` | chronological events for the timeline view |
| `GET /events` | searchable event list (`agent`,`source`,`tool`,`severity`,`q`,`since`,`until`) |
| `GET /event/{id}` | one event (redacted) |
| `GET /agents` · `GET /agents/{id}/history` | per-agent activity + tool usage + data classes |
| `GET /agents/{id}/permissions` | granted vs observed permission drift |
| `GET /deviations` · `POST /deviations/{id}/review` | deviations + review/false-positive |
| `POST /scan` | run the deviation scan now |
| `GET /verify` | JSONL hash-chain integrity |
| `GET /export?fmt=json\|csv` | export the audit log |

## Acceptance criteria

- ✅ Point Claude Desktop / any local agent at the proxy (or just chat in the
  workbench) → every tool call shows in the timeline within seconds.
- ✅ `agent-audit verify` (and the in-app chain badge) report integrity.
- ✅ Sensitive values are redacted.
- ✅ Novel high-risk tools / unseen paths are auto-flagged as deviations.
- ✅ `pip install -e .` + two commands (`proxy` + dashboard / in-app view).
