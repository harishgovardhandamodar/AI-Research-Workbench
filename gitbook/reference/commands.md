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
| `/compare <a> <b>` | Compare two runs (metric deltas + %); bare = last two |
| `/report [run_id]` | Lab-notebook report for the last (or given) run |
| `/commit` | Commit experiment artifacts to the management repo |
| `/push` | Push the management repo to its GitHub remote |
| `/kaggle <owner/dataset>` | Import a public Kaggle dataset |
| `/notebook <name>` | Run a project notebook |
| `/status` | Model, gateway, reviewer, max-iters, repo status |
| `/clear` | Clear this project's conversation |

> `/god`, `/sandbox` alias `/godmode`. `/flat` and `/sets` toggle rendering mode.
