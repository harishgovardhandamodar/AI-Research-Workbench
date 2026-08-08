# Learnings & knowledge memory

Rounds of experimentation produce knowledge that is usually lost in chat text.
**Learnings** make it structured, persistent, and *re-injected* so each
experiment starts from what earlier ones discovered.

## Capture — deterministic, from what we already measure

- **Resolved suggestions**: when an applied suggestion is measured by the
  regression check, a learning is recorded:
  `"Tried 'try eps=2': acc 0.5→0.8 (+0.300) — improved."` (or `no gain`).
- Capture happens in the improve loop and the Apply & rerun chat path.

## Storage

A `learnings` table: experiment, run, metric, baseline/outcome/delta,
`improved` (1/0/null), summary, source. `GET /learnings` lists them.

## Injection — feed memory back in

Learnings are injected into:

- **Agent context** — the experiment context includes "Prior learnings" for the
  goal metric, so the model builds on what worked and avoids repeating failures.
- **Reviewer context** — the reviewer sees prior learnings for the experiment,
  so it doesn't re-suggest known no-gain changes.
- **Campaign planner** — new campaign plans are grounded in prior learnings.
- **Project report** — the report lists the project's learnings.

## UI

Experiment cards and the VS Code extension show learnings with **✓ improved /
✗ no gain** badges.

## Why it matters

This is the *compounding* part of autonomous research: the system gets better
over time without being told — each measured outcome makes the next experiment
smarter.
