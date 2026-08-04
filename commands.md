# Fox — Commands

Everything you can type in the chat box (and the one-click shortcuts above it).

## Slash commands

Type these in the chat input and press **Enter**. The agent routes them
deterministically (no model needed for most).

| Command | What it does |
|---|---|
| `/help` | Show the command list in the chat. |
| `/godmode <request>` | **God mode** — full access (shell / network / MCP all auto-approved) inside a quarantined sandbox folder `<project>/godmode/<timestamp>/`. The agent must do all work there. |
| `/improve [experiment]` | Run the improve loop for the latest experiment (or one you name). |
| `/experiments` | List experiments with id, status, goal metric/target and best value. |
| `/compare <a> <b>` | Compare two runs by id (metric deltas + %). Bare `/compare` uses the last two runs. |
| `/report [run_id]` | Generate a lab-notebook report for the last run (or a specific one) and post it to the chat. |
| `/commit` | Commit this project's experiment artifacts to the **experiment management repo**. |
| `/push` | Push the management repo to its GitHub remote. |
| `/kaggle <owner/dataset>` | Import a public Kaggle dataset into the project's `data/` dir. |
| `/notebook <name>` | Run a project notebook (e.g. `/notebook 01_simple_decay_fit`). |
| `/status` | Show model, gateway, reviewer, max-iters and experiment-repo status. |
| `/clear` | Clear this project's conversation history. |

> `/godmode`, `/god`, `/sandbox` are aliases. `/improve` with no argument uses the
> most recently created experiment.

## UI switches

Add to the URL (or type in the chat box):

| Switch | Effect |
|---|---|
| `?flat=1` | Plain per-message bubbles — **the default**. |
| `?sets=1` | Grouped, collapsible conversation sets (request + steps + result in one card). |
| `/flat` / `/sets` (in chat) | Same toggles without typing the URL. |

## Quick-action shortcuts

One-click buttons above the composer:

- 🛡 **Privacy workflow** — run the privacy peer-exploitation workflow
- ↻ **Fresh rerun** — rerun it with a new random seed
- ⇄ **Compare runs** — compare the stored privacy-workflow runs
- ⚡ **God mode** — full access in a quarantined sandbox (`/godmode`)
- 🔁 **Improve experiment** — improve the latest experiment toward its goal
- ⚗ **New experiment** — have the agent plan + create an experiment
- 🏃 **Run sample notebook** — run `01_simple_decay_fit`
- 🥇 **Compare last 2 runs** — show metric deltas between the last two runs
- 📊 **Report last run** — generate a lab-notebook report
- 📝 **Summarize chat** — agent summarizes the conversation
- 📥 **Import Kaggle dataset** — import the titanic dataset
- 🏆 **Best run so far** — show the best run and why it wins
- 🗂 **Experiments** — list experiments and their status/best metric

## Related settings

- **Experiment management repo** — Settings → *Experiment management repo*:
  repo path (Detect sibling repos), GitHub remote (Link), auto-commit / auto-push.
  `/commit` and `/push` invoke the same actions manually.
- **Kaggle** — Settings → *Kaggle API*: username + key for `/kaggle`.
- Rendering mode persists via the URL (`?flat=1` / `?sets=1`).
