# Demo Experiments

Three runnable, reproducible science experiments of increasing scale. All are
deterministic (fixed seeds) so results are identical every run, and every figure
becomes an auditable artifact with its producing code + environment snapshot.

| File | Scale | What it does |
|------|-------|--------------|
| `01_simple_decay_fit.py` | simple | Simulate an exponential-decay time course, fit `A0·e^(−kt)`, estimate half-life with 95% CI, plot data + fit + residuals |
| `02_midscale_cell_clustering.py` | mid-scale | Simulate a 500-cell single-cell RNA-seq dataset, normalize → PCA → KMeans → t-SNE, plot embeddings + marker heatmap, report Adjusted Rand Index |
| `03_large_protein_pipeline.py` | large | Build a mini protein's backbone from φ/ψ angles (internal-coordinate geometry), write a PDB file, compute a Ramachandran plot, composition, Kyte-Doolittle hydrophobicity, secondary structure, and a full markdown report |

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
Load and run examples/experiments/02_midscale_cell_clustering.py. Report the
Adjusted Rand Index and show the embedding figure.
```

```
Run examples/experiments/03_large_protein_pipeline.py, then save the markdown
report as an artifact and show the Ramachandran plot.
```

Because the kernel is persistent, you can then ask for follow-ups directly:

> "Increase the noise to 10% and rerun experiment 01 — how does the CI change?"
> "Regenerate the embedding figure without axis labels."

## Starter project templates

For blank-slate experiments, create a project in the UI and ask for the workflow
you want (single-cell clustering on real data, protein structure from PDB, a
literature review with citations, etc.). The reviewer agent will flag numbers that
can't be traced to code output after each turn.
