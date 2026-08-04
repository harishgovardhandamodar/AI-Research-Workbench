# Fox — How to Use

A practical, hands-on guide to the **Fox experiment workbench**: a fully local,
open-source research assistant that pairs a chat-driven AI agent with a
persistent Python/R kernel, Jupyter notebooks, full artifact provenance, a
background reviewer, and experiment tracking.

Everything runs on your machine. Your data never leaves home unless you
explicitly approve a network command.

---

## Table of contents

1. [Quick start](#quick-start)
2. [First launch and settings](#first-launch-and-settings)
3. [Tour of the interface](#tour-of-the-interface)
4. [Working with the AI agent](#working-with-the-ai-agent)
5. [Projects and files](#projects-and-files)
6. [Persistent kernels](#persistent-kernels)
7. [Jupyter notebooks](#jupyter-notebooks)
8. [Artifacts and provenance](#artifacts-and-provenance)
9. [Background reviewer](#background-reviewer)
10. [Permissions and approvals](#permissions-and-approvals)
11. [Experiment tracking](#experiment-tracking)
12. [Agent dashboard and MCP servers](#agent-dashboard-and-mcp-servers)
13. [In-browser VS Code editor](#in-browser-vs-code-editor)
14. [Built-in workflows](#built-in-workflows)
15. [Demo experiments](#demo-experiments)
16. [Configuration reference](#configuration-reference)
17. [Troubleshooting](#troubleshooting)

---

## Quick start

### Local (Python)

Requirements: **Python 3.12+**, a **local Ollama server** (or any
OpenAI-compatible endpoint), and a model with tool-calling support (e.g.
`qwen3.6:latest`, `glm-4.7-flash:bf16`, `gemma4:31b`; tiny models like
`llama3.2:3b` work but are less reliable).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install numpy pandas scipy matplotlib scikit-learn   # kernel stack
./run.sh                                                            # http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765>, pick a model under **Settings**, and chat.
All state is stored under `workbench/projects/<name>/` (SQLite + artifacts).

### Docker (persistent data)

```bash
docker compose up -d --build
# open http://127.0.0.1:8765
```

Projects, artifacts and config live in the named volume `fox_data` (mounted at
`/app/workbench`), so they survive restarts and rebuilds. The container reaches
Ollama / the hive gateway on the **host** via `host.docker.internal`.

Other useful commands:

```bash
docker compose logs -f fox      # follow logs
docker compose down             # stop (data is kept in the volume)
docker compose --profile ollama up -d --build   # run Ollama as a container too
```

### Inside Jupyter

Run the whole workbench as a `jupyter_server` extension (single origin, shared
port):

```bash
.venv/bin/pip install jupyter_server        # optional dependency
./run-jupyter.sh                            # starts Jupyter on :8888
# open http://localhost:8888/fox/
```

---

## First launch and settings

Click **Settings** (top-right) and configure:

| Setting | Purpose |
|---------|---------|
| **Gateway base URL** | Chat + model list. Defaults to a hive-cluster gateway (`http://localhost:8081/v1`). |
| **Direct Ollama base URL** | Tool-calling turns. Defaults to `http://127.0.0.1:11434/v1`. |
| **Model** | Which model drives the agent (type to filter the model list). |
| **Temperature** | Sampling temperature for the agent. |
| **Run background reviewer after each turn** | Enables the reviewer that checks claims against execution history. |
| **MCP servers** | Model Context Protocol servers (see [Agent dashboard](#agent-dashboard-and-mcp-servers)). |

Use **Test connection** before saving. Settings persist in
`workbench/config.json`.

> **LLM routing note:** the gateway is used for plain chat/model listing; tool
> calls go directly to Ollama because some gateways strip `tools` from OpenAI
> requests. Both are local — change them under **Settings**.

---

## Tour of the interface

### Top bar

- **Main views** — `Chat` · `Experiments` · `Agent` · `Editor`
- **Project selector** with **new (+)** · **fork (⧉)** · **delete (✕)** buttons
- **Model selector** for the current project
- Connection status dot, side-panel toggle, **Export PDF**, **Settings**

### Chat panel (left)

- **Workflow** banner — live progress for long pipelines (arXiv replication,
  privacy workflow, improve loop).
- Message stream — your prompts, the agent's replies, tool calls/results.
- **Quick actions** — one-click prompts:
  - **Privacy workflow** — run the privacy peer-exploitation workflow
  - **Fresh rerun** — rerun it with a new random seed
  - **Compare runs** — compare the stored workflow runs
- **Composer** — type your request and press **Send**.

### Side panel tabs (right)

| Tab | What it shows |
|-----|---------------|
| **Artifacts** | Every figure/table the agent produced, with provenance. |
| **Notebooks** | Jupyter notebooks in the project; create, open, run. |
| **Files** | Project files (upload/download/delete) + knowledge graphs. |
| **Kernel** | Persistent Python kernel variables and environment; R kernel status; reset button. |
| **Review** | Background reviewer findings; run a review now. |
| **Permissions** | Granted permissions and recent approval decisions. |

---

## Working with the AI agent

The agent (called **Fox**) solves problems by writing and running code in a
persistent, sandboxed kernel. Variables, dataframes and figures survive across
turns, so you can ask follow-ups like *"increase the noise and rerun"*.

### Agent tools

| Tool | Use it for |
|------|-----------|
| `run_python` | Compute, data analysis, plots (numpy/pandas/scipy/matplotlib). Figures are auto-captured as artifacts. |
| `run_r` | R code (requires `Rscript`). Each call is a fresh process — state does not persist between R calls. |
| `run_shell` | Shell commands in the session workspace. Network/destructive commands require approval. |
| `save_artifact` | Persist a named table/text/CSV as an artifact with provenance. |
| `run_notebook` / `create_notebook` | Create and execute Jupyter notebooks in the project. |
| `create_experiment` / `start_run` / `finish_run` / `report_metric` | Structured experiment tracking (see [Experiment tracking](#experiment-tracking)). |
| `editor__*` | List/read/edit/open files in the in-browser VS Code editor. |
| `<server>__<tool>` | MCP server tools (e.g. `privacy__assess_dataframe_privacy`). |

### Example prompts

> "Load the attached CSV and cluster the cells, then plot a UMAP."

> "Run the experiment in examples/experiments/01_simple_decay_fit.py and
> summarize the fitted half-life."

> "Run the notebook examples/notebooks/02_midscale_cell_clustering.ipynb and
> report the Adjusted Rand Index. Keep the results in the notebook."

> "Use the MCP science tools to compute the GC content of ATGCCGTAATG and look
> up UniProt P04637."

> "Run privacy__assess_dataframe_privacy on the clinical cohort, then apply
> privacy__apply_laplace_dp to the admission counts with ε=0.5."

### Tips for reliable results

- Ask the agent to use `create_experiment` + `report_metric` for any headline
  number so runs are recorded, comparable, and appear in the **Experiments** tab.
- Delimit variants explicitly with `start_run` (label + config) and `finish_run`
  so each variant is recorded against the baseline.
- Figures are **auto-saved** as artifacts — there is no need to call
  `plt.savefig()`.
- After a figure is produced, open it in the **Artifacts** tab and use
  **Regenerate** to request changes (e.g. *"remove the gridlines, use a log
  scale on y"*).

---

## Projects and files

- **New project** — `+` in the top bar; each project has its own kernel, chat
  history, notebooks, artifacts and SQLite store.
- **Fork** — duplicate a project (and its history/kernel state) into a new
  session.
- **Delete** — removes the project and its stored data.
- **Upload** — in the side panel's **Files** tab, choose a file and click
  **Upload**; it appears in the project workspace for the agent to read.

---

## Persistent kernels

The **Kernel** side-panel tab shows:

- **Python kernel (persistent)** — live variables from your session; **Reset**
  clears the kernel state. This is where `run_python` executes.
- **Environment** — the package environment snapshot captured with each run.
- **R kernel** — status only; R runs via a fresh `Rscript` process per call.

If a long session gets into a bad state, use **Reset** rather than restarting
the server.

---

## Jupyter notebooks

Notebooks live in the project's `notebooks/` folder and are plain `.ipynb`
files (openable in JupyterLab too).

- **Create** — side panel → **Notebooks** → **+ New** (name + optional initial
  code).
- **Edit** — open a notebook; add cells with **+ cell**, edit inline, **Save**.
- **Run** — **Run all** executes the cells through the persistent kernel and
  writes results (outputs, figures, errors) *into* the notebook.
- The agent can also run them via `run_notebook`.

---

## Artifacts and provenance

Every figure, table and file the agent produces is registered as an artifact
with full provenance:

- **Code that produced this** — the exact source.
- **Environment snapshot** — package versions/state at run time.

In the **Artifacts** tab, click an artifact to view it and its metadata, and use
**Show provenance** for the code + environment. **Regenerate** lets you ask the
agent to change the figure (e.g. *"remove the gridlines"*) and re-run it.

---

## Background reviewer

The reviewer agent re-checks claims against the execution history after each
turn (enable under **Settings**). Its findings appear in the **Review** side
panel — use **Run now** to trigger a review manually. When a reviewer
suggestion offers an "Apply & rerun" action, the agent applies it as a fresh
turn.

---

## Permissions and approvals

- `run_python` runs inside the isolated per-project kernel — no prompt needed.
- `run_shell` always asks; **network and destructive commands** are flagged and
  denied unless you explicitly approve them.
- MCP tools marked read-only run freely; anything that writes data or launches
  compute asks first.
- Approvals can be **one-time (temporary)** or remembered. Grant/revoke them in
  the **Permissions** side-panel tab.

---

## Experiment tracking

The **Experiments** main view is your research log. Everything the agent
produces (agent runs, notebooks, privacy workflows) lands here with structured
metrics.

### Experiments

Create an experiment (in the UI or via the agent's `create_experiment` tool)
with:

- **Name** and one-sentence **hypothesis**
- **Goal metric** and **goal target**, plus **higher is better**
- An optional **plan** (hypothesis, configs/variables to try, stopping criteria)

Experiments have a **lifecycle status** (`active` / `completed` / `cancelled`);
only active experiments can be improved by the loop. Each experiment lists its
runs and a **ranking** (leaderboard) of variants by the goal metric.

### Timeline and Graph views

- **Timeline** — metric evolution across runs over time.
- **Graph** — similarity edges between runs (thicker = more similar). Click a
  node for details.

### Goals

Add project-wide or experiment-scoped goals (metric, target, higher/lower is
better). Progress is shown and used by the improve loop to decide when to stop.

### Run comparison

Select **Run A vs Run B** and click **Compare** to see deltas (B minus A) for
every metric.

### Improve loop

Ask the agent to improve an experiment toward its goal, e.g.:

> "Improve experiment 'my_experiment' toward its goal."

The bounded loop (max 5 iterations) runs a variant, has the reviewer suggest
the next change, applies the best suggestion, and reruns — stopping when the
goal is reached, the reviewer has no further suggestions, or the budget is
spent. A summary table reports per-iteration metrics and the applied
suggestion.

---

## Agent dashboard and MCP servers

The **Agent** main view shows the agent + add-on dashboard, including the
configured **MCP servers**. The workbench is an MCP host: it discovers tools
from local (`stdio`) and remote (`streamable HTTP`) servers and merges them into
the agent's tool set.

Built-in servers (all local-first, in `mcp_servers/`):

| Server | Tools (prefix) | Capabilities |
|--------|----------------|--------------|
| `science` | `science__*` | GC content, peptide mass, hydrophobicity, offline UniProt mock, Welch's t-test. |
| `privacy` | `privacy__*` | PII detection, dataframe privacy assessment, red-team / membership-inference / re-identification evaluation, Laplace/Gaussian DP with budget tracking, schema-preserving synthetic data. |
| `robustness` | `robustness__*` | Adversarial robustness (ART FGSM/PGD on sklearn), robustness metrics, checklist, simple FGSM perturbation. |
| `arxiv` | `arxiv__*` | Turn an arXiv paper into a replication workflow: ingest, summarize, structured notes, experiment spec, compare, replication report, knowledge graph. |
| `graphrag` | `graphrag__*` | Lightweight GraphRAG retrieval over knowledge graphs with an LLM-ready answer prompt. |

Add/remove servers under **Settings → MCP** (name, transport, command/args for
stdio, or URL + headers for HTTP), then re-save. Mark a server **Trusted** to
skip approval prompts for its tools.

---

## In-browser VS Code editor

`docker compose up` also starts a `code-server` sidecar sharing the `fox_data`
volume, so everything the agent generates — reports, notebooks, knowledge
graphs, project files — can be edited in a full VS Code editor in your browser.

- Open the **Editor** tab (or go to <http://127.0.0.1:8787>).
- No login by default; require one with `CODE_SERVER_AUTH=password` and a
  `CODE_SERVER_PASSWORD`.
- The agent can drive it via the `editor__*` tools (edits ask for approval).

See **[docs/VSCODE-EDITOR.md](docs/VSCODE-EDITOR.md)** for the full guide.

---

## Built-in workflows

### arXiv replication

Trigger it in chat with an arXiv paper, e.g.:

> "Replicate https://arxiv.org/pdf/2409.12642 and summarize what the authors
> claim."

The workflow ingests the paper, extracts text, writes structured notes,
summarizes, crafts an experiment spec, runs it, compares results, writes a
replication report, and builds a **knowledge graph** — with a live progress
banner in the chat panel. Query/merge graphs with `arxiv__query_knowledge_graph`
and `arxiv__merge_knowledge_graphs`, and browse them in the **Files → Knowledge
graphs** list. See **[docs/ARXIV-REPLICATION.md](docs/ARXIV-REPLICATION.md)**.

### Privacy workflow

Three quick-action buttons or the chat prompt:

> "Exploit privacy as a peer in the distribution, run red-team corner cases,
> apply DP and check robustness, and document the whole process as an audit
> trail."

Runs a deterministic 3-stage pipeline (peer-in-distribution exploitation →
red-team corner cases → differential-privacy robustness) and writes an audit
trail plus figures under `examples/privacy/reports/`. See
**[docs/PRIVACY-MCP.md](docs/PRIVACY-MCP.md)**.

---

## Demo experiments

- **Scripts** — `examples/experiments/01_simple_decay_fit.py`,
  `02_midscale_cell_clustering.py`, `03_large_protein_pipeline.py` (run directly
  or through the agent).
- **Notebooks** — `examples/notebooks/` (28 demo notebooks built by
  `examples/build_notebooks.py`) spanning statistics, kinetics, PDEs,
  single-cell, epidemiology, time series, omics, physics, ML and image
  processing.
- **Obfuscation** — `examples/obfuscation/` bundles a synthetic credit-card
  transaction generator, an obfuscation library, and 9 threat scenarios.
- **Adversarial** — `examples/adversarial/` datasets + FGSM-style attacks for
  the robustness server.
- **Privacy** — `examples/privacy/` end-to-end red-team/DP/synthetic-data
  evaluation (`run_privacy_eval.py`, `run_peer_exploitation.py`).
- **ArXiv** — the arXiv MCP server (`mcp_servers/arxiv_replication.py`) drives
  the full replication workflow; the standalone scripted local replication runs
  (`examples/arxiv/`) now live in the **personal-experiments** repo
  (`../personal-experiments/examples/arxiv/`).

See **[examples/README.md](examples/README.md)** for the full catalog.

---

## Configuration reference

Environment variables (used by `docker-compose.yml` and the app):

| Variable | Default | Purpose |
|----------|---------|---------|
| `FOX_BASE_URL` | `http://host.docker.internal:8081/v1` | Gateway for chat / model list. |
| `FOX_TOOL_BASE_URL` | `http://host.docker.internal:11435/v1` | Direct endpoint for tool-calling turns. |
| `FOX_MODEL` | `qwen3.6:latest` | Default model. |
| `FOX_BIND` | `127.0.0.1` | Bind address (set to `0.0.0.0` to expose to the LAN). |
| `FOX_EDITOR_URL` | `http://127.0.0.1:8787` | In-browser VS Code URL. |
| `FOX_EDITOR_PROBE_URL` | `http://code-server:8080` | Editor health-check URL. |
| `FOX_EDITOR_FOLDER` | `/home/coder/workbench` | Folder the editor opens. |
| `CODE_SERVER_AUTH` / `CODE_SERVER_PASSWORD` | — | Require a password for the editor. |

Runtime settings (base URL, tool URL, model, temperature, reviewer toggle, MCP
servers) live in `workbench/config.json`.

---

## Troubleshooting

- **No models / connection fails** — verify Ollama is running and reachable,
  then **Settings → Test connection**. Some models need tool-calling support.
- **Agent loops without finishing** — try a larger/smarter model, lower the
  temperature, or check `max_iters` in `workbench/config.json` (`agent.max_iters`).
- **Kernel in a bad state** — side panel → **Kernel → Reset**.
- **VS Code editor not reachable** — start it with
  `docker compose up -d code-server`, then refresh the **Editor** tab.
- **R code errors** — each `run_r` call is a fresh process; re-set state
  (libraries/variables) inside each snippet, or prefer `run_python`.
- **Reviewer too noisy** — disable *Run background reviewer after each turn* in
  **Settings**.
- **Nothing leaves the machine** — data stays local unless you approve a network
  `run_shell` command.

For per-feature deep-dives, see the docs in `docs/`:
[VSCODE-EDITOR.md](docs/VSCODE-EDITOR.md),
[PRIVACY-MCP.md](docs/PRIVACY-MCP.md),
[ARXIV-REPLICATION.md](docs/ARXIV-REPLICATION.md),
[Add_MCP_adversarial_robustness_evaluation.md](docs/Add_MCP_adversarial_robustness_evaluation.md).
