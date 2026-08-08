# Knowledge graphs (RKG)

The **Research Knowledge Graph** subsystem ingests arXiv/domain papers, builds a
knowledge graph + RAG index, and exposes them to the agent and the UI.

## The 🕸 Graphs tab

- **Pool** — papers + topics.
- **Graph** — the knowledge graph (nodes/edges) with search.
- **Scenarios** — domain research scenarios with scheduler-driven synthesis
  (corpus → synthesis → replication experiments → fold-back report).
- **Jobs** — background ingestion/synthesis jobs.

## Agent tools

The agent can query literature via `rkg__query_rag`, `rkg__paper_notes`,
`rkg__scenario_status`, and `rkg__scenario_report` — grounding answers and
experiments in published work.

## Literature grounding (round 11)

Planning, review, and reports proactively consult the RKG:

- **Campaign planner** includes *Related work* for the research question.
- **Reviewer** appends related-work for the experiment's question (to flag
  unsupported claims and avoid re-trying known results).
- **Project report** gains a *Related work* section (top corpus sources).

All of it is best-effort: when the corpus is empty or the RKG is unreachable,
everything runs unchanged.

## Scheduling

The RKG scheduler (enabled in config) periodically ingests configured papers and
synthesizes scenario reports, so the corpus grows on its own.
