# REST API

All endpoints are under the workbench base URL (default `http://localhost:8765`).
Unless noted, JSON in/out. A project-scoped path uses `/api/projects/{name}`.

## System

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Health check |
| GET/POST | `/api/config` | Read / update global config |
| GET | `/api/models` | List models (enriched size/quantization) |
| GET | `/api/editor` | In-browser editor status + reachability |
| GET | `/api/mcp` | MCP server statuses |
| GET | `/api/experiments` | Global experiments overview |
| GET | `/api/system/stats` | Host/GPU resource HUD (cached) |

## Projects

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create a project |
| DELETE | `/api/projects/{name}` | Delete a project |
| GET | `/api/projects/{name}/state` | Project state + management activity |
| GET | `/api/projects/{name}/workflow` | Live workflow snapshot |
| GET | `/api/projects/{name}/workflow/history` | Archived workflow runs |

## Runs & experiments

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects/{name}/runs` | Runs list |
| GET | `/api/projects/{name}/runs/{rid}` | One run (full provenance) |
| GET | `/api/projects/{name}/runs/{rid}/diff` | Diff vs parent/run (config, tools, code, metrics) |
| GET | `/api/projects/{name}/runs/{rid}/commits` | Management-repo commit(s) for the run |
| GET | `/api/projects/{name}/runs/{rid}/audit` | Per-run audit trail + deviations + chain status |
| GET | `/api/projects/{name}/runs/{rid}/verify` | Integrity-hash verification |
| POST | `/api/projects/{name}/runs/{rid}/restore` | Restore run artifacts from its commit |
| POST | `/api/projects/{name}/runs/{rid}/report` | Generate a lab-notebook report artifact |
| GET | `/api/projects/{name}/experiments` | List experiments (with run counts) |
| POST | `/api/projects/{name}/experiments` | Create an experiment |
| GET | `/api/projects/{name}/experiments/{eid}` | One experiment with runs |
| PATCH | `/api/projects/{name}/experiments/{eid}` | Edit objective fields / status |
| GET | `/api/projects/{name}/experiments/{eid}/ranking` | Leaderboard for the experiment |
| GET | `/api/projects/{name}/experiments/compare` | Cross-experiment leaderboard |
| GET | `/api/projects/{name}/experiments/focus` | Focused experiment |
| POST | `/api/projects/{name}/experiments/focus` | Set / clear focus |
| GET | `/api/projects/{name}/experiments/history` | Unified run records |
| GET | `/api/projects/{name}/experiments/graph` | Similarity graph |
| GET | `/api/projects/{name}/experiments/branches` | Git-flow branch graph |

## Goals, learnings, suggestions

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/projects/{name}/goals` | List / add goals |
| DELETE | `/api/projects/{name}/goals/{metric}` | Remove a goal |
| GET | `/api/projects/{name}/learnings` | List learnings (knowledge memory) |
| DELETE | `/api/projects/{name}/learnings/{id}` | Remove a learning |
| GET | `/api/projects/{name}/suggestions` | List suggestion records with status/outcome |
| POST | `/api/projects/{name}/suggestions/{sid}/resolve` | Resolve (regression-check) a suggestion |

## Campaigns & benchmarks

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects/{name}/campaigns` | List campaigns + running flag |
| POST | `/api/projects/{name}/campaigns` | Create a campaign |
| POST | `/api/projects/{name}/campaigns/{cid}/run` | Start / resume in background |
| POST | `/api/projects/{name}/campaigns/{cid}/stop` | Stop |
| GET | `/api/projects/{name}/campaigns/{cid}` | One campaign with steps |
| GET | `/api/projects/{name}/campaigns/compare` | Campaign leaderboard |
| GET | `/api/projects/{name}/evals` | List benchmarks + running flag |
| POST | `/api/projects/{name}/evals` | Create a benchmark |
| POST | `/api/projects/{name}/evals/{eid}/run` | Start in background |
| POST | `/api/projects/{name}/evals/{eid}/stop` | Stop |

## Compare, report, next, export

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects/{name}/compare?run_a=&run_b=` | Pairwise run comparison |
| GET | `/api/projects/{name}/compare?runs=1,2,3` | N-run side-by-side table |
| GET/POST | `/api/projects/{name}/report` | Project research report (markdown) |
| GET | `/api/projects/{name}/next` | Next-research agenda (+ proposed campaign) |
| POST | `/api/projects/{name}/next/post` | Post the agenda to chat |
| POST | `/api/projects/{name}/export` | Portable zip bundle (application/zip) |

## Artifacts, files, notebooks, kernel

| Method | Path | Purpose |
|---|---|---|
| GET | `/artifacts/{id}` | Download artifact bytes |
| GET | `/api/artifacts/{id}/meta` | Artifact metadata |
| GET | `/api/projects/{name}/artifacts` | List artifacts |
| GET/POST | `/api/projects/{name}/files` | List / upload project files |
| GET | `/api/projects/{name}/files/schema` | CSV schema (for `@schema`) |
| GET/PUT | `/api/projects/{name}/notebooks/{nb}` | Read / write a notebook |
| POST | `/api/projects/{name}/notebooks/{nb}/execute` | Execute a notebook |
| GET/POST | `/api/projects/{name}/kernel/status` · `/kernel/execute` | Kernel status / run code |
| POST | `/api/projects/{name}/kernel/reset` | Reset the kernel |
| POST | `/api/projects/{name}/kaggle/import` | Import a Kaggle dataset |

## Finetune (dk-lora)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/finetune/status` | All dk-lora training jobs with live progress + last metrics |
| GET | `/api/finetune/jobs/{job_id}` | One job: record + log tail + metric history |
| GET | `/api/finetune/pipeline` | Pipeline snapshot (stages 1–4) for the chat card |
| POST | `/api/finetune/workspace` | Set the dk-lora workspace directory |

See [Finetune status](../features/finetune-status.md) for the full story.


## Audit

`/api/projects/{name}/audit/*` — `summary`, `events`, `event/{id}`, `timeline`,
`agents`, `agents/{id}/history`, `agents/{id}/permissions`, `deviations`,
`deviations/{id}/review`, `scan`, `verify`, `export`.

## Management repo

`/api/management/repos`, `/api/management/status`, `/api/management/link`,
`/api/projects/{name}/management/commit`, `/push`, `/commit-and-push`.

## Research knowledge graphs

`/api/rkg/*` — pool, graph, scenarios, jobs, scheduler, RAG (`query_rag`).

## Hive companion (narrow AGI)

`/api/hive/health` — companion availability + components.
`/api/hive/research/sessions` — Feynman research sessions (503 with hint when
the optional `[hive]` extra is missing — degraded, never 500).
`POST /api/hive/research/run` — queue a local research run `{topic, model, depth}`.
`/api/hive/machine/status`, `/api/hive/workbench/status` — module probes.
`/api/hive/workbench/profiles` — narrow workbench profiles with full YAML
fields (`description`, `domain`, `datasets`, `allowed_tools`,
`model_preference`, `prompts`, `evaluation`, `constraints`) — same source as
Journey (single source of truth).
`POST /api/hive/workbench/loops/run` — run a narrow AGI loop
`{profile, task, iterations}` (hash-chained audit event).
`/api/hive/loops/status`, `/api/hive/audit/timeline` (`limit`, `session_id` —
nodes=actor, edges=action, captures for overlays).
`/api/hive/journey` — Narrow Space AGI dashboard: profiles, narrow runs,
charts, mermaid diagrams, ledgers, auditable proofs, `agi_features[12]` with
per-feature availability, `learn` status (ledger rewards → memory). See
`docs/REMOTE-WORKBENCH.md` for the remote half and the Journey tab (`#journey`).

## Remote workbench (LAN / Tailscale)

`/api/remote/hosts` — list (tokens redacted) / add-or-update remote
`fox-kernel` hosts `{name, base_url, username, token}`; seeded read-only from
`REMOTE_HOSTS` when unconfigured (stable `seed-*` ids).
`DELETE /api/remote/hosts/{id}`, `POST /api/remote/active` — remove / select.
`POST /api/remote/discover` — on-demand probe: `/health` (5s) then
`/api/kernel/gpu` (8s); no SSH, no background pollers. Reports
`online/compatible/GPU devices/latency` per host.
`POST /api/remote/run` — offload `{host_id?, project, code, timeout≤600,
label?, experiment_id?, use_gpu?}`; records a `kind="remote"` run with
host/duration/GPU metrics. `use_gpu:true` fails fast when the host reports no
GPU. Remote side: `GET /api/kernel/gpu`, `POST /api/kernel/execute` (Bearer
`REMOTE_TOKEN` when set). GUI: Remote tab (`#remote`). Tunnel guide:
`docs/REMOTE-WORKBENCH.md`, unit: `deploy/axiom/fox-kernel.service`.
