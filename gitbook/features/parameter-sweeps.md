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

## Output

A markdown summary table: `point · label · config · metric columns` (best
ranked), plus any failed points, and the recorded run count.

## When to use it

- Grid searches ("sweep eps over {0.5, 1, 2}").
- Ablations over a small config space.
- Any evaluation where the same pipeline runs under many parameter sets.
