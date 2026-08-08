# The improve loop

The reviewer-driven improve loop is the workbench's core optimization cycle:
**run → review → apply best suggestion → rerun → measure**, bounded by an
iteration budget.

## How it works

1. **Plan**: the agent proposes and creates an experiment (hypothesis, goal
   metric/target, explicit plan, baseline config).
2. **Iterate** (bounded): each iteration runs a variant, the background reviewer
   produces up to 3 suggestions, and the loop applies the first **unused**
   suggestion's prompt to the next iteration.
3. **Measure**: every applied suggestion is bound to the run it produced and
   resolved by the **regression check** — comparing the goal metric against the
   run it derived from (`delta`, `improved`).
4. **Learn**: each measured outcome becomes a **learning** (see
   [Learnings](learnings.md)).

## Stop conditions

- Any applicable target (experiment or Goals-panel) is reached → auto-mark
  **completed**.
- **Two consecutive applied suggestions fail to improve** the goal → stop
  ("the current direction is exhausted").
- The reviewer offers no actionable suggestion → stop.
- Iteration budget spent.

## Suggestion records

Suggestions are first-class records with a lifecycle
(`pending → applied → accepted/rejected`), so the loop never re-tries a change
already measured, and the UI shows ✓/✗ outcome badges on applied suggestions.

## Resume & retry

A failed iteration can be **retried** from the workflow panel (resumes from
iteration N). The loop is resumable across restarts via persisted workflow
metadata.
