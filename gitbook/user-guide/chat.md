# Chat with Fox

The chat window is the primary interface. Each turn runs through the agent loop:
the LLM calls tools (`run_python`, `run_r`, `run_shell`, `save_artifact`,
`create_experiment`, `run_sweep`, `start_run`/`finish_run`, notebooks, editors,
RKG, MCP), the results stream in, and the turn is recorded as a **run**.

![Chat with Fox](../assets/screenshots/chat.png)

## Provenance labels

Assistant bubbles read **Fox · model · MCP · action** (e.g.
`FOX · QWEN3.6:LATEST · GITHUB · PUSH`), so a glance shows which model and tool
produced the reply. Tags are clickable to filter the chat.

## Streaming & controls

- Text **streams** token-by-token (debounced re-render).
- **Stop** cancels the current turn cooperatively; progress so far is saved.
- **Edit / retry / copy / delete** per message.
- **Attach** files to a turn; `@schema <file>` renders a schema card inline.

## Permissions & approvals

Network and destructive commands require approval (a prompt appears in the
window). Decisions can be **allowed / denied** and remembered as grants.
**God mode** (`/godmode`, ⚡) auto-approves everything but confines all work to
a quarantined per-turn sandbox folder `<project>/godmode/<timestamp>/`.

## Tool-step budget

Each turn may make up to **`agent.max_iters`** tool calls (default 20) before
the agent must answer. If a long task hits the limit, the reply ends with the
"maximum number of tool steps" message and a **▶ Continue** button appears under
it — click it and the agent picks up where it left off with a fresh budget. To
let the agent do more work per turn, raise **Max tool steps per turn** in
Settings (⚙).

## Pipeline view

Each assistant reply that produced a run carries a 🛠 **Pipeline** block
(collapsible) summarizing the entire turn at a glance:

- **Experiment** — name, goal metric/target, and the **strategy** (baseline,
  parameter sweep, improve-loop iteration, restore, …).
- **Data** — the data-related steps and datasets touched.
- **Steps** — the ordered tool actions with phase tags (🧬 Data · 🔍 Explore ·
  🧠 Model · 📊 Evaluate · 💾 Persist) and the code/args snippet each ran.
- **Hyperparameters / model config** — the final variant's config (model,
  epochs, lr, …) and the **metrics** the run reported (goal ★ highlighted).

The same per-run pipeline is available in the experiment **detail modal** — click
any run there to expand its full pipeline. Pipelines are also re-attached to
older messages once the run list loads, so history shows the same detail.

## Reviewer

After each turn, the background reviewer re-reads the recent transcript and
flags untraceable numbers, mismatched figures, unsupported claims, missing
provenance, and code issues — then suggests up to 3 concrete next steps. Each
suggestion becomes a first-class record; **Apply & rerun** tracks it, and the
**regression check** reports whether it actually improved the goal (✓/✗).

## Next steps

The latest suggestions also render as **Suggested next steps** under the last
assistant message, with one-click **Run**.
