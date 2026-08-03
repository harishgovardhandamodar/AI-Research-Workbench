# arXiv → Experiment Replication (and how to trigger it locally)

This guide explains how the workbench turns an arXiv paper into a
replication-ready research workflow, and how to trigger it locally — using
**https://arxiv.org/pdf/2409.12642** ("Deep generative models as an adversarial
attack strategy for tabular machine learning", Dyrmishi et al. 2024) as the
worked example.

Everything runs locally or under explicit user approval (network/PDF downloads
are permission-gated).

---

## 1. The pipeline

```
arXiv ID / URL  or  local PDF
   │  arxiv__ingest_arxiv_paper          (downloads metadata + PDF)
   ▼
Paper record + PDF  ->  /app/workbench/papers/<id>/
   │  arxiv__extract_paper_text          (PyMuPDF)
   ▼
Plain text
   │  arxiv__extract_structured_notes    (JSON schema the LLM fills)
   ▼
Structured notes (methods, datasets, metrics, reported results, claims)
   ├─► arxiv__summarize_paper
   ├─► arxiv__build_knowledge_graph_from_notes   -> Knowledge Graph
   ├─► arxiv__merge_knowledge_graphs             -> corpus graph (multi-paper)
   ├─► arxiv__craft_experiment_from_notes        -> experiment spec
   │        │
   │        ▼
   │   local run (workbench kernel / robustness MCP tools)
   │        │
   │        ▼
   │   own results
   │        │
   └─► arxiv__compare_results(authors, own)      -> match / discrepancy
        │
        ▼
   arxiv__prepare_replication_report             -> provenance-linked report
   arxiv__query_knowledge_graph / graphrag__graphrag_retrieve  -> retrieval
```

Every intermediate result (metadata, text, notes, graph, spec, metrics,
report) is stored as a first-class **Artifact** with provenance.

**MCP servers involved** (all live by default):

| Server | Tools |
|--------|-------|
| `arxiv` | `ingest_arxiv_paper`, `extract_paper_text`, `extract_structured_notes`, `summarize_paper`, `craft_experiment_from_notes`, `compare_results`, `prepare_replication_report`, `build/query/merge/export_knowledge_graph` |
| `robustness` | `evaluate_sklearn_robustness`, `robustness_metrics_from_predictions`, `adversarial_robustness_checklist`, `simple_fgsm_perturbation` |
| `graphrag` | `graphrag_retrieve`, `graphrag_answer_prompt` |

---

## 2. Triggering it locally from the shared paper

### Path A — chat prompt (agent runs the whole loop)

Paste into the chat window (replace `Qwen`/model as configured):

> "Replicate arXiv 2409.12642 (https://arxiv.org/pdf/2409.12642):
> ingest the paper, extract and summarize it, fill the structured notes schema,
> build a knowledge graph, craft an experiment spec, run a local simplified
> adversarial attack, compare my ASR with the authors' reported ~95% ASR, and
> produce a replication report + knowledge graph as artifacts."

The agent will call `arxiv__ingest_arxiv_paper`, `arxiv__extract_paper_text`,
`arxiv__build_knowledge_graph_from_notes`, `arxiv__craft_experiment_from_notes`,
then `run_python` for the local attack and `arxiv__compare_results` +
`arxiv__prepare_replication_report`.

Smaller steps if you prefer to drive it yourself:

> "Run arxiv__ingest_arxiv_paper on https://arxiv.org/pdf/2409.12642 and save
> the knowledge graph artifact."

> "Run arxiv__compare_results with author ASR 0.95 against my local ASR 0.77."

### Path B — run the bundled local experiment script

A self-contained local replication of 2409.12642 ships at
`examples/arxiv/run_local_replication.py`. It:

1. prints the adversarial-robustness checklist (robustness MCP),
2. builds a synthetic tabular task + logistic classifier (a tractable proxy for
   the paper's DGM attack),
3. runs a **PGD attack** and measures **ASR vs ε** (robustness MCP),
4. compares the local ASR against the authors' reported ~95% with
   `arxiv__compare_results`,
5. prints the verdict.

Run it standalone:

```bash
.venv/bin/python examples/arxiv/run_local_replication.py
```

Or inside the workbench kernel (ask the agent, or exec it):

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
exec(open("examples/arxiv/run_local_replication.py").read())
```

Expected output (deterministic): clean accuracy ~0.99, ASR growing with ε
(≈0.06 at ε=0.5 → ≈0.77 at ε=2.0), and a comparison verdict of **match**
(relative difference ≈19% < the 20% tolerance) against the paper's 0.95.

### Path C — inspect the knowledge graph + GraphRAG afterwards

After ingestion, the paper is a queryable graph. Ask the agent:

> "Run arxiv__query_knowledge_graph (datasets) and (metrics) on the knowledge
> graph for 2409.12642."

> "Run graphrag__graphrag_retrieve on the 2409.12642 knowledge graph for
> 'which dataset and metric relate to the adversarial attack?', then
> graphrag__graphrag_answer_prompt."

---

## 3. Where the data lives

- Ingested papers: `/app/workbench/papers/<arxiv_id>/` (metadata.json, PDF, .txt)
- Knowledge graphs, notes, reports: saved as **Artifacts** (Artifacts panel)
- All read-only tools run freely; `ingest_arxiv_paper`, `extract_paper_text` and
  `export_knowledge_graph` (with `output_path`) ask for approval.

---

## 4. Limitations (be explicit)

- **2409.12642 trains tabular DGMs** (WGAN/TableGAN/CTGAN/OCT-GAN). The local
  replication is a **tractable proxy** (logistic regression + PGD on a synthetic
  tabular task) — it demonstrates the *methodology* (ingest → replicate →
  compare → report), not a faithful reproduction of the paper's numbers.
- Full reproduction needs the paper's datasets (URL, WiDS, HELOC, Credit),
  hyperparameters and seeds, which the paper does not fully disclose.
- `compare_results` is a quantitative check with a configurable tolerance — a
  "match" does **not** validate the methodology, only that numbers are close.
- FGSM/PGD are white-box attacks; the paper's DGM-based attacks are a different
  threat model.

---

## 5. Extending

- Multi-paper: `arxiv__merge_knowledge_graphs([graph1, graph2, ...])` builds a
  corpus graph, then `graphrag__graphrag_retrieve` answers cross-paper
  relational questions.
- Export for external tools: `arxiv__export_knowledge_graph(..., format=
  "cypher_snippets" | "json" | "triples")`.
