# Project history (rounds)

The workbench was built in feature rounds, each with a design doc under `docs/`
and a test file under `tests/`. The progression:

| Round | Theme | Key additions |
|---|---|---|
| 1 | Chat UI/UX redesign | Message actions (edit/retry/copy/delete), `@schema`, model-dropdown grouping, debounced streaming, markdown gaps, experiment-context steering, inline next-steps, quick actions, attach |
| 2 | Goal-first experimentation | Objective editing (PATCH), focus experiment (`/focus`), goal-grounded reviewer context, distance-to-target, cross-experiment best, project-data visibility, `/api/models` enrichment |
| 3 | Close the improvement loop | First-class suggestion records + regression check, per-experiment model pinning, run diffs + revert-to-run, workflow-stage retry, parallel parameter sweeps (`run_sweep`) |
| 4 | Reproducibility & provenance | Git-backed run lineage (`git_commit`), full-code capture, per-run env snapshot, run restore, run-diff code sections |
| 5 | Research campaigns | `campaigns`/`campaign_steps`, plan → execute → synthesize, campaign report artifact |
| 6 | Background autonomy & monitoring | Project event bus, background campaign runner (start/stop/recover), campaigns panel + polling |
| 7 | Learnings & knowledge memory | `learnings` table, capture from resolved suggestions, injection into context/reviewer/planner |
| 8 | Verifiable run history | Integrity hashes (`integrity_hash`, verify), run↔trace linkage, per-run audit endpoint |
| 9 | Compare & evaluate | Cross-experiment/campaign leaderboards, N-run comparison, model benchmarks (evals) + background runner |
| 10 | Reports & export | Project report, portable zip export |
| 11 | Literature grounding | RKG RAG in planning/review/report (best-effort) |
| 12 | Resilient & proactive | LLM retry-with-backoff, next-research agenda |
| 13–18 | Experiments-tab UX | Section nav + Overview KPIs, compact cards + progress + sort, theme-aware charts + git-style controls, expandable run detail, navigable KPIs, detail drill-down + tooltips, scroll-zoom disabled |
| 19 | Experiments-tab analytics & ergonomics | Trend sparklines + Δ-vs-best deltas on cards and runs, lazy chunked lists, richer run comparison (goal verdict · config · tool trail), CSV export, shareable deep links, keyboard navigation |
| 20 | Experiments-tab insight & lifecycle | N-way run comparison (best-per-metric), trend stats (μ/σ/slope), goal-reached + campaign/benchmark completion alerts, clickable chart legend + best-fit trend line, time-range filter, run restore + bulk compare/export |
| 21 | Running-experiments indicator | "Running now" strip + pulsing card badge + Running KPI for experiments the agent is actively working on (live turn, running campaign steps, running benchmarks) |
| + | Tooling | VS Code extension (experiment tracking + documentation) |

## Design docs

Per-round plans live in `docs/` (e.g. `goal-first-experimentation.md`,
`round3-close-the-loop.md`, …, `round13-experiments-ux.md`,
`vscode-extension.md`). `docs/langchain-orchestration-plan.md` covers the
optional LangGraph orchestrator.
