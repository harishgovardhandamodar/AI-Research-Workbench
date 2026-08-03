# Fox - Experiment workbench

A fully local, open-source experiment workbench: the local-models equivalent of
"Claude Science". It runs entirely on your machine with local LLMs (via Ollama),
so your data never leaves home unless you explicitly approve a network command.

Following the plan in `plan.md`, it provides the core Phase 0–3 stack:

- **Chat + tool-calling agent** against local models (OpenAI-compatible)
- **Persistent, sandboxed Python kernel** — variables, dataframes and figures
  survive across turns
- **Artifact system with full provenance** — every figure/table records its exact
  code + environment snapshot, stored in SQLite + filesystem
- **Background reviewer agent** that checks claims against the execution history
- **Permission model** — shell commands ask before running; network is deny-by-default
- **Project workspaces** — SQLite-backed sessions, per-project kernels
- **Figure annotation / regeneration** — "remove the gridlines" regenerates the figure

## Architecture

```
Frontend (vanilla JS, served by FastAPI)   ← /ws chat + /api
Backend (FastAPI + asyncio)
  ├─ Coordinator agent (tool loop) + Reviewer agent
  ├─ Tool registry  (run_python, run_r, run_shell, save_artifact, ...)
  ├─ Kernel manager (persistent Python subprocess; figure capture; env snapshot)
  ├─ Artifact store (SQLite + filesystem, provenance)
  └─ Permission manager (allow / ask / deny)
LLM routing (hybrid, 100% local):
  ├─ Gateway  http://localhost:8081/v1   → plain chat, model list (hive cluster)
  └─ Direct   http://127.0.0.1:11434/v1  → tool-calling turns (Ollama)
```

### LLM routing

The workbench defaults to the hive cluster gateway (`http://localhost:8081/v1`,
the Go cluster in `~/WorkBook/Ollama-local-hives-cluster`) for model listing and
plain chat. Tool-calling turns go **directly to local Ollama**
(`http://127.0.0.1:11434/v1`) because the hive gateway currently strips `tools`
from OpenAI requests. Both are local; change them under **Settings**.

### Requirements

- Python 3.12+
- A local Ollama server (or any OpenAI-compatible endpoint)
- Models with tool-calling support (e.g. `qwen3.6:latest`, `glm-4.7-flash:bf16`,
  `gemma4:31b`). Tiny models like `llama3.2:3b` work but are less reliable.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install numpy pandas scipy matplotlib scikit-learn   # kernel stack
./run.sh                 # starts http://127.0.0.1:8765
```

Open http://127.0.0.1:8765, pick a model (Settings → Test connection), and chat.
Everything is stored under `workbench/projects/<name>/` (SQLite + artifacts).

## Run with Docker (persistent data)

Builds the workbench into a container. Projects, artifacts and config live in the
named volume `fox_data` (mounted at `/app/workbench`), so your data survives
container restarts and rebuilds.

```bash
docker compose up -d --build
# open http://127.0.0.1:8765
```

By default the container talks to Ollama / the hive gateway running on the **host**
via `host.docker.internal` (Linux uses `extra_hosts` → `host-gateway`). Override with
environment variables:

```bash
FOX_BASE_URL=http://host.docker.internal:8081/v1 \
FOX_TOOL_BASE_URL=http://host.docker.internal:11434/v1 \
FOX_MODEL=qwen3.6:latest \
docker compose up -d --build
```

Optionally run Ollama itself as a container (instead of the host one):

```bash
docker compose --profile ollama up -d --build
# then point the workbench at it (Settings → LLM) using http://ollama:11434/v1
```

Other useful commands:

```bash
docker compose logs -f fox      # follow logs
docker compose down             # stop (data is kept in the volume)
docker compose down -v          # stop AND delete all persistent data
```

## Model Context Protocol (MCP) support

The workbench is an **MCP Host**: it discovers tools from local (`stdio`) and remote
(`streamable HTTP`) MCP servers and merges them into the agent's tool set, so the
local LLM can call database connectors, domain tools, etc. — the same servers that
Claude, Cursor or VS Code could use.

- Tool names are namespaced `<server>__<tool>` (e.g. `science__uniprot_lookup`).
- A built-in **`mcp_servers/science_tools.py`** server ships tools for sequence
  GC content, peptide mass, Kyte–Doolittle hydrophobicity, an offline UniProt
  mock connector, and Welch's t-test. Run it standalone or use it from the workbench.
- A built-in **`mcp_servers/privacy_tools.py`** server ships local-first privacy
  tooling: PII detection, dataframe privacy assessment, red-team / membership-
  inference / re-identification evaluation, differential privacy (Laplace /
  Gaussian, budget tracking, ε-gauge) and synthetic-data generation. See
  `docs/PRIVACY-MCP.md`. A privacy workflow (peer-in-distribution exploitation →
  red-team corner cases → DP robustness → audit trail) auto-runs when you ask
  for it in chat — reports and figures land in the Artifacts panel.
- Add/remove servers under **Settings → MCP** (stdio command+args, or HTTP URL +
  headers), then re-save; status and tool counts are shown.
- **Human-in-the-loop**: tools annotated read-only run freely; anything that may
  write data or launch compute asks the user before running (one-time grant).

```bash
.venv/bin/pip install mcp        # optional; enables MCP support
```

Demo prompts:

> "Use the MCP science tools to compute the GC content of ATGCCGTAATG and look up
> UniProt P04637."

> "Run privacy__assess_dataframe_privacy on the clinical cohort, then apply
> privacy__apply_laplace_dp to the admission counts with ε=0.5."

> "Exploit privacy as a peer in the distribution, run red-team corner cases,
> apply DP and check robustness, and document the whole process as an audit trail."

## Jupyter integration (run as an addon inside Jupyter)

Run the whole workbench as a `jupyter_server` extension, so the AI Science
Workbench becomes a panel at `/fox` inside your Jupyter server (single origin,
shared port):

```bash
.venv/bin/pip install jupyter_server        # optional dependency
./run-jupyter.sh                            # starts jupyter on :8888
# open  http://localhost:8888/fox/
```

How it works: the extension (`jupyter_fox/`) spawns the workbench FastAPI app as a
sidecar subprocess on an ephemeral localhost port, then proxies HTTP and WebSocket
traffic to it under the `/fox` prefix — chat, persistent kernel, artifacts,
reviewer and notebook execution all work inside Jupyter. The frontend auto-detects
the `/fox` base path (see `FOX_BASE` in `frontend/`). Enable/disable with
`jupyter server extension enable/disable jupyter_fox`.

Notebook experiments stored in a project's `notebooks/` folder are plain
`.ipynb` — you can also open them in JupyterLab's normal notebook view.

## Try the demo experiments

See `examples/README.md`. 3 script experiments plus **18 Jupyter notebooks**
(`examples/notebooks/`, built by `examples/build_notebooks.py`) spanning tiny →
large across statistics, kinetics, PDEs, single-cell, epidemiology, time series,
omics, physics, ML and image processing — 54 executable cells producing 32
figures, all runnable inside the workbench or as addon in Jupyter.

## Security model

- `run_python` runs inside the persistent kernel subprocess (isolated per project).
- `run_shell` always prompts the user; network and destructive commands are
  flagged and denied unless explicitly approved.
- Nothing leaves the machine unless you approve a network command.

## Status / roadmap

Implemented: chat + tool calling, persistent kernels, artifacts + provenance,
reviewer, permissions, regeneration, projects. Next phases from the plan:
scientific renderers (3Dmol/igv/RDKit), SSH/Slurm compute, skills/connectors.
