# Command-line (fox CLI)

The `fox` CLI (`python -m cli` / `bin/fox`) drives the workbench and its
subsystems from the terminal.

## Core

| Command | Purpose |
|---|---|
| `fox` | Open the full-screen terminal UI |
| `fox serve` | Launch the workbench server |
| `fox status` | Workbench + model + research overview |
| `fox doctor` | Environment check |
| `fox version` | Version |

## Projects & runs

| Command | Purpose |
|---|---|
| `fox projects` | Manage projects |
| `fox runs <project>` | List runs of a project |
| `fox run <id>` | Inspect a run / generate a report |
| `fox compare <a> <b>` | Metric delta between two runs |
| `fox experiments` / `fox experiment` | List / start experiments |

## Knowledge & research

| Command | Purpose |
|---|---|
| `fox graph` | Knowledge-graph summary |
| `fox papers` | List / search / ingest papers |
| `fox research` | Research scenarios + autoresearch |
| `fox pool` | Research pool (papers + topics) |
| `fox jobs` | List / inspect RKG background jobs |
| `fox scheduler` | Research scheduler status |

## Governance

| Command | Purpose |
|---|---|
| `fox audit <project>` | Agent audit trail for a project |
| `fox manage` | Experiment management repo (commit/push/status) |
| `fox eda` | Exploratory data analysis + report |
| `fox manual` | Print the full manual |
