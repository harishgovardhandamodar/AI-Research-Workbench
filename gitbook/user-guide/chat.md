# Chat with Fox

The chat window is the primary interface. Each turn runs through the agent loop:
the LLM calls tools (`run_python`, `run_r`, `run_shell`, `save_artifact`,
`create_experiment`, `run_sweep`, `start_run`/`finish_run`, notebooks, editors,
RKG, MCP), the results stream in, and the turn is recorded as a **run**.

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

## Reviewer

After each turn, the background reviewer re-reads the recent transcript and
flags untraceable numbers, mismatched figures, unsupported claims, missing
provenance, and code issues — then suggests up to 3 concrete next steps. Each
suggestion becomes a first-class record; **Apply & rerun** tracks it, and the
**regression check** reports whether it actually improved the goal (✓/✗).

## Next steps

The latest suggestions also render as **Suggested next steps** under the last
assistant message, with one-click **Run**.
