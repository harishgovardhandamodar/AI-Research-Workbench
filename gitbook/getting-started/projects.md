# Projects & sessions

## Projects

A **project** is one research workspace: its own SQLite store
(`workbench.db`), artifacts, kernels, notebooks, audit trail, and experiment
management snapshots. Projects appear in the top-bar **🗂 Sessions** menu and on
the landing page.

Project data lives under `<workbench>/projects/<name>/`:

| Path | Contents |
|---|---|
| `workbench.db` | SQLite: messages, runs, experiments, goals, learnings, campaigns, evals, suggestions, settings, workflow history |
| `artifacts/` | Figures, tables, text and data artifacts |
| `notebooks/` | Jupyter notebooks |
| `data/` | Imported datasets (e.g. Kaggle) |
| `research/` | Autoresearch target files (`program.md`, `experiment.py`, `log.md`) |
| `audit/` | The tamper-evident audit log (SQLite + JSONL) |
| `godmode/` | Quarantined sandboxes from `/godmode` runs |

## Sessions

The **🗂 Sessions** menu lists projects; you can **switch**, **fork**, or
**delete** a session. Each project has its own conversation, kernels, and
experiment history. Switching projects resets the chat context and reloads the
Experiments tab.

## Project state

The **Experiments tab → Overview** shows KPI cards per project: experiments,
runs, campaigns, benchmarks, learnings, and open goals — plus a
cross-experiment leaderboard. Click a KPI card to jump to its section.

## Focus

Each project can set a **focused experiment** (★ on an experiment card, or
`/focus <name|id|off>`). The agent's context steers toward the focused
experiment, free-form chat turns auto-attach to it, and its runs/timelines stay
grouped. The focus survives restarts (stored as a project setting).
