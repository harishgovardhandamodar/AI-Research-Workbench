# Knowledge Graphs & Research Workbench

The workbench turns a raw arXiv paper into a typed knowledge graph, a RAG
searchable vault, and — with the **Research Workbench** — a fully autonomous
per-domain research loop that builds corpora, synthesizes literature reports,
and runs replication experiments.

Everything lives in `backend/research_knowledge_graphs/` and is exposed through
a single FastAPI router namespaced at `/api/rkg/*`, with two SPA views served at
`/rkg/dashboard` and `/rkg/landscape`. No ingested papers, graphs, or notes are
committed to the repo — all runtime data lands under
`<FOX_WORKBENCH_DIR>/research_knowledge_graphs/` (gitignored).

---

## 1. Architecture overview

```mermaid
flowchart LR
    subgraph Browser
        UI[Knowledge Graphs tab<br/>(iframe of /rkg/dashboard)]
    end

    subgraph Backend [FastAPI app]
        R[router.py<br/>/api/rkg/* + /rkg/* views]
        J[Job registry<br/>background threads + polling]
        WB[Research Workbench<br/>research_loop.py]
    end

    subgraph Research [research_knowledge_graphs modules]
        O[Organizer<br/>orchestrator]
        P[PaperPipeline<br/>pipeline.py]
        KG[(KnowledgeGraph<br/>Hive typed graph)]
        POOL[(ResearchPool<br/>SQLite topic monitor)]
        RAG[RAGEngine<br/>embeddings + cosine]
        LLM[LLMInterface<br/>Ollama]
        W[WebIngester]
        S[Similarity]
        GPU[GPUManager<br/>nvidia-smi]
    end

    subgraph Storage [gitignored data root]
        PAPERS[(papers/ PDFs)]
        VAULT[(vault/ markdown notes + figures)]
        GRAPH[(graph/ JSON)]
        SC[(scenarios/ config + reports)]
    end

    subgraph External
        ARXIV[(arXiv API + PDFs)]
        OLLAMA[(Ollama<br/>chat + embeddings)]
    end

    UI --> R
    R --> J
    R --> WB
    R --> O
    O --> P --> KG
    O --> RAG
    O --> POOL
    O --> W
    O --> S
    O --> LLM
    LLM --> OLLAMA
    P --> ARXIV
    POOL --> ARXIV
    KG --> GRAPH
    P --> PAPERS
    P --> VAULT
    WB --> SC
    GPU --> OLLAMA
```

**Request flow:** the dashboard iframe calls the namespaced REST API. Read-only
endpoints call the `Organizer` directly (heavy blocking work via
`asyncio.to_thread`). Long operations (paper ingestion, the research loop
phases) are submitted to the **job registry**, which returns a job id
immediately; the dashboard polls `GET /api/rkg/jobs/{id}` and the per-scenario
status endpoint until the work completes.

---

## 2. Data model

A **Hive knowledge graph** (`hive_datatype.py`) holds typed `Node`s and directed
`Edge`s, persisted as JSON (`graph/main.json`). Each node also owns a
**vault note** — a human-readable markdown file that is the audit trail of the
LLM analysis.

```mermaid
classDiagram
    class HiveGraph {
        +nodes: list~Node~
        +edges: list~Edge~
        +stats() HiveStats
    }
    class Node {
        +id: str
        +label: str
        +type: NodeType
        +abstract: str
        +authors: str
        +published: str
        +definition: str
        +is_paper() bool
        +to_dict() dict
    }
    class Edge {
        +source: str
        +target: str
        +relation: str
        +cross_graph: bool
    }
    class NodeType {
        PAPER
        CONCEPT
        WEB
    }
    class KnowledgeGraph {
        +add_paper() Node
        +add_concept() Node
        +add_edge()
        +get_paper() Node
        +to_node_link() dict
        +save()
    }

    HiveGraph "1" --> "*" Node
    HiveGraph "1" --> "*" Edge
    KnowledgeGraph --> HiveGraph : wraps
    Node --> NodeType
```

```mermaid
flowchart LR
    P1["Paper node<br/>arXiv:id"] -->|related_to| C1["Concept node<br/>e.g. prompt-injection"]
    P1 -->|cites| P2["Paper node<br/>lineage reference"]
    P1 -->|related_to| T1["Tag node"]
    W1["Web node<br/>ingested URL"] -->|related_to| C2["Concept node"]
    P2 -->|related_to| C1
```

Node types and edge semantics:

| Node / edge | Meaning |
|---|---|
| `PAPER` | an ingested (or lineage-fetched) arXiv paper |
| `CONCEPT` | an extracted domain concept with a definition |
| `WEB` | a web page ingested via URL |
| `related_to` | paper ⇄ tag / concept association |
| `cites` | paper → referenced arXiv paper (lineage) |

---

## 3. Paper ingestion pipeline

The path from an arXiv id to a fully analyzed, graph-linked paper:

```mermaid
flowchart TD
    A[arXiv id / URL] --> B[arxiv_fetcher.py<br/>metadata + PDF download]
    B --> C[parser.py<br/>PyMuPDF: text + figures + references]
    C --> D{PDF available?}
    D -- yes --> E[LLM analysis of full text]
    D -- no --> F[LLM analysis of abstract]
    E --> G[tags/concepts/relations/summary]
    F --> G
    G --> H1[add paper node]
    G --> H2[add concept nodes + related_to edges]
    G --> H3[write vault note 00_notes.md]
    G --> H4[write per-experiment files]
    C --> I[parser: referenced arXiv IDs]
    I --> J[lineage fetch: cites edges to prior papers]
    E --> K[figures extracted to vault/figures]
    H1 --> L[graph/main.json]
    H2 --> L
    J --> L
    H3 --> VAULT[(vault/)]
    K --> VAULT
    H4 --> VAULT
```

Per paper, the LLM extracts:

- **tags** — short keywords (fast model)
- **summary** + **notes** — method, architecture, results with numbers
- **experiments[]** — structured experiment specs (goal, methodology, dataset,
  setup, baselines, metrics, results, findings) written to
  `*-00-experiment.md` files
- **concepts[] / relations[]** — graph primitives with definitions
- **lineage_notes** — prior work and how the paper differs

---

## 4. Research pool (arXiv topic monitor)

`pool.py` maintains a SQLite database of **topics** (name → arXiv query). Every
12h (and on demand) it searches arXiv for each topic, records every observed
paper with its topics, and marks which papers have been imported into the
knowledge graph. The dashboard's Pool view lets you browse candidates and
import them in bulk.

```mermaid
flowchart LR
    subgraph Pool [ResearchPool SQLite]
        T[(topics)]
        P[(papers: observed + imported flag)]
        C[(cache)]
    end
    T -->|search_arxiv| ARXIV[(arXiv API)]
    ARXIV -->|records| P
    P -->|mark_imported| P
    P -->|candidates| UI[Pool panel]
    UI -->|import / import_batch| J[background job]
    J --> PIPELINE[PaperPipeline]
```

Topics are seeded with general defaults plus the scenario topics of the two
sample Research Workbench domains.

---

## 5. RAG search

Each imported paper's full text is chunked, embedded with
`nomic-embed-text` (via Ollama), and stored in an in-memory vector index. A
question is embedded and answered over the top-k chunks with citations:

```mermaid
flowchart LR
    PDF[PDF text] -->|rag.py chunk| CH[chunks 512/64 overlap]
    CH -->|embed| EMB[(embeddings 768-d)]
    Q[question] -->|embed| QE[query vector]
    EMB -->|cosine top-k| TOP[top-k chunks]
    TOP -->|LLM answer with citations| A[answer]
```

---

## 6. Research Workbench — autoresearch loops

The flagship: a **scenario** is a research domain (arXiv topics + a research
lens). Two sample scenarios ship with the tool:

| Scenario id | Name | Lens |
|---|---|---|
| `autonomous-agents-security` | Autonomous Agents & Security Lapses | security lapses of autonomous agents |
| `enterprise-ai-security` | Enterprise AI Adoption & Security Lapses | AI adoption in the enterprise + production security lapses |

The chained loop (`research_loop.py`) runs four phases over the corpus:

```mermaid
flowchart TD
    subgraph Phase1 [1. Build corpus]
        A1[seed scenario topics into pool]
        A2[refresh pool from arXiv]
        A3[rank un-imported candidates by recency]
        A4[import top-N -> knowledge graph + vault]
    end
    subgraph Phase2 [2. Synthesize domain report]
        B1[build corpus context: papers, concepts, tags, notes]
        B2[LLM writes report with citations]
        B3[LLM reviewer scores 0-100]
        B4[iterate: improve -> re-score -> keep best]
    end
    subgraph Phase3 [3. Replication experiments]
        C1[rank papers by graph degree + recency]
        C2[extract experiment specs from vault]
        C3[LLM writes runnable experiment.py]
        C4[improve loop: run under budget, keep metric wins]
        C5[append results to vault note + results.md]
    end
    subgraph Phase4 [4. Fold back]
        D1[re-synthesize report with experiment results]
    end

    A1 --> A2 --> A3 --> A4
    A4 --> B1 --> B2 --> B3 --> B4
    B4 --> C1 --> C2 --> C3 --> C4 --> C5
    C5 --> D1
    D1 --> R[(report.md + scenario.json)]
```

**The synthesis loop in detail** — the research analog of the classic
`experiment.py` autoresearch loop: the *artifact under improvement* is the
domain report, and the *metric* is the LLM reviewer's 0–100 score:

```mermaid
flowchart LR
    R[report.md] -->|improve prompt| LLM[Ollama large model]
    LLM -->|revised report| SCORE[reviewer scores 0-100]
    SCORE --> KEEP{score >= best?}
    KEEP -- yes --> BEST[(best report)]
    KEEP -- no --> DISCARD[revert to best]
    BEST --> R
    BEST -->|loop| LLM
```

**The replication improve loop** — per chosen paper, the classic metric
keep/revert loop over `experiment.py`:

```mermaid
flowchart TD
    SPEC[experiment spec from vault] -->|LLM| CODE[experiment.py]
    CODE --> RUN[run under fixed budget]
    RUN --> METRIC{METRIC line?}
    METRIC -- yes --> IMP{improved?}
    IMP -- yes --> KEEP[keep change, record best]
    IMP -- no --> REV[revert to snapshot]
    KEEP --> ITER{iterations left?}
    REV --> ITER
    ITER -- yes --> PROPOSE[LLM proposes one change]
    PROPOSE --> RUN
    ITER -- no --> OUT[results.md + vault note + project store run]
```

Every loop run is recorded in the scenario's **project store**
(`scenarios/<id>/project/workbench.db`) as runs attached to a per-scenario
experiment — so the Research Workbench results appear in the workbench's own
experiment timeline.

Per-scenario state under `<root>/scenarios/<scenario_id>/`:

```
scenario.json     config + corpus paper ids + best score + last loop results
report.md         best domain report (markdown)
log.md            append-only research log
experiments/      per-paper experiment.py + results.md
project/          ProjectStore DB (runs, messages, experiments)
```

---

## 7. Background jobs

Paper ingestion and the research-loop phases take minutes (arXiv + multiple LLM
calls + embeddings). Holding a browser fetch open that long fails with a
`NetworkError`, so long endpoints submit work to the **job registry** instead:

```mermaid
sequenceDiagram
    participant UI as Dashboard (JS)
    participant API as /api/rkg (FastAPI)
    participant JB as Job registry
    participant LLM as Ollama / arXiv

    UI->>API: POST /scenarios/{sid}/loop
    API->>JB: submit(run_full_loop)
    JB-->>UI: {"id":"abc…","status":"running"} (instant)
    loop every ~4s
        UI->>API: GET /jobs/abc…
        API->>JB: status
        JB-->>UI: {"status":"running","progress":0.4,…}
    end
    loop phases run in a worker thread
        JB->>LLM: synthesize / replicate / embed
    end
    JB-->>UI: {"status":"done","result":{…}}
    UI->>API: GET /scenarios/{sid}/status (final phase + log tail)
```

---

## 8. API reference (`/api/rkg/*`)

### Research pool & papers

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/rkg/pool` | GET | pool feed (cached topic → papers) |
| `/api/rkg/pool/topics` | GET | list monitor topics |
| `/api/rkg/pool/topics/add` · `/remove` | POST | manage topics |
| `/api/rkg/pool/papers` · `/graph` | GET | observed papers / similarity graph |
| `/api/rkg/pool/import` · `/import_batch` | POST | import one / many (job) |
| `/api/rkg/papers` · `/papers/search` | GET | graph papers |
| `/api/rkg/add` | POST | add a paper by arXiv id (job) |
| `/api/rkg/search` · `/import` | POST | search arXiv / import all results (job) |

### Graph, browse, retrieval

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/rkg/graph` · `/stats` | GET | node-link graph / counts |
| `/api/rkg/concepts` | GET | concept list |
| `/api/rkg/browse` · `/read` · `/raw` | GET | file tree / file contents |
| `/api/rkg/graph/detail` | POST | LLM-generate edge details (job) |
| `/api/rkg/definitions` | POST | fill concept definitions (job) |
| `/api/rkg/query` | POST | RAG question answering |
| `/api/rkg/lineage` | POST | fetch citations for a paper |
| `/api/rkg/similarity` | GET/POST | paper similarity matrix |
| `/api/rkg/web/add` · `/web/list` | POST/GET | web ingestion |

### Research Workbench scenarios

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/rkg/scenarios` | GET | list scenarios + live status |
| `/api/rkg/scenarios/{sid}` | GET | scenario detail |
| `/api/rkg/scenarios/{sid}/status` | GET | live loop status (phase, progress, log) |
| `/api/rkg/scenarios/{sid}/report` | GET | best domain report (markdown) |
| `/api/rkg/scenarios/{sid}/build` | POST | phase 1 — build corpus (job) |
| `/api/rkg/scenarios/{sid}/synthesize` | POST | phase 2/4 — synthesize report (job) |
| `/api/rkg/scenarios/{sid}/experiments` | POST | phase 3 — replication experiments (job) |
| `/api/rkg/scenarios/{sid}/loop` | POST | full chained loop (job) |

### Jobs & system

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/rkg/jobs/{id}` · `/jobs` | GET | job status / recent jobs |
| `/api/rkg/ollama` · `/gpu` · `/logs` | GET | model/GPU/system status |

---

## 9. Configuration

Runtime data root (default `<FOX_WORKBENCH_DIR>/research_knowledge_graphs/`),
Ollama endpoints, arXiv behavior and GPU settings all come from `config.py`.
Ollama falls back to the workbench's own `CONFIG` LLM wiring, so inside the
Docker image the RKG reaches the same relayed Ollama as the rest of the app
(e.g. `host.docker.internal:11435`).

```yaml
directories:
  root: /path/to/data
  papers: /path/to/data/papers
  graph: /path/to/data/graph
  vault: /path/to/data/vault

arxiv:
  max_results: 10
  download_pdf: true

ollama:
  base_url: http://localhost:11434   # or OLLAMA_BASE_URL env var
  model: qwen3.6:35b                 # or OLLAMA_MODEL
  fast_model: llama3.2:3b            # or OLLAMA_FAST_MODEL
  embed_model: nomic-embed-text      # or OLLAMA_EMBED_MODEL
```

---

## 10. Running it

```bash
# dev server
uvicorn backend.main:app --port 8765

# open the Knowledge Graphs tab (or directly)
#   http://localhost:8765/rkg/dashboard
#   http://localhost:8765/rkg/landscape
```

**Worked example of the Research Workbench:** open the **Research** panel in
the dashboard, pick the *Autonomous Agents & Security Lapses* scenario, and hit
**Full loop**. The loop builds the corpus (~10–15 min: pool refresh + per-paper
LLM analysis), synthesizes a report (auto-scored 0–100, improved each
iteration), replicates the strongest papers' experiments, and folds the results
back into the final report. Watch the live phase/progress/log, then open
**Report** to read the result.
