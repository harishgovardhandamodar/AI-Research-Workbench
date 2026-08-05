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
    FOX --> P["projects<br/>list|new|show|rm|fork"]
    FOX --> RUNS[runs &lt;project&gt;]
    FOX --> RUN[run &lt;p&gt; &lt;id&gt;<br/>show|report]
    FOX --> EXP["experiments<br/>list|start|run-obfuscation"]
    FOX --> EXPERIMENT[experiment &lt;p&gt; &lt;id&gt;<br/>show|ranking]
    FOX --> COMPARE[compare &lt;p&gt; &lt;a&gt; &lt;b&gt;]
    FOX --> RES["research<br/>list|status|report|build|synthesize|experiments|loop"]
    FOX --> G[graph]
    FOX --> PAPERS["papers<br/>list|search|add"]
    FOX --> JOBS[jobs [id]]
    FOX --> SCHED[scheduler]
    FOX --> POOL["pool<br/>topics|import"]
    FOX --> MGMT["manage<br/>status|commit|push"]
    FOX --> MAN["manual [section]"]
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
Recent agent runs for a project: run id, label, status, best metric,
started-at.

### `fox run <project> <id> [report]`
Inspect a single run: metrics, prompt, review findings, artifacts. Append
`report` to generate + print its lab-notebook report.

```
fox run <project> <id>
fox run <project> <id> report
```

### `fox experiments <project>`
List, launch, or run experiments for a project. Prerequisites: the workbench
server must be running (`fox serve` or `./run.sh`) and the project must exist
(`fox projects new <name>`).

```
fox experiments <project>                        # list experiments
fox experiments <project> start                  # launch a new experiment
fox experiments <project> start --name "eps sweep" \
    --hypothesis "smaller eps improves acc" --goal-metric accuracy --goal-target 0.9
fox experiments <project> run-obfuscation        # bank obfuscation suite
fox experiments <project> run-obfuscation --n-rows 5000 --seed 7
```

`start` submits an experiment (named `<project> experiment` unless you pass
`--name`) and prints its id. Optional fields: `--hypothesis`,
`--goal-metric`, `--goal-target`, `--plan`. The
`run-obfuscation` action runs the synthetic bank-transaction scenario suite
locally and records each scenario as a run (metrics + figure + masked-vs-raw
transactions) under an `obfuscation (bank)` experiment, which the app's
Experiments panel then displays. Use `--n-rows` to size the synthetic dataset
(default 2000) and `--seed` for reproducibility (default 42).

Check results with the runs listing and drill-down commands:

```
fox runs <project>                 # recent agent runs
fox run <project> <id>             # single run detail
fox run <project> <id> report      # lab-notebook report
fox experiment <project> <id>      # experiment detail + its runs
fox experiment <project> <id> ranking   # leaderboard (Δ vs best)
fox compare <project> <a> <b>      # metric delta between two runs
```

The same commands work inside the interactive shell:

```
  fox > experiments <project>
  fox > experiments <project> start --name "eps sweep"
  fox > runs <project>
  fox > run <project> 8 report
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

### `fox papers [list|search|add]`
```
fox papers                       # latest ingested papers
fox papers search <query>        # search the knowledge graph
fox papers add <ref>             # ingest: arXiv id, URL, or free-text query
```

`add` routes by reference shape: a `\d{4}.\d{4,5}` arXiv id → pool import, an
`http(s)://` URL → web ingest, anything else → arXiv search import. Each runs
as a background job (polled live; `job completed` marks success).

### `fox jobs [id]`
List recent background jobs, or inspect one job (kind, status, timeline).

### `fox scheduler`
Research scheduler status: enabled, check cadence, synthesize flag, active,
due scenarios.

### `fox pool [action]`
```
fox pool                          # topic -> paper counts
fox pool topics                   # topic -> search query
fox pool topics add <name> <query>
fox pool topics rm <name>
fox pool import <arxiv_id>        # ingest a pool paper (background job)
```

### `fox manage [action]`
Experiment management repo — snapshot + version experiments/artifacts in a
sibling git repo (see HOW-to-USE.md § Experiment source control).

```
fox manage status                          # repo dir / github repo / remote
fox manage repos                           # sibling git repos found
fox manage link <owner/repo>               # set the GitHub remote
fox manage commit <project> [-m msg]       # snapshot + commit
fox manage push <project>                  # push to remote
fox manage commit-and-push <project> [-m msg]
```

### `fox splash`
Static splash panel (no animation).

### `fox manual [section]`
Print the full manual, or one section:
`quickstart | status | projects | runs | run | experiments | experiment |
compare | research | graph | papers | jobs | scheduler | pool | manage |
manual`.

---

## 5. Output conventions

- **Panels** (`╭─╮`) carry the command name and group related output.
- **Colors**: fox-orange accent · green = ok/done · red = error · amber = warning
  · dim = metadata.
- **Spinner** (`⠋⠙…`) runs on stderr while the CLI blocks on HTTP calls.
- **Job polling**: research actions poll `/api/rkg/jobs/{id}` every 4s; the
  spinner label shows the latest log line.

## 5b. Scripting (`--json`)

Every data command emits machine-readable JSON on stdout with `--json`
(panels and spinners suppressed, stderr untouched):

```
fox --json projects
fox --json experiments <project>
fox --json research list | jq -r '.[].id'
fox --json scheduler
```

`--quiet` suppresses the spinner without switching output format. Combine with
`--url` for remote hosts. Exit codes are stable for scripting (see §7).

## 6. Configuration

The CLI is configuration-free; all server settings (LLM endpoint, model, agent
knobs, research data root) live in the workbench `config.json`, surfaced by
`fox status`. Connection is selected by `FOX_URL` or `--url`.

Debug tracing (request/response logging to stderr) is enabled with `--debug`
or `FOX_DEBUG=1`:

```
FOX_DEBUG=1 fox status         # [fox:debug] GET /api/config -> 200 …
```

Shell tab-completion scripts ship in `completions/` (`fox.bash` for bash,
`_fox` for zsh); the interactive shell completes commands and actions natively.

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
- `cli/log.py` — leveled stderr logging (`--debug` / `FOX_DEBUG=1`)
- `completions/fox.bash`, `completions/_fox` — bash / zsh tab-completion
