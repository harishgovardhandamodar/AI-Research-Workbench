# Fox — Experiment Tracking (VS Code extension)

A VS Code extension that brings the **Local · Open · Agentic Experimentation
Workbench**'s experiment-tracking features into the editor: browse experiments,
runs, leaderboards, campaigns and benchmarks; take actions (create, focus, run a
campaign or benchmark); and **produce experimentation documentation** — the
project report, the next-research agenda, and a summary of findings — as
markdown files opened right in VS Code.

The extension is a thin client: it talks to the workbench's REST API over HTTP
and needs the workbench running (the Docker container on port 8765).

## Install (local)

1. `code --install-extension vscode/fox-experiment-tracking-0.1.0.vsix`
   (after packaging), or during development:
   `code --extensionDevelopmentPath=$PWD/vscode .`
2. Start the workbench (`docker compose up -d fox`).
3. Run the command **Fox: Open Experiment Tracking**.

## Configuration

| Setting | Default | Description |
|---|---|---|
| `fox.baseUrl` | `http://localhost:8765` | Workbench base URL. |
| `fox.project` | *(auto first project)* | Project to track. |

## Commands

- **Fox: Open Experiment Tracking** — the tracking dashboard (webview).
- **Fox: Refresh Tracking** — reload the dashboard.
- **Fox: Generate Experimentation Report** — writes `reports/<project>-report.md`
  and opens it in the markdown preview.
- **Fox: Next Research Agenda** — writes the agenda + proposed campaign to a
  markdown file.
- **Fox: Summary of Findings** — the executive summary + leaderboard + learnings.

## Dashboard

- **Experiments** — list with goal/best/status/runs; click a name for the detail
  (leaderboard + runs). Create, focus, and edit from the panel.
- **Campaigns** — create + run/resume/stop background research campaigns.
- **Benchmarks** — create + run/rerun/stop model benchmarks.
- **Learnings** — the accumulated findings with ✓/✗ outcome badges.
- **Docs & summary** — one-click documentation generation.

> Note: the agentic **improve loop** runs in the workbench chat (WebSocket),
> which this REST-based extension doesn't drive — the panel shows a hint instead.
