# VS Code extension

The companion extension (`vscode/`) brings experiment tracking and
documentation into VS Code. It is a **thin REST client**: the extension host
does all HTTP to the workbench (no CORS), relaying results to a webview
dashboard via `postMessage`. No backend changes are required.

## Files

| File | Purpose |
|---|---|
| `package.json` | Manifest: commands, config, activation |
| `extension.js` | Activation, API client, webview panel, markdown writer |
| `media/tracking.html` | Webview shell |
| `media/tracking.js` | Dashboard logic (posts `{kind:"api"}` to the host) |
| `media/tracking.css` | Dashboard styles |

## Commands

- **Fox: Open Experiment Tracking** — the dashboard webview.
- **Fox: Refresh Tracking**
- **Fox: Generate Experimentation Report** — writes `reports/<project>-report.md`,
  opens in the markdown preview.
- **Fox: Next Research Agenda**
- **Fox: Summary of Findings** — executive summary + leaderboard + learnings.

## Dashboard tabs

Experiments (list + detail/leaderboard, create/focus) · Campaigns (create/run/
resume/stop) · Benchmarks (create/run/rerun/stop) · Learnings (✓/✗ badges) ·
Docs & summary.

## Settings

| Setting | Default |
|---|---|
| `fox.baseUrl` | `http://localhost:8765` |
| `fox.project` | auto-select first project |

## Note

The agentic **improve loop** runs over the chat WebSocket, which this REST-based
extension does not drive — the panel shows a hint instead. Everything else
(create/focus/status, campaigns, benchmarks, reports, agenda, summary) is
REST-driven.
