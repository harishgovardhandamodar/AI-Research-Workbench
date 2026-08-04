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

Eighteen Jupyter notebooks (`.ipynb`) spanning many scales and scientific domains.
Open them from the workbench UI (**Notebooks** tab), or ask the agent to run one —
the agent executes the cells and the results (outputs + figures + errors) are
written back *into the notebook*.

| Notebook | Scale | Domain | Figures |
|----------|-------|--------|---------|
| `00_tiny_quick_stats` | tiny | statistics | 1 |
| `06_tiny_clt_demo` | tiny | probability / CLT | 2 |
| `01_simple_decay_fit` | simple | kinetics / physics | 1 |
| `07_simple_heat_diffusion` | simple | PDE / physics | 2 |
| `08_simple_logistic_growth` | simple | population dynamics | 2 |
| `02_midscale_cell_clustering` | mid | single-cell | 2 |
| `04_midscale_epidemiology` | mid | epidemiology (SIR) | 1 |
| `09_midscale_regression_diagnostics` | mid | statistics / ML | 2 |
| `10_midscale_ar_forecast` | mid | time series | 3 |
| `11_midscale_volcano_ma` | mid | transcriptomics | 1 |
| `12_midscale_monte_carlo_pi` | mid | simulation | 2 |
| `13_midscale_lotka_volterra` | mid | ecology / ODEs | 2 |
| `14_midscale_hierarchical_clustering` | mid | bioinformatics | 2 |
| `03_large_protein_pipeline` | large | structural bio | 1 |
| `05_large_model_benchmark` | large | machine learning | 1 |
| `15_large_metabolomics_pipeline` | large | metabolomics | 3 |
| `16_large_double_pendulum` | large | physics / chaos | 2 |
| `17_large_image_convolution` | large | image processing | 2 |
| `18_obfuscation_techniques` | mid | data privacy / obfuscation | 1 |
| `19_obfuscation_threat_scenarios` | mid | data privacy / threat modeling | 6 |
| `20_privacy_assessment` | mid | data privacy / red-teaming | 1 |
| `21_differential_privacy` | mid | data privacy / differential privacy | 1 |
| `22_synthetic_data` | mid | data privacy / synthetic data | 1 |

32 figures across 54 executable cells. Regenerate all of them with
`python examples/build_notebooks.py`.

## Data-obfuscation experiments (`obfuscation/`)

The obfuscation study (from `~/WorkBook/obfuscation-study`), adapted to
credit-card transaction data, is bundled as an importable package so you can run
the threat scenarios or obfuscate data directly from the workbench kernel:

- `credit_card_data.py` — `generate_credit_card(n_rows, seed)` produces synthetic
  credit-card transaction records (PANs, issuer BINs, cardholder/merchant names,
  countries, amounts).
- `obfuscate.py` — reusable library: `apply_masking`, `tokenize`, `fuzzy_bucket`,
  `noisy_aggregate`, `k_anonymize`, `sanitize_metadata`, and the high-level
  `obfuscate_dataframe(df, {"mask": ..., "tokenize": ..., "k_anonymize": ...})`.
- `experiments.py` — the 8 threat scenarios (BEC/fraud, insider threat,
  supply-chain leakage, sanctions evasion, corporate espionage, test-environment
  exposure, ATO via security questions, re-identification) plus a counterparty
  reconstruction supplement. Each returns metrics, prints results and draws a
  figure (auto-captured as a workbench artifact). `run_all(df)` returns a
  markdown report.

```bash
# standalone (writes examples/obfuscation/obfuscation_report.md)
.venv/bin/python examples/obfuscation/experiments.py
```

In the workbench kernel:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from examples.obfuscation.credit_card_data import generate_credit_card
from examples.obfuscation import experiments as exp

df = generate_credit_card(2000, seed=42)
report = exp.run_all(df)          # prints summary, returns markdown
```

Obfuscate your own uploaded data (any CSV with matching column names):

```python
from examples.obfuscation import obfuscate as obf
import pandas as pd
mine = pd.read_csv("my_data.csv")
safe = obf.obfuscate_dataframe(mine, {"mask": True, "tokenize": ["card_number"]})
anon, risk = obf.k_anonymize(mine, ["transaction_date", "cardholder_city"], k=5)
```

Notebooks `18_obfuscation_techniques` and `19_obfuscation_threat_scenarios` walk
through the techniques and all nine scenarios inside the workbench UI.

## Privacy red-team / DP / synthetic-data experiments (`privacy/`)

The workbench ships a **privacy MCP server** (`mcp_servers/privacy_tools.py`,
registered as the `privacy` MCP server) covering four areas:

- **Detection & assessment** — `privacy__detect_pii_in_text`,
  `privacy__assess_dataframe_privacy`
- **Red-teaming** — `privacy__membership_inference_eval`,
  `privacy__reidentification_scenario`, `privacy__privacy_redteam_checklist`
- **Differential privacy** — `privacy__apply_laplace_dp`,
  `privacy__apply_gaussian_dp`, `privacy__dp_privacy_budget_report`,
  `privacy__dp_guarantee_summary` (ε-gauge / budget-bar visualization data)
- **Synthetic data** — `privacy__generate_synthetic_tabular`,
  `privacy__synthetic_data_quality_report`

Everything is local-first. The heavy optional libraries (presidio, sdv, opendp)
are used when installed; otherwise built-in implementations (regex PII scan,
native Laplace/Gaussian mechanisms, schema-preserving generation) provide the
same capability.

```bash
# end-to-end red-team + DP + synthetic-data evaluation (writes CSVs + 3 figures)
.venv/bin/python examples/privacy/run_privacy_eval.py
```

Chat prompts for the agent (which call the tools via MCP):

> "Run privacy__assess_dataframe_privacy on examples/privacy/clinical_cohort.csv
> and then privacy__privacy_redteam_checklist for public release. Summarize the
> re-identification risk."
> "Aggregate the admission counts from the clinical cohort with
> privacy__apply_laplace_dp (ε=0.5), track the budget with
> privacy__dp_privacy_budget_report, and save the ε-gauge figure as an artifact."
> "Generate a synthetic version of the cohort with
> privacy__generate_synthetic_tabular and compare utility with
> privacy__synthetic_data_quality_report."

Notebooks `20_privacy_assessment`, `21_differential_privacy` and
`22_synthetic_data` walk through the four capability areas inside the UI.

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
