# fox manual — AI Research Workbench CLI

`fox` is the command-line front-end to the Fox workbench: local-first AI
experiments, persistent kernels, research knowledge graphs and scenario-driven
autoresearch loops — wrapped in a modern terminal UI.

## Quick start

```
fox                 # opencode-style terminal window (splash + command panel)
fox status          # server / model / research overview
fox serve           # launch the workbench server (http://127.0.0.1:8765)
fox manual          # this manual
fox help            # subcommand help
```

`fox` with no arguments opens a **full-screen terminal window** (header status
bar, navigable sidebar, scrollable output panel, themed input line with
history + tab completion). The same window is available at any time with
`fox tui`; when stdin/stdout isn't a terminal it falls back to a plain `>`
prompt. Keybindings: `Enter` run · `Tab` complete/cycle focus · `↑/↓` history
or sidebar · `PgUp/PgDn` scroll · `Ctrl+L` clear · `Ctrl+T` theme · `Ctrl+P`
palette · `Ctrl+B` sidebar · `?` help · `Ctrl+C` interrupt · `Ctrl+D` quit.

### Themes

The window is themed with semantic color tokens. Built-ins: `opencode-dark`
(default), `opencode-light`, `opencode-midnight`, `solarized-dark`,
`high-contrast-dark`, `high-contrast-light`. Switch in-app with `Ctrl+T`,
persist with `--theme NAME`, or configure:

```json
{ "theme": "opencode-light", "mouse": true, "sidebar": true }
```

stored at `$XDG_CONFIG_HOME/fox/tui.json` (default `~/.config/fox/tui.json`).
`--render-preview` prints a static frame for a view/theme (docs & tests).

Full user guide with screenshots-style examples: `docs/CLI.md`.

Point the CLI at a remote server with `FOX_URL`:

```
FOX_URL=http://host:8765 fox status
```

## Global options

| option       | meaning                                     |
|--------------|---------------------------------------------|
| `--url URL`  | server base URL (default `$FOX_URL` or `http://127.0.0.1:8765`) |
| `--json`     | machine-readable JSON on stdout (no panels/ANSI) |
| `--quiet`    | suppress spinner/progress output            |
| `--debug`    | debug logging to stderr (also `FOX_DEBUG=1`)|
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
Recent agent runs for a project (id, label, status, best metric, started).

### `fox audit <project> [action]`
Local agent audit trail for a project:
```
fox audit <project>                # KPI overview (events, criticals, overrides, deviations)
fox audit <project> events         # recent audit events table
fox audit <project> deviations     # flagged behavioural deviations
fox audit <project> agents         # per-agent activity
fox audit <project> verify         # JSONL hash-chain integrity (exit 1 if broken)
```
See `docs/AUDIT-TRAIL.md` for the full audit system (MCP proxy, redaction,
deviation detector, in-app Audit Trail timeline).

### `fox run <project> <id> [report]`
Inspect a single run (metrics, prompt, review findings, artifacts), or
`report` to generate + print its lab-notebook report.

### `fox experiments <project>`
List, launch, or run experiments for a project (requires a running server and
an existing project).

```
fox experiments <project>                        # list experiments
fox experiments <project> start                  # launch a new experiment
fox experiments <project> start --name "eps sweep" --hypothesis "…" \
    --goal-metric accuracy --goal-target 0.9
fox experiments <project> run-obfuscation        # bank obfuscation suite
fox experiments <project> run-obfuscation --n-rows 5000 --seed 7
```

`start` creates an experiment (named `<project> experiment` by default;
override with `--name`, plus `--hypothesis` / `--goal-metric` /
`--goal-target` / `--plan`) and returns its id. `run-obfuscation` runs the synthetic
bank-transaction scenario suite and records each scenario as a run under an
`obfuscation (bank)` experiment (`--n-rows` dataset size, default 2000;
`--seed` RNG seed, default 42). Pair with `fox runs <project>` to inspect
results.

### `fox experiment <project> <id> [ranking]`
Experiment detail (goal, hypothesis, plan, its runs), or `ranking` for the
leaderboard (rank, metric, Δ vs best) — `--metric m` overrides the goal metric.

### `fox compare <project> <run_a> <run_b>`
Metric delta between two runs (value, Δ, %) plus a shared/increased/decreased
summary.

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

### `fox papers [list|search|add]`
```
fox papers                       # latest ingested papers
fox papers search <query>        # search the knowledge graph
fox papers add <ref>             # ingest: arXiv id, URL, or free-text query
```

`add` submits a background job and reports the outcome (papers added with
concept counts). arXiv ids are matched as `\d{4}.\d{4,5}`, URLs are ingested
as web pages, anything else runs as an arXiv search.

### `fox jobs [id]`
List recent background jobs, or inspect one job.

### `fox scheduler`
Research scheduler status (enabled, cadence, synthesize, active, due
scenarios).

### `fox pool [action]`
```
fox pool                     # topic -> paper counts
fox pool topics              # topic -> search query
fox pool topics add <name> <query>
fox pool topics rm <name>
fox pool import <arxiv_id>   # ingest a pool paper (background job)
```

### `fox manage [action]`
Experiment management repo (source control for experiments/artifacts).

```
fox manage status                          # repo dir / github / remote
fox manage repos                           # sibling git repos found
fox manage link <owner/repo>               # set the GitHub remote
fox manage commit <project> [-m msg]
fox manage push <project>
fox manage commit-and-push <project> [-m msg]
```

### `fox splash`
Render the fox splash panel (static).

### `fox tui [--theme NAME] [--render-preview]`
Open the full-screen terminal window (what `fox` with no arguments launches).
All commands below run inside it with live streaming output; `--json` /
`--debug` / `--quiet` work there too. `--theme` selects a built-in theme;
`--render-preview [--view …]` prints a static frame and exits (used by the
docs and tests).

### `fox manual [topic]`
Print this manual, or one section (e.g. `fox manual research`).

## Configuration

The CLI itself is configuration-free; server settings (LLM endpoint, model,
agent knobs) live in the workbench `config.json`, which `fox status` reflects.
Debug tracing is enabled with `--debug` or `FOX_DEBUG=1`.

## Completion

bash / zsh tab-completion scripts ship in `completions/` (`fox.bash`,
`_fox`). The interactive shell (`fox`) also completes commands and actions.

## Exit codes

`0` success · `1` server/command error · `2` usage error

## Sections

* `quickstart` — Quick start
* `status` — fox status / doctor / serve
* `projects` — projects, runs, experiments
* `research` — research scenarios + autoresearch loop
* `graph` — knowledge graphs
* `manual` — this manual
