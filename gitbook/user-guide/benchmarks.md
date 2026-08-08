# Model benchmarks

A **model benchmark** (eval) answers "which of the workbench's LLMs is best for
this task?". It runs the same task prompt under each model — each model pinned
to its own experiment — collects the goal metric, and produces a ranked
leaderboard.

## Launch

In the **Benchmarks** panel: give it a name, a task prompt, a comma-separated
list of models, and a goal metric. Or in chat: `Eval <prompt>` via the eval
intent. The task tells the agent to run the experiment and report the goal
metric via `report_metric`.

## Execution

For each model, the benchmark:

1. Creates an experiment named `[Eval] <name> · <model>` **pinned to that model**
   (per-experiment model pinning makes the coordinator use it).
2. Runs one agent turn with the task prompt.
3. Records the run (metrics, code, env, integrity hash) under that experiment.

Benchmarks run in the **background** (start/stop), stream progress, and produce
a **leaderboard report** (model · best metric · Δ vs best) posted to chat and
saved as an artifact. Models without a measured value are ranked last.

## Interpretation

The ranked report highlights the **best model** and its value on the goal
metric. Because every run records its model, environment, code and integrity
hash, the comparison is reproducible.
