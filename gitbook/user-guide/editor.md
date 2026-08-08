# In-browser editor & VS Code extension

## In-browser VS Code (code-server)

The 🖊 **Editor** tab embeds a code-server instance (port 8787) pointed at the
workbench's shared workspace volume. Use it to edit generated code, notebooks,
and project files without leaving the browser.

- Editor URL is derived from the host you browse from (`FOX_EDITOR_URL` /
  `FOX_EDITOR_PROBE_URL`); the backend probes reachability and reports it.
- Auth: default `none`; set `CODE_SERVER_AUTH=password` +
  `CODE_SERVER_PASSWORD=<pw>` to require one.

## VS Code extension — Fox Experiment Tracking

A companion extension brings experiment tracking and documentation into VS Code:

- **Experiments** — list with goal/best/status/runs, detail with leaderboard +
  runs, create + focus.
- **Campaigns & benchmarks** — create / run / resume / stop.
- **Learnings** — accumulated findings with ✓/✗ badges.
- **Documentation** — commands that write markdown to `reports/*.md` and open it
  in VS Code:
  - **Fox: Generate Experimentation Report**
  - **Fox: Next Research Agenda**
  - **Fox: Summary of Findings**

The extension is a thin REST client (`fox.baseUrl`, `fox.project` settings) and
requires the workbench running. See [VS Code extension](../development/vscode-extension.md).
