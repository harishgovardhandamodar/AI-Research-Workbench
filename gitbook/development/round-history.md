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
| 22 | Dataset comparison | Runs carry a `dataset` tag (`report_dataset("real"/"synthetic")` kernel helper, config fallback, editable per-run); Datasets section groups an experiment's runs by dataset with per-dataset stats, a between-dataset metric matrix (best dataset per metric ★), and per-dataset run lists |
| 23 | Horizontal experiment slider | The Experiments list is a horizontally scrollable slider (‹ › arrows, all experiments rendered, no chunk paging), so old experiments are always reachable; reveal scrolls the strip to center the card |
| 24 | Show every run | The graph/timeline and branch endpoints no longer cap at the 50-run default (`list_runs(limit=None)`), so the chart, cards and runs list agree on the real run count (e.g. 93) instead of truncating to 50 |
| 25 | Tool-step budget & continue | `agent.max_iters` (tool steps per turn) is editable in Settings and takes effect immediately; when a turn hits the limit the chat shows a one-click **Continue** button |
| 26 | Pipeline view | Every assistant turn gets a 🛠 **Pipeline** block in the chat (and each run in the experiment detail modal) summarizing the whole run: experiment + goal, strategy, data steps, the ordered tool actions with code snippets, and finally the hyperparameters / model config + metrics — also re-attached to older messages after runs load |
| 27 | Pipeline view — researcher deep-dive | The pipeline block now is a full researcher workbench per run: ⏱ run duration, per-step ⌨ code disclosure (lazy-loads the exact executed code, index-aligned with the tool trail), per-step duration + result disclosure, a ⇄ compare-vs-parent diff (config · tools · metrics) for improve loops, produced-artifact chips, ✓ goal-reached badge, and a ⧉ copy-pipeline-as-markdown export for reports; `include_code` flag on the single-run endpoint |
| 28 | Research advisor | Every experiment gets a deterministic 🧭 **advisor** (in the experiment detail modal + a chat health strip) that surfaces: 🎯 goal proposal & alignment (proposes a goal metric/target from the measured data when missing, progress-to-target bar), 🧩 missing elements checklist (hypothesis, target, plan, model pin, dataset tags, metrics, learnings), 🔬 areas of improvement grouped by typed suggestion category (🔧 hyperparameter · 🧬 data · 🧠 model · 🧪 method · 🎓 finetune · 📊 eval · 🔒 reproducibility), 🔧 suggested hyperparameters from the best config + hyperparameter suggestions, 🧬 data pipeline (datasets + tools), 🤖 model selection (pinned vs used), and 🎓 finetune readiness checklist; reviewer suggestions are now typed (`category`) with keyword fallback for legacy rows |
| 29 | Sweep UI + finetune launch | A **Sweep & Finetune** section in the Experiments tab: the **parameter sweep** composer builds a config grid (grid-search cartesian product or explicit JSON points) + code that reads `config` and reports metrics, and launches via a deterministic `run_sweep` intent (no LLM round-trip) that reuses the parallel-kernel sweep machinery and records one run per point; the **finetune launch** flow builds a finetune config (base model, dataset, epochs/lr/batch/LoRA rank), records a `kind="finetune"` run with a generated HuggingFace training script, and adds a `run_finetune` tool for the agent; both stream through chat so the pipeline view captures the launch |
| + | Tooling | VS Code extension (experiment tracking + documentation) |

## Design docs

Per-round plans live in `docs/` (e.g. `goal-first-experimentation.md`,
`round3-close-the-loop.md`, …, `round13-experiments-ux.md`,
`vscode-extension.md`). `docs/langchain-orchestration-plan.md` covers the
optional LangGraph orchestrator.
