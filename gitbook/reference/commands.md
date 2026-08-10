# Chat slash commands

Type these in the chat input and press Enter. Most route deterministically (no
model needed).

| Command | What it does |
|---|---|
| `/help` | Show the command list in the chat |
| `/godmode <request>` | Full access in a quarantined sandbox (`<project>/godmode/<ts>`) |
| `/improve [experiment]` | Run the improve loop for the latest (or named) experiment |
| `/campaign <question>` | Plan and run a multi-step research campaign in the background |
| `/autoresearch [metric]` | Autonomous research loop over `research/experiment.py` |
| `/focus <name|id|off>` | Set / clear the focused experiment |
| `/experiments` | List experiments with status, goal, best |
| `/complete <name|id>` | Mark an experiment completed (also publishes its aggregate report) |
| `/cancel <name|id>` | Mark an experiment cancelled |
| `/activate <name|id>` | Reopen an experiment (status → active) |
| `/compare <a> <b>` | Compare two runs (metric deltas + %); bare = last two |
| `/report [run_id]` | Lab-notebook report for the last (or given) run |
| `/commit` | Commit experiment artifacts to the management repo |
| `/push` | Push the management repo to its GitHub remote |
| `/kaggle <owner/dataset>` | Import a public Kaggle dataset |
| `/notebook <name>` | Run a project notebook |
| `/session <name>` | Begin a fresh session (create + switch) from the chat window |
| `/status` | Model, gateway, reviewer, max-iters, repo status |
| `/clear` | Clear this project's conversation |

> Inline commands (no model needed): `@schema <file>` shows a file's columns +
> dtypes; `@load <file> [var]` loads a data file into the Python kernel as a
> DataFrame (default var = file stem) and shows a shape/preview card. Use
> `@load data.csv df` to load a CSV as `df`, then ask Fox to analyze it.
> `@mcp <server>__<tool> [json]` deterministically invokes an MCP tool (results
> pretty-printed into a code block); `@mcp bg …` runs it in the background and a
> bare `@mcp` lists every connected server's tools. `/god`, `/sandbox` alias
> `/godmode`. `/flat` and `/sets` toggle rendering mode.
