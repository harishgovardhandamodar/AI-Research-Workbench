# Notebooks

Jupyter notebooks live under `<project>/notebooks/` and execute on the project's
**persistent Python kernel**.

## Using notebooks

- **Create** a notebook (code + markdown cells) from the Notebooks tab or via
  the agent (`create_notebook`).
- **Run** it — each code cell executes in the kernel; stdout becomes stream
  output; every figure is saved as an **artifact**; per-cell ok/error is
  recorded.
- **`/notebook <name>`** runs a notebook from chat and records a run
  (`kind="notebook"`) with its metrics and artifact links.

## Notebooks & experiments

Notebook runs appear in the Experiments timeline/graph (kind `notebook`) and can
be attached to an experiment, so figures and metrics from a notebook are
tracked like any other run. A campaign's final step can also produce a
"synthesis notebook".
