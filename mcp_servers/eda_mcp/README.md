# EDA MCP — Exploratory Data Analysis + Automated Report Generation

A suite of **five focused MCP servers** that let any MCP host (Fox chat,
Claude, Cursor, LangChain, …) perform a thorough exploratory data analysis on a
dataset and produce a structured, professional report. Every server is
**100 % local** — no cloud APIs, and the optional LLM narrative uses **only
local models**.

```
Dataset (CSV / Parquet / Excel / SQL…)
        │
        ▼
  eda-data-profiler          load, validate, schema & type inference
        │
   ┌────┼──────────────┐
   ▼    ▼              ▼
 univariate    multivariate    visualizer
 (single-var)  (correlations)  (plots + insights)
   └────┼──────────────┘
        ▼
  eda-report-generator        Markdown / HTML / PDF report
```

| Server | Entry point | Tools |
|---|---|---|
| 1. `eda-data-profiler` | `python -m mcp_servers.eda_mcp.profiler` | `load_dataset`, `get_schema`, `profile_basic`, `detect_data_quality_issues`, `list_datasets` |
| 2. `eda-univariate` | `python -m mcp_servers.eda_mcp.univariate` | `univariate_numeric`, `univariate_categorical`, `missing_analysis`, `distribution_summary` |
| 3. `eda-multivariate` | `python -m mcp_servers.eda_mcp.multivariate` | `correlation_matrix`, `pairwise_plots_data`, `target_relationships`, `pca_summary`, `clustering_preview` |
| 4. `eda-visualizer` | `python -m mcp_servers.eda_mcp.visualizer` | `generate_plot`, `auto_visualize`, `extract_visual_insights` |
| 5. `eda-report-generator` | `python -m mcp_servers.eda_mcp.report` | `compile_report`, `add_custom_section`, `export_report` |

All servers share a disk-backed **DatasetStore** keyed by `dataset_id` (default
workspace `~/.fox/eda`, override with `FOX_EDA_WORKSPACE`), so the agent never
re-uploads data — it just passes the id around. Intermediate artifacts (parquet,
schema metadata, PNG plots, reports) live in that workspace.

## Installation

Inside the Fox workbench this is already on `PYTHONPATH` (the repo root). The
scientific stack ships with the workbench (`numpy`, `pandas`, `scipy`,
`scikit-learn`, `matplotlib`, `jinja2`, `mcp`). To use it standalone:

```bash
pip install -e mcp_servers/eda_mcp          # or copy the folder out as eda-mcp/
pip install -e mcp_servers/eda_mcp[langchain]   # optional orchestration layer
```

## Running each server (stdio)

```bash
python -m mcp_servers.eda_mcp.profiler       # server 1
python -m mcp_servers.eda_mcp.univariate     # server 2
python -m mcp_servers.eda_mcp.multivariate   # server 3
python -m mcp_servers.eda_mcp.visualizer     # server 4
python -m mcp_servers.eda_mcp.report         # server 5
```

## Client configuration

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "eda-data-profiler":  {"command": "python", "args": ["-m", "mcp_servers.eda_mcp.profiler"], "cwd": "/path/to/fox-repo"},
    "eda-univariate":     {"command": "python", "args": ["-m", "mcp_servers.eda_mcp.univariate"], "cwd": "/path/to/fox-repo"},
    "eda-multivariate":   {"command": "python", "args": ["-m", "mcp_servers.eda_mcp.multivariate"], "cwd": "/path/to/fox-repo"},
    "eda-visualizer":     {"command": "python", "args": ["-m", "mcp_servers.eda_mcp.visualizer"], "cwd": "/path/to/fox-repo"},
    "eda-report-generator": {"command": "python", "args": ["-m", "mcp_servers.eda_mcp.report"], "cwd": "/path/to/fox-repo"}
  }
}
```

**Fox workbench (chat):** all five servers are registered as defaults in
`backend/mcp.py`, so the chat agent can already call
`eda_profiler__load_dataset`, `eda_report__compile_report`, etc. with your
local model.

## Example end-to-end workflow

1. `load_dataset("/data/titanic.csv")` → note the `dataset_id`.
2. `profile_basic(dataset_id)` + `detect_data_quality_issues(dataset_id)`.
3. `univariate_numeric(dataset_id, "age")`, `univariate_categorical(dataset_id, "embarked")`.
4. `correlation_matrix(dataset_id)`, `target_relationships(dataset_id, "survived")`.
5. `auto_visualize(dataset_id)` → PNG plots + captions.
6. `compile_report(dataset_id)` → Markdown report; `export_report(report_id, "html")`.

## LangChain orchestration (local models only)

`mcp_servers/eda_mcp/langchain/` provides a ready-to-use agent:

```python
import asyncio
from mcp_servers.eda_mcp.langchain import run_eda

asyncio.run(run_eda("/data/titanic.csv"))   # uses the five MCP servers
```

The LLM is always a **local** model: `ChatOpenAI(base_url=FOX_TOOL_BASE_URL,
model=FOX_MODEL)` — defaults to Ollama at `http://127.0.0.1:11434/v1`. A
LangGraph pipeline (`langchain.graphs.build_eda_graph`) is also provided for a
deterministic profile → quality → univariate → multivariate → visualize →
report flow.

## Security

- No arbitrary code execution inside the servers (pure pandas/numpy/scipy/
  sklearn/matplotlib).
- `load_dataset` accepts local paths or URLs; URLs are downloaded to a temp
  cache before parsing.
- Plot/report generation writes into the shared workspace, which is why those
  tools ask for approval in the workbench chat (analysis tools are read-only).

## Scope notes

- Inputs: CSV, Parquet, Excel and JSON. A live SQL connector is not included yet
  (pandas `read_sql` can be added behind `load_dataset` when a DSN is needed).
- Plotting uses pure matplotlib (no seaborn/missingno dependency); adding a
  `seaborn`-based style later is easy via the `_PLOT_BUILDERS` registry.
- LLM narrative is rule-based unless `--llm`/`use_llm` is set, and even then it
  only ever talks to a **local** model (`FOX_TOOL_BASE_URL` + `FOX_MODEL`).

## Tests

```bash
python -m pytest mcp_servers/eda_mcp/tests
```
