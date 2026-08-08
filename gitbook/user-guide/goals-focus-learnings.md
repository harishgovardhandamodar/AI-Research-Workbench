# Goals, focus & learnings

These three systems steer, stabilize, and accumulate research knowledge.

## Goals (the objective system)

A **goal** is a target metric: `metric`, `target`, `higher is better`, and an
optional experiment scope. Goals live in the **Goals** panel (near the top of
the Experiments tab). After every run, `goal_notices` reports progress: current
value, target, distance to go, and **new best** highlights. A goal is
"reached" when any run satisfies it (shown as ✓ and a filled progress bar).

![Goals panel](../assets/screenshots/goals.png)

Goals also appear in the agent's experiment context (as *additional goals*), so
the agent optimizes for them — not just the experiment's own goal metric.

## Focus (anti-drift)

A project can set a **focused experiment** (★ on a card, or `/focus`). The
context selection prefers the focus; free-form chat turns auto-associate to it;
its runs/timelines stay grouped. This prevents the agent from drifting between
experiments as it answers open-ended prompts.

## Learnings (knowledge memory)

Every **measured outcome** becomes a structured *learning*:

- When an applied suggestion is resolved by the regression check
  ("Tried 'try eps=2': acc 0.5→0.8 (+0.300) — improved" / "no gain"), it is
  recorded.
- Learnings are stored per project and **injected back** into:
  - the agent's experiment context ("Prior learnings"),
  - the reviewer's context (so it doesn't re-suggest known no-gain changes),
  - the campaign planner (so new plans build on what already worked).

The Experiments cards and the VS Code extension show learnings with ✓/✗ badges.
See [Learnings](../features/learnings.md).
