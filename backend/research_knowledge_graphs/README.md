# Research Knowledge Graphs

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
