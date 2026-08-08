# Goal-first experimentation

The workbench is organized around **objectives**, not just turns. Every
experiment carries a goal metric + target + direction, and the whole system
steers toward it.

## Objective editing

Experiments can be refined **in place** (✎ edit): change the name, hypothesis,
goal metric, target, higher-is-better, and plan without recreating the
experiment and orphaning its runs. Editing only the target keeps the metric.
Targets/plans can be cleared.

## Distance-to-target

Every leaderboard and experiment card shows **distance to target**
(`to_target` / `% of target`) and a **✓ reached** state when the goal is met.
The agent context reports `best so far`, `% of target`, and target reached, so
the model knows how close it is.

## Focus experiment

A project can set a **focused experiment** (`/focus`, ★). Context selection
prefers the focus; free-form turns auto-associate to it; runs/timelines stay
grouped — preventing objective drift across a long session.

## Combined goal checks

The improve loop and the agent check the experiment's own goal **plus**
Goals-panel goals (scoped or project-wide). When any target is reached, the
loop stops and the experiment is auto-marked **completed**.

## Cross-experiment memory

The agent context also reports the **best value for the goal metric across all
experiments**, so it knows the project-wide baseline, not just the current
experiment's.

See [Goals, focus & learnings](../user-guide/goals-focus-learnings.md).