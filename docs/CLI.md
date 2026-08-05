# Fox CLI — Terminal Front-end

`fox` is the command-line front-end to the Fox workbench. It talks to the same
FastAPI backend the web UI uses, so everything it shows is live server state:
projects, agent runs, experiments, research scenarios, knowledge-graph stats and
ingested papers.

The CLI is **zero-dependency** (stdlib only) and styled like modern terminal AI
tools: animated fox splash screen, rounded bordered panels, aligned key/value
rows, data tables, a live spinner during blocking calls, and an interactive
`>` shell.

Source: `cli/` · entry points: `python -m cli`, `bin/fox`, the `fox` console
script · full reference: `cli/manual.md` (also printed by `fox manual`).

---

## 1. Command layout

```mermaid
flowchart LR
    FOX[fox] --> SHELL[no args → interactive shell]
    FOX --> SPLASH[splash]
    FOX --> V[version]
    FOX --> STATUS[status]
    FOX --> DOC[doctor]
    FOX --> SERVE[serve]
    FOX --> P[projects<br/>list|new|show|rm|fork]
    FOX --> RUNS[runs &lt;project&gt;]
    FOX --> EXP[experiments<br/>list|start]
    FOX --> RES[research<br/>list|status|report|build|synthesize|experiments|loop]
    FOX --> G[graph]
    FOX --> PAPERS[papers]
    FOX --> MAN[manual [section]]
```

Every subcommand accepts `--help`. Global `--url` (or `FOX_URL`) selects the
server.

---

## 2. Installation & entry points

The CLI runs from the repo checkout with no extra dependencies:

```bash
./bin/fox --version        # wrapper: finds the repo venv automatically
python -m cli --version    # with the venv active
fox --version              # after `pip install .` (console script)
```

Point the CLI at any running workbench:

```bash
FOX_URL=http://host:8765 ./bin/fox status
./bin/fox --url http://host:8765 research list
```

> Default server: `http://127.0.0.1:8765` (set by `FOX_URL`, then `--url`).

---

## 3. Interactive shell

`fox` with no arguments shows the animated fox splash and drops into a `>`
prompt. Every prompt command is the same handler as the one-shot form:

```
  fox > help
  fox > status
  fox > projects
  fox > research status autonomous-agents-security
  fox > exit
```

Useful in `help`: `status`, `doctor`, `projects`, `runs <p>`,
`experiments <p>`, `research`, `graph`, `papers`, `serve`, `manual`, `exit`.

---

## 4. Subcommand reference

### `fox status`
Health + configuration summary: server reachability, LLM model and endpoints,
agent settings (`max_iters`, reviewer), and research-scenario counts.

```
$ fox status
╭ fox ╮ status ────────────────────────────────────────╮
│    server:              running  http://127.0.0.1:8765 │
│    model:               qwen3.6:latest                 │
│    base_url:            http://host.docker.internal:8081/v1 │
│    tool_base_url:       http://host.docker.internal:11435/v1 │
│    max_iters:           20                              │
│    reviewer:            on                              │
│    research scenarios:  2 total                         │
╰───────────────────────────────────────────────────────╯
```

### `fox doctor`
Environment check: Python version, required packages, server reachability, git
repo state. Exits `1` when something needs attention (e.g. server offline).

### `fox serve [--host H] [--port P]`
Launch the FastAPI workbench in the foreground (default `127.0.0.1:8765`) —
equivalent to `./run.sh`.

### `fox projects`
```
fox projects                       # list all projects
fox projects show <name>           # project detail
fox projects new <name> -d "desc"  # create
fox projects rm <name>             # delete
fox projects fork <name> <target>  # fork
```

### `fox runs <project>`
Recent agent runs for a project: run id, model, status, iteration count,
duration, started-at.

### `fox experiments <project>`
```
fox experiments <project>           # list experiments
fox experiments <project> start     # launch a new experiment
```

### `fox research`
Scenario-driven autoresearch loops over a domain corpus (Research Workbench).

```
fox research list                                 # all scenarios
fox research status <scenario>                    # live phase, progress, log tail
fox research report <scenario>                    # latest synthesis report (markdown)
fox research build <scenario>                     # phase 1: build the corpus
fox research synthesize <scenario>                # phase 2: grounded report (reviews + keep/revert)
fox research experiments <scenario>               # phase 3: replication experiments
fox research loop <scenario>                      # full chained 4-phase loop
```

`build` / `synthesize` / `experiments` / `loop` submit a **background job** and
poll it live with a spinner; a `job completed <id>` line marks success. Long
phases (arXiv refresh + per-paper LLM analysis) take minutes by design.

```
$ fox research list
╭ research scenarios ──────────────────────────────╮
│    id                         name                phase  papers  score │
│    ─────────────────────────  ──────────────────  ─────  ──────  ───── │
│    autonomous-agents-security Autonomous Agents &… done   5       98.0 │
│    enterprise-ai-security     Enterprise AI Adop… idle   0       None  │
╰──────────────────────────────────────────────────────────────╯
```

### `fox graph`
Knowledge-graph summary: papers, concepts, relations, RAG chunks/embedding
dimension, and GPU availability.

### `fox papers`
Latest ingested papers with concept counts.

### `fox splash`
Static splash panel (no animation).

### `fox manual [section]`
Print the full manual, or one section:
`quickstart | status | projects | research | graph | manual`.

---

## 5. Output conventions

- **Panels** (`╭─╮`) carry the command name and group related output.
- **Colors**: fox-orange accent · green = ok/done · red = error · amber = warning
  · dim = metadata.
- **Spinner** (`⠋⠙…`) runs on stderr while the CLI blocks on HTTP calls.
- **Job polling**: research actions poll `/api/rkg/jobs/{id}` every 4s; the
  spinner label shows the latest log line.

## 6. Configuration

The CLI is configuration-free; all server settings (LLM endpoint, model, agent
knobs, research data root) live in the workbench `config.json`, surfaced by
`fox status`. Connection is selected by `FOX_URL` or `--url`.

## 7. Exit codes

| code | meaning                                  |
|------|------------------------------------------|
| `0`  | success                                  |
| `1`  | server / command error (e.g. unreachable)|
| `2`  | usage error (unknown command/action)     |

## 8. Reference / diagrams

- `cli/manual.md` — embedded user manual (`fox manual`)
- `cli/ui.py` — ANSI panel/table/spinner toolkit
- `cli/client.py` — HTTP client for `/api/*` and `/api/rkg/*`
- `cli/commands.py` — subcommand implementations
- `cli/interactive.py` — `>` shell (`fox` with no args)
- `cli/splash.py` — animated fox splash
