# Editing generated content with VS Code (in-browser editor)

The workbench ships with a **VS Code editor in the browser** (powered by
[code-server](https://github.com/coder/code-server), the maintained way to run
VS Code on a server) that shares the same data volume as the workbench. Every
artifact the agent produces — lab-notebook reports, Jupyter notebooks,
knowledge-graph JSON, project files — can be opened, reviewed and edited in a
full VS Code UI without leaving the app. The agent can also drive the editor
programmatically as part of its workflow.

> Why code-server and not the `vscode` source repo? The
> `harishgovardhandamodar/vscode` fork is the complete VS Code codebase
> (160k+ commits); building it from source needs the whole Node toolchain and
> is impractical as a sidecar. code-server *is* VS Code, built and packaged —
> this integration uses it so you get the real editor without the build.

---

## 1. Start everything

```bash
docker compose up -d --build
```

This starts the workbench (`http://127.0.0.1:8765`) **and** the editor sidecar
(`http://127.0.0.1:8787`). Both containers mount the same named volume
(`fox_data`), so they see exactly the same files.

Verify the editor is up:

```bash
curl http://127.0.0.1:8787/        # -> 302 redirect to the login page (normal)
curl http://127.0.0.1:8765/api/editor
```

The second command returns something like:

```json
{"editor":{"enabled":true,"url":"http://127.0.0.1:8787","folder":"/home/coder/workbench","reachable":true}}
```

- `reachable: true` — the editor is running; the **Editor** tab will show it.
- `reachable: false` — start the sidecar with `docker compose up -d code-server`.

---

## 2. Open the editor

1. Open <http://127.0.0.1:8765>.
2. Click the **Editor** tab in the top bar (next to Chat / Experiments / Agent).
3. The VS Code UI loads in the tab, rooted at the **workbench data folder**.

**First-time login:** none — the editor has **auth disabled by default** and
opens straight to the workbench folder. To require a password instead:

```bash
CODE_SERVER_AUTH=password \
CODE_SERVER_PASSWORD=my-secret \
docker compose up -d --build
```

If the iframe is blocked (some browsers/extensions refuse cross-origin
iframes), use **"Open in new tab"** in the Editor tab's toolbar — it's the same
editor, just in its own tab.

---

## 3. What you can edit

The editor opens `/home/coder/workbench` (inside the code-server container),
which is the **same volume** the workbench mounts at `/app/workbench`. All
per-project generated content lives under `projects/<name>/`:

| Path (relative to project)   | Contents                                                        |
| ---------------------------- | --------------------------------------------------------------- |
| `artifacts/`                 | Saved artifacts: reports (`*.md`), tables, text outputs         |
| `notebooks/`                 | Jupyter notebooks (`*.ipynb`) created/run by the agent          |
| `knowledge_graphs/`          | Per-paper arXiv knowledge graphs (`<arxiv_id>.json`, `corpus.json`) |
| `files/`                     | Uploaded project files                                          |
| `workbench.db`               | Project SQLite store (leave alone!)                             |

Because the workbench and the editor share the volume, changes are **live both
ways**:

- Edit a report in VS Code → it updates in the workbench's Files/Artifacts views.
- The agent saves a new artifact → it appears in the editor's file tree instantly.

---

## 4. Agent-driven editing (the "agentic workflow")

The agent can operate on the same files programmatically. It has four
`editor__*` tools that the model can call mid-conversation:

| Tool                 | What it does                                                        |
| -------------------- | ------------------------------------------------------------------- |
| `editor__list_files` | List workspace files for the current project (paths are exact).     |
| `editor__read_file`  | Read a generated file (report, notebook JSON, knowledge-graph JSON). |
| `editor__edit_file`  | Apply an exact text replacement to a file (asks for approval).      |
| `editor__open`       | Returns the editor URL, optionally focused on a specific file.      |

Example conversation:

> "Fix the summary in my last report to say 0.62 instead of 0.50, then open it
> in the editor so I can review the rest."

The agent will `editor__read_file` to find the text, `editor__edit_file` to
apply the fix (you approve the write), and `editor__open` to hand you the file
in the Editor tab. `editor__edit_file` follows the same permission model as
`run_shell` — the write appears as a permission prompt in the chat window.

### Path safety

`editor__*` tools validate paths against the current project directory.
Paths like `../../etc/passwd` are rejected — the agent can only touch the
project's own files.

---

## 5. Path mapping (container vs. editor)

The fox container and the code-server container mount the **same volume** at
different paths:

| Container        | Volume mount                  | You see in the editor         |
| ---------------- | ----------------------------- | ----------------------------- |
| fox-workbench    | `fox_data` → `/app/workbench` | n/a (backend)                 |
| fox-code-server  | `fox_data` → `/home/coder/workbench` | the root folder of the editor |

So `/app/workbench/projects/default/artifacts/...` (fox) is the same file as
`/home/coder/workbench/projects/default/artifacts/...` (editor). You generally
don't need to think about this — it only matters if you point the editor's
"Open Folder" somewhere: always use `/home/coder/workbench`.

---

## 6. Configuration

Everything is optional and env-driven:

| Variable                | Default                        | Meaning                                        |
| ----------------------- | ------------------------------ | ---------------------------------------------- |
| `CODE_SERVER_AUTH`      | `none`                         | `none` = no login; `password` = require `CODE_SERVER_PASSWORD` |
| `CODE_SERVER_PASSWORD`  | `fox-workspace`                | Editor login password (only used with `CODE_SERVER_AUTH=password`) |
| `FOX_EDITOR_ENABLED`    | `1`                            | Set `0` to disable the editor integration      |
| `FOX_EDITOR_URL`        | `http://127.0.0.1:8787`        | Browser-visible editor URL. When left at the default, the backend auto-derives it from the host you browse from (loopback locally, your network IP on a remote machine). |
| `FOX_EDITOR_PROBE_URL`  | `http://code-server:8080`      | Internal URL the fox service probes (compose network) |
| `FOX_EDITOR_FOLDER`     | `/home/coder/workbench`        | Folder the editor opens                        |

Example:

```bash
CODE_SERVER_PASSWORD=swordfish \
FOX_EDITOR_URL=http://127.0.0.1:8787 \
docker compose up -d --build
```

---

## 7. Troubleshooting

**Editor tab says "code-server unreachable"**
- Start the sidecar: `docker compose up -d code-server`, then click Refresh.
- Check it's listening: `docker compose ps` (port `8787->8080`) and
  `curl -sI http://127.0.0.1:8787/`.
- If the probe URL is wrong for your network, set `FOX_EDITOR_PROBE_URL`.

**"127.0.0.1 refused to connect" when accessing from another machine**
- When you open the app via a **network IP** (e.g. `http://192.168.1.50:8765`),
  the editor URL is automatically derived from the host you browsed from
  (`http://192.168.1.50:8787`) instead of loopback. Make sure port `8787` is
  open in the remote machine's firewall.
- If you set `FOX_EDITOR_URL` explicitly, that value is used verbatim (no
  auto-derivation) — set it to the remote host if needed.
- The reachability probe always uses `FOX_EDITOR_PROBE_URL` inside the Docker
  network, so it's unaffected by where you browse from.

**Login screen in the iframe / "Open in new tab" is needed**
- Auth is **disabled by default** — a login page means an older container is
  still running with `--auth password`. Recreate it: `docker compose up -d
  code-server` (the current compose file defaults to `--auth none`).
- If you deliberately enabled `CODE_SERVER_AUTH=password`, use the password you
  set (default `fox-workspace`). If your browser blocks the cross-origin
  iframe, use **Open in new tab** — identical editor.

**Can't save a file / "permission denied"**
- The sidecar runs as `root` so the shared volume is writable. If you changed
  `user:` in `docker-compose.yml`, make sure the mount is writable by that user.

**The agent's edit was denied**
- `editor__edit_file` asks for approval in chat, like `run_shell`. Approve it,
  or allow it permanently when prompted. Denies are one-off.

**Files the agent just created don't show in the editor tree**
- The editor's file explorer may need a refresh (`Ctrl/Cmd+Shift+P` →
  "File: Refresh Explorer" or restart the editor window).

**I don't want the editor at all**
- `FOX_EDITOR_ENABLED=0 docker compose up -d --build`, or just don't start the
  `code-server` service; the Editor tab then shows the "not reachable" fallback.

---

## 8. Using a real (native) VS Code instead

If you prefer your local VS Code over the in-browser one, open the workbench
volume directly from the repo checkout:

```bash
# local dev (not Docker): the data lives in ./workbench
code ./workbench/projects/default
```

For the Docker setup, the data is in the `fox_data` named volume, so use the
code-server UI (Section 2) or `docker compose cp` files out as needed. The
in-browser editor is the intended path so you never have to leave the app.
