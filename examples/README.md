# Demo Experiments

Runnable, reproducible science experiments of increasing scale. All are
deterministic (fixed seeds) so results are identical every run, and every figure
becomes an auditable artifact with its producing code + environment snapshot.

## Scripts (`experiments/`)

| File | Scale | What it does |
|------|-------|--------------|
| `01_simple_decay_fit.py` | simple | Simulate an exponential-decay time course, fit `A0·e^(−kt)`, estimate half-life with 95% CI, plot data + fit + residuals |
| `02_midscale_cell_clustering.py` | mid-scale | Simulate a 500-cell single-cell RNA-seq dataset, normalize → PCA → KMeans → t-SNE, plot embeddings + marker heatmap, report Adjusted Rand Index |
| `03_large_protein_pipeline.py` | large | Build a mini protein's backbone from φ/ψ angles (internal-coordinate geometry), write a PDB file, compute a Ramachandran plot, composition, Kyte-Doolittle hydrophobicity, secondary structure, and a full markdown report |

## Notebooks (`notebooks/`)

Six Jupyter notebooks (`.ipynb`) spanning six scales. Open them from the workbench
UI (**Notebooks** tab), or ask the agent to run one — the agent executes the cells
and the results (outputs + figures + errors) are written back *into the notebook*.

| Notebook | Scale | What it does |
|----------|-------|--------------|
| `00_tiny_quick_stats` | tiny | Two-group comparison: summary stats, Welch t-test, Cohen's d, histogram |
| `01_simple_decay_fit` | simple | Exponential decay fit + half-life with 95% CI |
| `02_midscale_cell_clustering` | mid | Synthetic 500-cell single-cell clustering (PCA → KMeans → t-SNE), ARI + marker heatmap |
| `04_midscale_epidemiology` | mid | SIR epidemic ODE model for several R₀; peak + attack rate |
| `03_large_protein_pipeline` | large | Protein backbone from φ/ψ, PDB, Ramachandran plot, secondary structure |
| `05_large_model_benchmark` | large | 3 classifiers with 5-fold CV + ROC comparison |

Regenerate them with `python examples/build_notebooks.py`.

## Run standalone (no agent needed)

```bash
.venv/bin/python examples/experiments/01_simple_decay_fit.py
.venv/bin/python examples/experiments/02_midscale_cell_clustering.py
.venv/bin/python examples/experiments/03_large_protein_pipeline.py
```

## Run inside the Fox workbench (recommended)

Start the workbench (`./run.sh`, open http://127.0.0.1:8765), pick a model, then
paste one of these prompts into the chat. Fox will run the code in the persistent
kernel and every generated figure lands in the **Artifacts** panel with a
"Show provenance" view.

```
Run the experiment in examples/experiments/01_simple_decay_fit.py and
summarize the fitted half-life.
```

```
Run the notebook examples/notebooks/02_midscale_cell_clustering.ipynb and
report the Adjusted Rand Index. Keep the results in the notebook.
```

```
Create a notebook for a differential-expression experiment, run it, and
summarize the top hits.
```

Because the kernel is persistent, you can then ask for follow-ups directly:

> "Increase the noise to 10% and rerun experiment 01 — how does the CI change?"
> "Regenerate the embedding figure without axis labels."

## Starter project templates

For blank-slate experiments, create a project in the UI and ask for the workflow
you want (single-cell clustering on real data, protein structure from PDB, a
literature review with citations, etc.). The reviewer agent will flag numbers that
can't be traced to code output after each turn.
