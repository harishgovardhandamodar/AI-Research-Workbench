# Literature grounding

Planning, review, and reporting consult the **Research Knowledge Graph (RKG)**
RAG index so autonomous research is grounded in published work — not just local
trial and error.

## The shared helper

`literature_context(question)` queries the RKG RAG and returns a concise
**Related work** block: the answer summary + top `[arXiv:…]` source ids/titles.
It is best-effort and injectable: when the corpus is empty or the RKG is
unreachable, callers get `""` and run unchanged.

## Where it's used

- **Campaign planner** — the planning prompt includes related work for the
  research question, so steps build on published findings.
- **Reviewer** — the review prompt gets related work for the experiment's
  question, helping flag unsupported claims and suggesting grounded next steps.
- **Project report** — the report gains a **Related work** section (based on the
  latest campaign's research question / latest experiment's hypothesis).

## Reading

- `project_question(rt)` derives a sensible project-level question (latest
  campaign's research question, else latest experiment's hypothesis).
- Related work is always optional context; it never blocks execution.
