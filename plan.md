**Plan: Build an open-source AI Science Workbench (local-models equivalent of Claude Science)**

Claude Science (Anthropic, June 2026) is a desktop AI workbench for scientific research. It is **not** a new model. It is a specialized harness around Claude that:

- Runs local Python/R/shell code in sandboxed, persistent kernels
- Produces rich, auditable scientific artifacts (figures, tables, notebooks, protein structures, genome tracks, chemical drawings, manuscripts) with full provenance (code + environment + conversation)
- Orchestrates multi-agent workflows (generalist coordinator + specialist agents + background reviewer)
- Connects to 60+ scientific databases + domain tools (genomics, single-cell, proteomics, structural biology, cheminformatics)
- Manages compute (local laptop → SSH/HPC/Slurm → on-demand GPUs via Modal)
- Supports reusable skills/connectors and forking sessions
- Emphasizes reproducibility, permissioned access, and self-correction

**Goal of this plan**: Build a fully local, open-source alternative that runs entirely on the user’s machine (or their own cluster) using local LLMs (Ollama, LM Studio, llama.cpp, vLLM, etc.). No cloud model dependency required (though optional cloud backends can be supported).

The plan is written so a coding agent can implement it incrementally.

---

### 1. Product Vision & Core Principles

**Name (placeholder)**: LocalScience / OpenScienceBench / SciWorkbench (pick one later).

**North-star capabilities** (MVP → full parity):
1. Chat + multi-agent orchestration with local models
2. Persistent, sandboxed Python + R + shell kernels with environment management
3. Rich scientific artifact system with full provenance
4. Native scientific renderers (proteins, molecules, genome tracks, etc.)
5. Background reviewer agent that checks claims against execution history
6. Skills/connectors system (save pipelines, connect external tools/databases)
7. Local + remote compute management (SSH/Slurm/HPC)
8. Project/workspace model with session forking
9. Privacy-first: data never leaves the machine unless explicitly allowed

**Target users**: Computational biologists, chemists, physicists, data scientists who want Claude-Science-like workflows without sending data to Anthropic.

**Tech stack recommendations** (opinionated but flexible):
- **Frontend**: Electron or Tauri (desktop) + React/Next.js or SvelteKit. Prefer Tauri for smaller binary + better security.
- **Backend / Agent runtime**: Python (FastAPI or pure Python) + LangGraph / CrewAI / AutoGen / custom agent loop, or pure LlamaIndex + custom tools.
- **Local LLM serving**: Ollama (easiest), llama.cpp server, vLLM, or LM Studio. Support OpenAI-compatible API so any backend works.
- **Code execution**: Jupyter kernel protocol (jupyter_client) or restricted Python/R interpreters + bubblewrap/firejail/nsjail sandbox.
- **Artifact storage**: SQLite + filesystem (or DuckDB) for provenance graphs.
- **Scientific rendering**: 
  - Proteins: 3Dmol.js / NGL Viewer / Mol*
  - Molecules: RDKit + Kekule.js or SmilesDrawer
  - Genome tracks: igv.js
  - Plots: matplotlib/seaborn → PNG/SVG + Plotly for interactive
- **Packaging**: AppImage / .dmg / .deb + optional Docker for HPC nodes.

---

### 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Desktop App (Tauri / Electron)                             │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │ Chat UI     │  │ Artifact     │  │ Project / Session  │ │
│  │ + Agent     │  │ Viewer       │  │ Manager            │ │
│  │ Timeline    │  │ (3D, plots,  │  │                    │ │
│  │             │  │  tracks…)    │  │                    │ │
│  └─────────────┘  └──────────────┘  └────────────────────┘ │
└───────────────────────────┬─────────────────────────────────┘
                            │ IPC / HTTP / WebSocket
┌───────────────────────────▼─────────────────────────────────┐
│  Local Runtime (Python)                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Agent        │  │ Kernel       │  │ Compute Manager  │  │
│  │ Orchestrator │  │ Manager      │  │ (local / SSH /   │  │
│  │ (LangGraph)  │  │ (Python/R)   │  │  Slurm)          │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Tool / Skill │  │ Provenance   │  │ Reviewer Agent   │  │
│  │ Registry     │  │ Graph (SQLite)│  │                  │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└───────────────────────────┬─────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   Local LLM Server    Filesystem / DB    Remote HPC
   (Ollama etc.)       + Sandboxes        (SSH/Slurm)
```

---

### 3. Phased Implementation Roadmap

#### Phase 0 – Foundations (1–2 weeks)
- Project scaffolding (Tauri + Python backend or pure Electron + Node + Python sidecar).
- Local LLM connection layer (OpenAI-compatible client). Support model switching, system prompts, tool calling.
- Basic chat UI with streaming.
- Project / workspace model (folder-based or SQLite).
- Simple permission system (folder access grants, network deny-by-default).

#### Phase 1 – Code Execution & Persistent Kernels (core differentiator)
- Integrate Jupyter kernel protocol or restricted exec.
- Python kernel with common scientific stack (numpy, pandas, scipy, matplotlib, seaborn, biopython, scanpy, etc.).
- R kernel (tidyverse + ggplot2).
- Sandboxing: bubblewrap / firejail / nsjail (Linux) + equivalent on macOS.
- Persistent state across turns (variables, dataframes, loaded models stay in memory).
- Environment management: create/clone conda/mamba or uv environments per task; record exact package versions for provenance.
- Tool: `run_python`, `run_r`, `run_shell` with approval gates.

#### Phase 2 – Artifact System + Provenance
- Every generated figure / table / notebook / structure becomes an **Artifact** object.
- Artifact contains:
  - Binary / rendered content
  - Exact code that produced it
  - Environment snapshot (package list + versions)
  - Conversation history snippet that led to it
  - Human-readable description
  - Hash / ID for linking
- UI: side panel or dedicated viewer that shows the artifact + “Show provenance” button.
- Support forking a session (copy state + history).
- Markdown + LaTeX manuscript preview.

#### Phase 3 – Scientific Renderers
- Embed 3Dmol.js / NGL / Mol* for PDB/mmCIF.
- RDKit + JS viewer for SMILES / SDF.
- igv.js for genomic tracks.
- Automatic detection of file types and rendering.
- Annotation UI: click on a figure and type “remove gridlines / change axis to log” → agent edits the original code and regenerates.

#### Phase 4 – Multi-Agent Orchestration + Reviewer
- Coordinator agent (generalist) that can spawn specialist agents.
- Pre-built specialists: genomics, single-cell, proteomics, structural biology, cheminformatics, literature, manuscript.
- Background Reviewer agent that:
  - Scans recent messages + artifacts
  - Flags untraceable numbers, mismatched figures, bad citations
  - Can self-correct or surface issues to the user
- Actor-critic pattern support (as seen in the Allen Institute example).

#### Phase 5 – Skills & Connectors
- Skill = saved pipeline / prompt + tools + environment (YAML or JSON + code).
- Connector = MCP-style or simple HTTP/CLI wrapper to external databases/tools (UniProt, PDB, PubMed, ChEMBL, GEO, local ELNs, etc.).
- Registry of community skills.
- User can “Save as skill” from any successful workflow.

#### Phase 6 – Compute Management
- Local GPU detection and use.
- SSH + key management for remote Linux boxes / login nodes.
- Slurm / PBS job submission (write batch script → submit → poll → pull results).
- Optional Modal / cloud GPU backends (user-provided API keys).
- Always ask for permission before spinning up new resources.
- Keep large datasets on the remote side; only send necessary context to the LLM.

#### Phase 7 – Polish, Packaging, Extensibility
- Windows support (if desired).
- One-click installers.
- Plugin system for new renderers / tools.
- Export full project as reproducible bundle (code + envs + artifacts + conversation).
- Optional cloud sync of non-sensitive metadata only.
- Documentation, example skills, starter projects (single-cell analysis, protein folding pipeline, literature review, etc.).

---

### 4. Key Technical Decisions & Implementation Notes

| Area                  | Recommendation                                      | Why / Notes |
|-----------------------|-----------------------------------------------------|-------------|
| Agent framework       | LangGraph (Python)                                  | Explicit state machines, easy multi-agent, good tool calling |
| LLM interface         | OpenAI-compatible + tool calling                    | Works with Ollama, vLLM, LM Studio, llama.cpp, etc. |
| Code sandbox          | bubblewrap + restricted Jupyter kernels             | Real isolation on Linux; research macOS equivalents |
| Provenance store      | SQLite + JSON blobs + filesystem for large files    | Simple, queryable, offline |
| Scientific viz        | Browser-side (3Dmol, igv.js, RDKit.js)              | No heavy native deps |
| Permission model      | Explicit grants (folder, network host, compute)     | Safety & trust |
| Session state         | In-memory + checkpoint to disk                      | Fast iteration + recoverability |
| Packaging             | Tauri preferred                                     | Smaller, more secure than Electron |

**Local model recommendations** (as of mid-2026):
- Strong generalist: Llama-3.3-70B / Qwen2.5-72B / DeepSeek-V3 / Mixtral variants (quantized)
- Coding-heavy: CodeLlama, Qwen2.5-Coder, DeepSeek-Coder
- Long context: models with 128k+ context for literature + notebooks
- Support vision models later for figure understanding.

---

### 5. Suggested Directory Structure

```
localscience/
├── app/                    # Tauri / Electron frontend
├── runtime/                # Python backend
│   ├── agents/             # Coordinator, specialists, reviewer
│   ├── kernels/            # Python / R managers + sandbox
│   ├── artifacts/          # Provenance models + storage
│   ├── compute/            # Local / SSH / Slurm
│   ├── skills/             # Skill registry + loaders
│   ├── connectors/         # Database / tool connectors
│   └── main.py
├── skills/                 # Built-in + community skills (YAML + code)
├── examples/               # Starter projects
├── docs/
└── README.md
```

---

### 6. Success Metrics for MVP

- User can open a project, chat with a local model, run Python/R code in a persistent kernel, generate a matplotlib figure, and see the exact code + environment that produced it.
- User can annotate the figure in plain language and have the agent regenerate it correctly.
- Reviewer agent flags at least simple inconsistencies (e.g., claim not present in the code output).
- One complete scientific workflow works end-to-end (e.g., basic single-cell clustering or protein structure visualization from PDB).

---

### 7. Risks & Mitigations

- **Model quality**: Local models are weaker than Claude Opus at complex multi-step science. Mitigate with strong system prompts, retrieval over papers, and specialist agents.
- **Sandbox escapes**: Use well-tested isolation (bubblewrap + seccomp). Start conservative.
- **Environment bloat**: Use uv or mamba with explicit env per task; cache common ones.
- **HPC complexity**: Start with simple SSH + scp; add Slurm later.
- **Scientific rendering fidelity**: Prioritize the most common formats first (PDB, SMILES, BAM/bed, PNG/SVG plots).

---

### 8. Next Immediate Steps for a Coding Agent

1. Scaffold the project (Tauri + Python FastAPI or pure Python CLI + web UI).
2. Implement OpenAI-compatible LLM client + basic chat loop with tool calling.
3. Add a sandboxed Python executor with persistent state.
4. Create the Artifact model + simple provenance storage.
5. Build a minimal UI that shows chat + generated plots with “Show code” button.
6. Add a basic reviewer that re-reads the last N messages + code outputs.

Once Phase 1–2 are solid, the rest of the Claude Science feature set becomes incremental.

This plan gives a clear, incremental path to a local, privacy-preserving, auditable AI science workbench that mirrors the key ideas of Claude Science while remaining fully under the user’s control.
