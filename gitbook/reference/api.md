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

## Audit

`/api/projects/{name}/audit/*` — `summary`, `events`, `event/{id}`, `timeline`,
`agents`, `agents/{id}/history`, `agents/{id}/permissions`, `deviations`,
`deviations/{id}/review`, `scan`, `verify`, `export`.

## Management repo

`/api/management/repos`, `/api/management/status`, `/api/management/link`,
`/api/projects/{name}/management/commit`, `/push`, `/commit-and-push`.

## Research knowledge graphs

`/api/rkg/*` — pool, graph, scenarios, jobs, scheduler, RAG (`query_rag`).
