# fox manual — AI Research Workbench CLI

`fox` is the command-line front-end to the Fox workbench: local-first AI
experiments, persistent kernels, research knowledge graphs and scenario-driven
autoresearch loops — wrapped in a modern terminal UI.

## Quick start

```
fox                 # interactive shell (splash + `>` prompt)
fox status          # server / model / research overview
fox serve           # launch the workbench server (http://127.0.0.1:8765)
fox manual          # this manual
fox help            # subcommand help
```

Full user guide with screenshots-style examples: `docs/CLI.md`.

Point the CLI at a remote server with `FOX_URL`:

```
FOX_URL=http://host:8765 fox status
```

## Global options

| option       | meaning                                     |
|--------------|---------------------------------------------|
| `--url URL`  | server base URL (default `$FOX_URL` or `http://127.0.0.1:8765`) |
| `--help`     | show help                                   |
| `--version`  | show version                                |

## Subcommands

### `fox status`
Health and configuration summary: server reachability, LLM model + endpoint,
agent settings, and research scenario counts.

### `fox doctor`
Environment check: Python version, required packages, server reachability,
git repo state. Exits non-zero when something needs attention.

### `fox serve [--host H] [--port P]`
Launch the FastAPI workbench in the foreground (default `127.0.0.1:8765`).

### `fox projects`
```
fox projects list                     # list all projects
fox projects show <name>              # project detail
fox projects new <name> [-d DESC]     # create a project
fox projects rm <name>                # delete a project
fox projects fork <name> <target>     # fork a project
```

### `fox runs <project>`
Recent agent runs for a project (model, status, iteration count, duration).

### `fox experiments <project>`
```
fox experiments <project>             # list experiments
fox experiments <project> start       # launch a new experiment
```

### `fox research`
Scenario-driven autoresearch loops over a domain corpus.

```
fox research list                                 # all scenarios
fox research status <scenario>                    # live phase + progress
fox research report <scenario>                    # latest synthesis report
fox research build <scenario>                     # phase 1: build corpus
fox research synthesize <scenario>                # phase 2: grounded report
fox research experiments <scenario>               # phase 3: replication
fox research loop <scenario>                      # full 4-phase loop
```

Background jobs are polled live; a `job completed` line marks success.

### `fox graph`
Knowledge-graph summary (papers / concepts / relations).

### `fox papers`
Latest ingested papers with concept counts.

### `fox splash`
Render the fox splash panel (static).

### `fox manual [topic]`
Print this manual, or one section (e.g. `fox manual research`).

## Configuration

The CLI itself is configuration-free; server settings (LLM endpoint, model,
agent knobs) live in the workbench `config.json`, which `fox status` reflects.

## Exit codes

`0` success · `1` server/command error · `2` usage error

## Sections

* `quickstart` — Quick start
* `status` — fox status / doctor / serve
* `projects` — projects, runs, experiments
* `research` — research scenarios + autoresearch loop
* `graph` — knowledge graphs
* `manual` — this manual
