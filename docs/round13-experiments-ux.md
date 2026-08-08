# Round 13 — Experiments tab UI/UX overhaul

The Experiments tab grew organically across 12 rounds and is now a **single
long vertical scroll** with no navigation. This round restructures it around
clear sections with a sticky nav, adds an at-a-glance Overview, promotes the
Goals system, and makes lists searchable and cards compact.

## Improvement areas identified

### 1. Navigation & information architecture (highest impact)
- **Problem**: everything is one scroll — chart, experiments, campaigns, compare
  leaderboard, evals, runs, goals are stacked with no way to jump between them.
  The **Goals panel (the objective system) is buried at the very bottom**.
- **Fix**: a **sticky section nav** (Overview · Chart · Experiments · Campaigns ·
  Benchmarks · Runs · Goals) with scroll-to + active highlight
  (IntersectionObserver), and a section reorder so Goals sit near the top.

### 2. No at-a-glance summary
- **Problem**: the first thing you see is a large SVG chart; there are no KPIs,
  and the cross-experiment leaderboard is buried mid-page.
- **Fix**: a new **Overview** section at the top: KPI cards (Experiments, Runs,
  Campaigns, Learnings, open Goals, verified runs) + the compare-experiments
  leaderboard + a ▶ Next shortcut.

### 3. Chart hogs vertical space
- **Problem**: the timeline/graph SVG is ~330–580 px and always expanded.
- **Fix**: collapsible chart header (Expand/Collapse, remembered).

### 4. Bulky experiment cards
- **Problem**: every card shows goal, hypothesis, model pin, plan, learnings,
  and the full ranking table — long with many experiments.
- **Fix**: compact cards — headline (name, status, goal/best, focus/edit/
  improve) always visible; hypothesis/plan/learnings/ranking behind a details
  toggle (collapsed by default).

### 5. No search / filter
- **Problem**: experiments and runs can't be filtered; runs are one flat list.
- **Fix**: a search box for experiments (name/hypothesis/goal) and a filter for
  runs (experiment + text).

### 6. Toolbar clutter
- **Problem**: view toggle, metric select, Compare/Next/Report/Export/Refresh and
  a hint crowd the toolbar.
- **Fix**: group secondary actions (Next · Report · Export) into a labeled
  action group; keep the essential controls up front.

### 7. Inconsistent states
- **Problem**: empty states and section headers are inconsistent; no loading
  feedback on refresh.
- **Fix**: shared empty/loading styling; consistent section headers with counts.

## Files touched
- `frontend/index.html`, `frontend/app.js`, `frontend/styles.css`,
  `docs/round13-experiments-ux.md`, `tests/test_round13.py` (light — JS syntax
  via `node --check`; backend untouched).
