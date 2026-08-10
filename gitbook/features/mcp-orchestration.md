# MCP servers: management & orchestration

The workbench acts as an MCP **host**. Its built-in servers (science, privacy,
robustness, arxiv, graphrag, github, autoresearch, dk_lora, ft_validate, flint,
the experiment planner, and the EDA suite) plus any you add are merged into the
agent's tool set as namespaced tools (`server__tool`). This page covers managing
those servers and calling their tools directly from the Chat and Experiments
tab — no LLM required.

## Managing servers

**Settings → MCP** (and the Experiments tab's **MCP** section) lets you:

- **Add / edit / remove** servers (stdio or streamable HTTP), with a **trusted**
  flag that skips approval prompts.
- **Enable / disable** each server — disabled servers are neither probed for
  health nor offered to the agent.
- **Refresh** to re-probe health; **status + tool catalog** (name, description,
  read-only, and required params) come from `GET /api/mcp`.

MCP `env`/`header` tokens and kaggle keys are **redacted** from the config the
browser sees, and saving a redacted config preserves your live secrets.

## Calling tools from Chat

Type in the chat box (deterministic — no model round-trip):

```
@mcp <server>__<tool> [json args]
@mcp bg <server>__<tool> [json args]   # run in the background
```

- `@mcp` alone **lists** every connected server's tools with their required
  arguments.
- Results are JSON pretty-printed into a code block; very large results point at
  the recorded run instead of overflowing the chat.
- **Read-only** tools run freely. **Writable** tools ask for approval (the same
  permission model the agent uses) — a temporary or remembered grant is
  honored; background calls finish with a notice.

Every direct call is recorded as a run (`kind="mcp_tool"`), so it appears in the
Experiments timeline.

## Natural-language charting

Ask in plain words and the Flint charts MCP renders a chart of your project's
dataset — no spec needed (deterministic, no model round-trip):

```
make a distribution of transaction type
histogram of amount (INR)
scatter amount (INR) vs sender_bank
correlation between amount (INR) and sender_bank
trend of amount (INR) over hour_of_day
```

The request is parsed to a Flint spec, rendered via `flint__render_chart`, and
the PNG is registered as a figure artifact and posted inline in chat. If the
Node-based flint server is unavailable it falls back to a deterministic
matplotlib renderer, so charts always work. Unknown column names produce a
helpful message listing the dataset's columns.

## Calling tools from the Experiments tab

The **MCP** section shows every server (health, trust, enable/disable) and a
filterable tool catalog. Each tool offers **▶ Call**:

- Tools with scalar parameters get a **schema-driven form** (typed inputs,
  required marked); complex tools keep a JSON box.
- Run **synchronously** or **⏳ background** (poll the run until it finishes).
- **📈 track as experiment** attaches the call to an experiment and parses flat
  numeric metrics from a JSON reply into the run.
- **🔓 Allow** grants a writable tool on demand; **💾 Save as artifact**,
  **📋 Copy** and **↻ Re-run** round out the result actions.
- **Recent MCP calls** lists your last direct calls with one-click re-run.

## Permissions

Writable MCP tools are deny-by-default. A call returns a clear "needs
permission" message with the tool's key; you can grant it once from the panel,
or grant/revoke explicitly via `GET/POST …/mcp/grants`. Read-only tools never
prompt.

## Endpoints

- `GET /api/mcp` · `POST /api/mcp/refresh` — health + tool catalog
- `POST /api/mcp/servers/{name}/enabled` · `PATCH /api/mcp/servers/{name}` ·
  `POST/DELETE /api/mcp/servers` — management
- `POST /api/mcp/tools/{server}/{tool}` — invoke (sync/background/experiment)
- `GET/POST …/mcp/grants` · `POST …/mcp/artifacts` · `GET …/mcp/activity`
