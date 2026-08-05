# FOX AI-Research-Workbench — TO-DO List (Research-first)

Trackable roadmap toward a **Research-first autonomous autoresearch workbench**:
the knowledge graph drives the agent, the agent closes the loop back into the
graph, and long-running research is automated, verifiable and quantifiable.

Rules: each unchecked item below is a unit of work. Implement it, verify it
(py_compile, `unittest`, curl where relevant), **commit it on its own**, then
check the box.

Branch: `research-first` (base `1285eaf`).

---

## Diagnosis (why this roadmap)

Two siloed systems coexist:

1. **Per-project agent workbench** — chat agent + kernel + experiments +
   artifacts (great traceability, no external literature grounding).
2. **Research Knowledge Graph (RKG)** — arXiv corpus, typed graph, RAG,
   scenario autoresearch loops (great literature coverage, but the chat agent
   cannot see it and cannot act on it).

Consequences observed:

- **No bridge**: the agent has no RKG/arxiv tools; the RKG scenarios run in
  isolation from the user's live experiments.
- **No scheduler**: nothing re-runs corpus build / synthesis on a cadence; the
  graph goes stale.
- **No hypothesis layer**: nothing proposes "here are open gaps / next
  questions" from the graph.
- **Loop quality gaps**:
  - Synthesis reviews are single, subjective and stop only after `max_iters`
    (a real run produced 98 → 97/96/97/96 — 4 wasted iterations).
  - Citations are not verified against the corpus.
  - Replication experiments use single runs and don't compare against the
    paper-reported metric.
  - Fold-back is only qualitative — no replication results table lands in the
    report.
- **No resilience**: `_jobs` (router.py) and `_live` (research_loop.py) are
  in-memory; a restart orphans running jobs and loses progress; two long
  scenario ops on the same scenario can run concurrently.
- **No mocked-LLM tests** for the loop phases (only read-only smoke tests).

---

## Phase A — Unify (foundation)

- [x] **A1 · Agent↔RKG bridge: `rkg__*` tools for the agent**
  - New tools registered in `backend/agents/tools.py` (and schemas exposed via
    `get_tool_schemas`): `rkg__query_rag(question)`, `rkg__paper_notes(paper_id)`,
    `rkg__scenario_status(scenario_id)`, `rkg__scenario_report(scenario_id)`.
  - They resolve the **shared** RKG Organizer/Workbench (same lazily-built
    singleton as the router) so the chat agent and the dashboard share one KG.
  - Graceful when RKG is not initialized (return a clear message, never crash
    the turn).
  - Verify: unit test that a fake `get_workbench`/`get_org` is called and the
    tool returns its payload; full suite green.

- [x] **A2 · Persistent + resumable jobs & per-scenario guard**
  - Persist `_jobs` (router.py) to disk (`<data_root>/jobs.json`); on startup,
    restore the registry and mark `running` jobs as `interrupted`.
  - Persist scenario `_live` progress to `scenario.json` so polling survives
    restarts and phases resume from a recorded state.
  - Per-scenario concurrency guard: reject a new `build/synthesize/experiments/
    loop` job on a scenario that already has a `running` job.
  - Verify: router unit tests (jobs persisted → reloaded, `running` →
    `interrupted`, concurrent submit refused).

## Phase B — Automate (scheduler + gaps)

- [x] **B1 · Scenario scheduler (per-scenario cadence)**
  - Background asyncio task started with the app lifespan: every N minutes
    (configurable, default e.g. 60), check each scenario's `last_built_at` /
    `last_loop`; if older than the scenario's `schedule.interval_hours`
    (default off unless configured), trigger `build_corpus` → `run_synthesis`.
  - Respects the per-scenario guard from A2 (never queues onto a running job).
  - Config: `schedule` block in the RKG `config.yaml` + per-scenario
    `scenario.json` `schedule: {interval_hours, enabled}`.
  - Verify: scheduler unit test with a fake workbench (cadence math, guard
    interaction); `py_compile`.

- [x] **B2 · Gap discovery → hypotheses/topics**
  - New `ResearchWorkbench.gaps(sid)` that inspects the graph for research
    gaps: pool topics with no imported corpus papers, orphan concepts, papers
    not linked to the scenario's lens concepts, RAG chunks with no experiment.
  - Returns ranked suggestions (each: gap type, evidence, suggested hypothesis,
    suggested arXiv query) for the dashboard/agent to act on.
  - Verify: unit test with a small fake graph produces expected gap types.

## Phase C — Research quality loop

- [x] **C1 · Verifiable synthesis: plateau early-stop + citation audit**
  - `run_synthesis` stops early when (a) score reaches `review_target`, or
    (b) no improvement for `plateau_iters` consecutive iterations (default 2).
  - Every `[arXiv:xxxx]` citation in the accepted report is checked against the
    corpus; ungrounded citations are removed and logged, and the report gains a
    "References audit" note with verified/failed counts.
  - Verify: mocked-LLM unit test (scores 98→97→96 → stops after plateau, best
    kept; fake citation dropped, real citation retained).

- [x] **C2 · Rigorous replication: multi-run mean + delta vs paper**
  - `_improve_experiment` runs each candidate `num_runs` times (default 3) and
    keeps on the **mean** metric (not a single run); records mean/stdev.
  - Result records `delta_vs_paper` when a paper-reported value is extractable
    from the experiment spec; written into `results.md`.
  - Verify: mocked run-research-experiment test (keep/revert on means, delta
    computed).

- [x] **C3 · Quantitative fold-back: replication table in report**
  - After Phase 3, the fold-back synthesis (`include_experiments=True`) is fed
    a **replication results table** (paper, metric, paper-reported, our value,
    delta, status kept/reverted) and the final report embeds it as a section.
  - Verify: mocked-LLM test asserting the table is present in the corpus
    context and survives into the accepted report.

## Phase D — Governance, UX, tests

- [x] **D1 · Mocked-LLM loop tests**
  - `tests/test_research_loop.py`: fake LLM driving `run_synthesis` (plateau
    early-stop, citation audit), `run_experiments` (keep/revert), and
    `run_full_loop` phase chaining without Ollama.
  - Verify: `python -m unittest tests.test_research_loop -v` green.

---

## Notes

- All verification is local-first: `py_compile`, `.venv/bin/python -m unittest
  discover -s tests`, curl against a running server where relevant.
- The RKG singleton is created lazily; keep it that way (never block server
  startup on Ollama/GPU probing).
- Live KG stats used for sanity checks: 70 papers / 84 concepts / 183 relations.
- Scenario `autonomous-agents-security` real run: best `report_score` 98.0
  after 5 iterations (98 kept, then 97/96/97/96 discarded) — motivates C1.
- Deploy fast-loop: `docker compose cp <file> fox:/app/<file>` then
  `docker compose restart fox`; full image rebuild via
  `docker compose up -d --build`. Bump `FOX_VER` on frontend changes.
