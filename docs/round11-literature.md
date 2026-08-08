# Round 11 — Literature-grounded research

The workbench already ships a Research Knowledge Graph (RKG) subsystem — an
arXiv/domain corpus with a RAG index — exposed to the agent as `rkg__*` tools.
But planning, review, and reporting never *proactively* consult it. Round 11
makes every layer literature-aware: campaign plans, reviewer suggestions, and
the project report all ground themselves in the corpus (best-effort, graceful
when the RKG is empty or unreachable).

## Design

### 1. `backend/literature.py` — `literature_context(rt, question, limit)`
A shared async helper that queries the RKG RAG (`org.query_rag`) in a thread and
formats a concise **Related work** block: the answer summary + top source ids
(`[arXiv:…] title`). Returns `""` when RKG is unavailable or empty. The RAG
lookup is injectable (a `query_rag` callable) so tests can stub it.

### 2. Campaign planning (`backend/campaign.py`)
`_plan_campaign` queries `literature_context` for the campaign's research
question and includes it in the planning prompt, so steps build on published
work instead of starting from scratch.

### 3. Reviewer (`backend/agents/reviewer.py`)
`Reviewer.review` fetches a Related-work block for the owning experiment's
question/goal and appends it to the review prompt, so suggestions can
reference or check the literature and flag claims not supported by the corpus.

### 4. Project report (`backend/report.py`)
`build_project_report` gains a **## Related work** section (based on the most
recent campaign's research question / latest experiment's hypothesis), listing
the top corpus sources.

## Files touched
- `backend/literature.py` (new), `backend/campaign.py`,
  `backend/agents/reviewer.py`, `backend/report.py`,
  `docs/round11-literature.md`, `tests/test_round11.py`.

## Out of scope
- Ingesting/curating the corpus (existing RKG scheduler handles that).
- Full citation-graph rendering in reports (source ids + titles only).
