# Research Knowledge Graphs

> **Full documentation:** [`docs/KNOWLEDGE-GRAPHS.md`](../../docs/KNOWLEDGE-GRAPHS.md)
> — architecture, data model, ingestion pipeline, RAG, and the Research
> Workbench autoresearch loops, with Mermaid diagrams.

Vendored app logic + views from
[`hive-research-gpu`](https://github.com/your-org/hive-research-gpu) (research
knowledge base: arXiv ingestion, LLM-powered analysis, typed Hive knowledge
graph, RAG search, research pool, web ingestion, paper similarity, and the
dashboard / landscape views).

Only the **app logic and views** are vendored — no ingested papers, PDFs,
graph data, or vault notes are committed. Runtime data is written under the
workbench data dir (`<FOX_WORKBENCH_DIR>/research_knowledge_graphs/`), which is
gitignored.

## What's here

| File | Origin | Purpose |
|------|--------|---------|
| `hive_datatype.py` | hive-datatype | `HiveGraph` / `Node` / `Edge` data model |
| `arxiv_fetcher.py` | hive_research | arXiv API client (search / fetch / PDF download) |
| `parser.py` | hive_research | PDF text / figure / reference extraction (PyMuPDF) |
| `llm.py` | hive_research | Ollama client (generate / embed / structured extraction) |
| `gpu.py` | hive_research | NVIDIA GPU monitoring + Ollama instance lifecycle |
| `graph.py` | hive_research | Knowledge graph wrapper around `HiveGraph` |
| `pipeline.py` | hive_research | Paper ingestion pipeline (analysis + graph + notes) |
| `rag.py` | hive_research | RAG engine (chunk → embed → cosine → answer) |
| `pool.py` | hive_research | Research pool: SQLite topic monitors over arXiv |
| `similarity.py` | hive_research | Paper similarity algorithms (author / abstract / concept / vector) |
| `web_ingest.py` | hive_research | Web page ingestion into the graph |
| `organizer.py` | hive_research | Top-level orchestrator tying subsystems together |
| `logs.py` | hive_research | In-memory log capture for the dashboard |
| `config.py` | hive_research (adapted) | YAML config; data root defaults under the workbench dir |
| `router.py` | new | FastAPI router exposing the REST API (namespaced `/api/rkg`) |
| `views/dashboard.html` | hive_research | Full dashboard SPA (API calls rewritten to `/api/rkg`) |
| `views/landscape.html` | hive_research | Similarity-map landscape view (API calls rewritten to `/api/rkg`) |

## Wiring

- `backend/main.py` mounts `research_knowledge_graphs.router.router` on the
  FastAPI app.
- REST API is namespaced under `/api/rkg/*` so it never collides with the
  workbench's own `/api/*` routes.
- Views are served at `/rkg/dashboard` and `/rkg/landscape`.
- The workbench frontend has a **Knowledge Graphs** main-view tab that embeds
  `/rkg/dashboard` (see `frontend/index.html`, `frontend/app.js#loadRkg`).

## Configuration

Defaults live in `config.py` (`Config`). The data root defaults to
`<workbench>/research_knowledge_graphs/` (empty + gitignored); override with a
`config.yaml` whose `directories.root` points elsewhere:

```yaml
directories:
  root: /absolute/path/to/data
  papers: /absolute/path/to/data/papers
  graph: /absolute/path/to/data/graph
  vault: /absolute/path/to/data/vault
```

Ollama settings follow the same env-var overrides as the original app
(`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_FAST_MODEL`, `OLLAMA_EMBED_MODEL`).

## Research Workbench (autoresearch loops)

The dashboard's **Research** panel drives domain-scoped autoresearch loops over
the knowledge graph. Each **scenario** is a research domain (arXiv topics + a
lens). Two sample scenarios ship with the tool:

- **Autonomous Agents & Security Lapses** (`autonomous-agents-security`)
- **Enterprise AI Adoption & Security Lapses** (`enterprise-ai-security`)

A scenario's chained loop (implemented in `research_loop.py`) runs four phases:

1. **Build corpus** — scenario topics are (re)seeded into the research pool,
   the pool refreshes from arXiv, and the freshest un-imported candidates are
   ingested into the knowledge graph (PDF → LLM analysis → concepts/tags/
   relations → vault notes).
2. **Synthesize** — an LLM writes a domain research report (key findings,
   security-lapses catalog, method taxonomy, trends, open problems, references)
   grounded in the corpus. An LLM reviewer scores it 0–100; the loop iterates
   and keeps only score-improving revisions. **Plateau early-stop** halts the
   loop once the score reaches the scenario's `review_target` or after
   `plateau_iters` (default 2) flat iterations; every `[arXiv:xxxx]` citation
   is **audited** against the corpus and ungrounded ones are stripped.
3. **Experiments** — the strongest corpus papers (by graph degree + recency)
   are ranked, their extracted experiment specs become runnable `experiment.py`
   files, and each runs a bounded improve loop (propose → run N times → keep/
   revert on the **mean** `METRIC` over `num_runs` runs, default 3). Results
   record mean/stdev, best-effort paper-reported value, and `delta_vs_paper`,
   appended to the paper's vault note and the scenario's `results.md`.
4. **Fold back** — synthesis re-runs with a **replication results table**
   (paper, metric, workbench best, paper-reported, Δ vs paper, N runs) which the
   final report embeds under *Replication Results (Workbench)*.

Per-scenario state lives under `<root>/scenarios/<scenario_id>/`:
`scenario.json` (config + corpus + results), `report.md` (best report),
`log.md`, `experiments/`, and `project/workbench.db` (loop runs recorded via
the workbench's `ProjectStore`, attached to a per-scenario experiment).

All long phases are background jobs — the dashboard polls
`/api/rkg/jobs/{id}` and the live per-scenario status at
`/api/rkg/scenarios/{sid}/status` (phase, progress, message, log tail). The
job registry and per-scenario live state are **persisted** to disk (atomic
writes) so a restart marks interrupted work rather than losing it, and a
scenario that already has a `running` long job **refuses** a second one
(HTTP 409).

An opt-in **scenario scheduler** (`scheduler.py`, default off) runs with the
app lifespan: every `schedule.check_minutes` (default 60) it refreshes
scenarios whose newest activity is older than their `schedule.interval_hours`
(global `schedule.enabled` + per-scenario `schedule.enabled` both required;
never queues onto a busy scenario). Status and manual runs are exposed via
`GET /api/rkg/scheduler/status` and `POST /api/rkg/scheduler/tick`.

### Scenario API (`/api/rkg/scenarios*`)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/rkg/scenarios` | list scenarios + live status |
| `GET /api/rkg/scenarios/{sid}` | scenario detail |
| `GET /api/rkg/scenarios/{sid}/status` | live loop status |
| `GET /api/rkg/scenarios/{sid}/report` | best domain report (markdown) |
| `GET /api/rkg/scenarios/{sid}/gaps` | ranked research-gap suggestions (type, evidence, hypothesis, arXiv query) |
| `POST /api/rkg/scenarios/{sid}/build` | phase 1 (job) |
| `POST /api/rkg/scenarios/{sid}/synthesize` | phase 2 / 4 (job) |
| `POST /api/rkg/scenarios/{sid}/experiments` | phase 3 (job) |
| `POST /api/rkg/scenarios/{sid}/loop` | full chained loop (job) |

`ResearchWorkbench.gaps(sid)` inspects the graph for research gaps — untouched
pool topics (zero imported corpus papers), papers with no concept edges,
orphan concepts, and corpus papers without an experiment spec — and returns
each suggestion with a candidate hypothesis and a ready-to-run arXiv query.

### Agent ↔ RKG bridge

The chat agent can use the graph via `rkg__*` tools (`backend/agents/tools.py`):
`rkg__query_rag`, `rkg__paper_notes`, `rkg__scenario_status`,
`rkg__scenario_report`. They resolve the **same** Organizer/Workbench
singletons as the router, so the agent and dashboard share one corpus and
graph; if RKG is unavailable the tools return a clear `[error] RKG
unavailable: …` message instead of crashing the turn.

## Dependencies

Added to `pyproject.toml` / `requirements.txt`: `arxiv`, `PyMuPDF`, `requests`,
`PyYAML` (numpy was already present). GPU stats use `nvidia-smi` (optional).

## Example

```bash
uvicorn backend.main:app --port 8765
# open http://localhost:8765 → "Knowledge Graphs" tab
#   or directly http://localhost:8765/rkg/dashboard
#   or http://localhost:8765/rkg/landscape
```
