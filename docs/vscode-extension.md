# VS Code extension — Fox Experiment Tracking

A VS Code extension that brings the workbench's experiment-tracking features
into the editor: browse experiments/runs/rankings/campaigns/benchmarks, take
actions (create, focus, improve, run a campaign or eval), and **produce
experimentation documentation** — the project report, the next-research agenda,
and a findings summary — as markdown that opens in VS Code.

## Why an extension
- Data scientists/researchers already live in VS Code; tracking experiments and
  writing up findings there avoids switching to a browser.
- The backend already exposes every needed REST endpoint (`/experiments`,
  `/runs`, `/ranking`, `/campaigns`, `/evals`, `/learnings`, `/compare`,
  `/report`, `/next`) — the extension is a thin, focused client, so no backend
  changes are required.

## Architecture
- **Extension host (Node)** does all HTTP to the workbench (`fetch` to
  `http://localhost:8765`, configurable via `fox.baseUrl`). It relays results to
  the webview via `postMessage`, avoiding CORS entirely.
- **Webview panel** ("Fox · Experiment Tracking") renders the dashboard — it
  posts `{kind, path, method, body}` requests and renders the JSON.
- **Markdown output**: reports/agenda/findings are written to a file under
  `reports/` and opened in the editor (markdown preview) so the user can keep
  them.

## Features
1. **Project selector** — `GET /api/projects`; remembers `fox.project`.
2. **Experiments** — list (name, goal, best, status, runs) with:
   - detail (ranking / runs) on selection;
   - actions: **New experiment**, **Focus**, **Edit status**, **Improve**
     (fires the chat improve loop via the WebSocket-free REST + a notice),
     open the **detail modal**.
3. **Campaigns & benchmarks** — list with status + run/stop buttons; **New
   campaign**, **New eval**.
4. **Learnings** — the accumulated findings, with ✓/✗ badges.
5. **Documentation & summary** (the ask):
   - **Generate report** → `GET /report` → save `reports/<project>-report.md`
     and open it (markdown preview).
   - **Next research** → `GET /next` → show the agenda + proposed campaign.
   - **Summary of findings** → a webview tab combining the report's executive
     summary, learnings, and the experiment leaderboard.

## Files
- `vscode/package.json` — manifest (activation, commands, config, webview).
- `vscode/extension.js` — activation: commands, webview provider, API client,
  markdown file writing.
- `vscode/media/tracking.html|js|css` — webview dashboard.
- `vscode/README.md` — install/usage.
- `docs/vscode-extension.md` — this plan.

## Out of scope
- Live WebSocket streaming in the webview (uses the REST endpoints + manual
  refresh; campaigns/evals show status via `/campaigns`/`/evals` polling).
- Packaging/publishing to the marketplace (local `Install from VSIX` is the
  target).
