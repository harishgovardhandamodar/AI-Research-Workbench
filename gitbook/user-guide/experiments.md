# Experiments tab

The 🧪 **Experiments** tab is the tracking home. It is organized into sections
with a sticky nav (Overview · Goals · Chart · Experiments · Campaigns ·
Benchmarks · Runs).

![Experiments overview](../assets/screenshots/experiments.png)

## Overview

- **KPI cards** — experiments, runs, campaigns, benchmarks, learnings, open
  goals. Click a card to jump to its section.
- **Leaderboard** — cross-experiment comparison (best goal value, Δ vs best,
  % of target, status). Click a row to expand + scroll to that experiment.

## Goals

Add target metrics (project-wide or scoped to an experiment). Each goal shows a
**progress bar** (best vs target), a ✓ reached state, and the delta to go.
Progress is checked after every run.

## Chart (Evolution)

The **Timeline** (run evolution) and **Graph** (similarity edges) SVG views:
- Select a metric; filter by run kind (agent/sweep/campaign/notebook/eval/…).
- Drag to pan; use the **+ / − / ⊙** controls to zoom (scroll-zoom is disabled).
- Hover a node for a tooltip; click to open the run detail panel with
  **Improve from here** and **Compare vs best**.
- **Branches** opens the git-flow branch history overlay (parent→child lineage).

![Experiment tracking cockpit](../assets/screenshots/experiment-tracking.png)
![Experiment timeline](../assets/screenshots/experiment-timeline.png)
![Experiment graph](../assets/screenshots/experiment-graph.png)

## Experiments

Each card shows: name (click → **detail modal**), status badge, ✓ reached, run
count, goal + best, a **trend sparkline** of the goal metric across runs with a
**Δ best** chip, and a progress bar. Actions: ★ focus, ✎ edit, status select,
**🔁 Improve**, **⬇ export** (this experiment's runs as CSV), and **Details**
(hypothesis, model pin, plan, learnings, leaderboard). Search + sort (recently
active / best / name / runs). Large lists render in chunks with a **Show more**
control.

![Experiment detail](../assets/screenshots/experiment-detail.png)

## Campaigns & Benchmarks

Background research campaigns and model benchmarks with create/run/resume/stop,
status badges, and progress bars (see their dedicated pages).

## Runs

Every recorded turn, filterable by experiment and text. Click a run to expand:
metrics (goal ★ highlighted), config, tool trail, full prompt, and actions
(**Report**, **↶ revert**, **🔁 improve**). Each row shows its goal-metric value
with a **Δ best** chip, and **⬇ CSV** exports the filtered list.

![Run detail](../assets/screenshots/run-detail.png)

## Run comparison

Pick two runs (⇄ Compare) to see metric deltas (A → B), a **verdict** on the
owning experiment's goal metric, side-by-side **config** (changes highlighted),
**tool trails**, and a summary of improved/worsened/unchanged metrics.

## Shortcuts & deep links

- **Keyboard**: `O` overview · `G` goals · `C` chart · `X` experiments · `M`
  campaigns · `B` benchmarks · `R` runs (ignored while typing).
- **Shareable URLs**: the active section and expanded run are recorded in the
  URL (`?view=experiments&section=runs&run=123`), so a refresh or a pasted link
  lands on the exact place in the tab.
