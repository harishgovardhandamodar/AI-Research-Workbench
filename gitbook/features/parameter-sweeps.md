# Parameter sweeps

`run_sweep` executes the **same code once per config point** on **parallel
kernels** and records one run per config — preferred over hand-rolling
`start_run`/`finish_run` for a grid.

## How it works

- The tool takes `code`, `configs` (a list of dicts), and an optional
  `label_prefix`. The code reads its parameters from a `config` dict and reports
  its headline metric via `report_metric(name, value)`.
- Each config runs on its own `PythonKernel` from a **kernel pool** (true
  process-level parallelism) via `asyncio.gather`. When the kernel manager has
  no pool (remote kernels), it falls back to sequential on the main kernel.
- Each point records a run with `kind="sweep"`, its config, metrics, **full
  code** and **environment snapshot**, and an explicit `parent_run_id` (the
  experiment's best run), so the branch graph shows the sweep as one step.

## Sweep composer (UI)

The Experiments tab has a **Sweep & Finetune** section with a visual sweep
composer — no need to write a chat prompt:

1. Pick the experiment the runs attach to, and an optional label prefix.
2. Write the Python that reads `config` and reports metrics.
3. Define the config space either as a **grid** (parameter → comma-separated
   values → cartesian product, live preview of the point count) or as explicit
   **JSON points**.
4. **Run sweep** launches a deterministic `run_sweep` intent (no LLM
   round-trip): the backend expands the grid, reuses the parallel-kernel
   machinery, records one run per point, and streams the summary table into
   chat — so the pipeline view shows the whole launch.

## Output

A markdown summary table: `point · label · config · metric columns` (best
ranked), plus any failed points, and the recorded run count.

## When to use it

- Grid searches ("sweep eps over {0.5, 1, 2}").
- Ablations over a small config space.
- Any evaluation where the same pipeline runs under many parameter sets.
