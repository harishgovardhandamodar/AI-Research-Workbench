# Finetune status & live monitoring

The workbench can **watch LoRA/QLoRA training jobs** from the [dk-lora MCP
server](../../mcp_servers/dk_lora) and surface them in two places: a **status
panel** in the Experiments tab, and a **live pipeline card + streaming debug
log** inside the chat window of the session that owns the job.

The workbench only *reads* the dk-lora workspace on disk — it never starts or
stops training itself. That means a job launched from the CLI (e.g. via a
pipeline script) streams into the GUI just the same as one launched from the
UI.

## The dk-lora workspace

Training jobs live in a **workspace directory**:

```
<workspace>/
  jobs/<job_id>.json      job record (config, status, output_dir)
  jobs/<job_id>.log       live training log (tqdm progress + metrics)
  jobs/<job_id>.py        generated training script
  datasets/*.jsonl        prepared datasets
  configs/*.json          training configs
```

The workspace is resolved in order from:

1. `finetune.workspace` in the global config (settable from the UI)
2. the `FOX_DK_LORA_WORKSPACE` environment variable
3. `~/.fox/dk-lora`

## Experiments tab — Finetune status panel

The **Finetune status** section (Experiments tab) shows:

- the active workspace path, with a **Set workspace** input to repoint it;
- one card per training job: id, status badge (running / done / failed), backend
  and dataset, created age, live **step/total** progress bar, and the last
  reported **loss / epoch**;
- a **▾ log** toggle that lazily loads the job's log tail (last 8000 chars) plus
  its metric history from `GET /api/finetune/jobs/{id}`.

While any job is running the panel auto-refreshes every 5s.

## Chat window — live pipeline card

Opening (or reconnecting to) a session that owns a finetune job pushes a 🔧
**LoRA finetune** card into the chat that renders the whole pipeline:

| Stage | What it tracks |
|---|---|
| Ingest + chunk corpus | ingested artifacts & chunks in the workspace |
| Build training dataset | prepared dataset + example count |
| Train LoRA adapter | active job id · step/total · running loss |
| Verify base vs adapter | verification runs (ft-validate) |

Each stage shows a state icon (○ queued / ◔ running / ✓ done / ✗ failed), the
pipeline % is filled on a progress bar, and a **debug log** console streams the
live log lines appended by the trainer (tqdm progress redraws are collapsed to
their latest frame, ANSI escapes stripped).

### Events

The backend runs a small per-session monitor that tails the running job's log
every ~2s and broadcasts two events on the chat WebSocket:

| Event | Payload | Meaning |
|---|---|---|
| `finetune_pipeline` | `{pipeline: snapshot}` | Stage states + progress; sent on connect and whenever the snapshot changes |
| `finetune_log` | `{job, lines}` | New debug log lines since the last tick |

The log console caps at 600 lines to keep the DOM bounded.

## Session history

The monitor also persists a **compact finetune history** into the session's
messages (tagged `finetune`/`pipeline`) so the chat keeps a readable story:

- a **start** message when the job is first seen running;
- a **progress** message every 100 steps (loss/epoch + pipeline %);
- a **done / failed** message with final loss, epoch, error (if any), output
  adapter path, and a folded-in log tail.

These messages replay on reload as assistant bubbles with a collapsible
pipeline snapshot attached, so the finetune session history survives page
refresh and project switches.

## MCP servers

The workbench ships **two MCP servers** for this feature, registered in the
Agents tab (Settings → MCP):

- **`dk_lora`** — Domain Knowledge LoRA fine-tuning (16 tools: ingest, chunk,
  dataset, configure/start/cancel training, status, export, register + chat).
- **`ft_validate`** — RAG verification (16 tools: build/list RAG indexes,
  retrieve, generate eval sets incl. custom, run verification, reports).

Both run as stdio servers (`python -m mcp_servers.dk_lora.server` /
`mcp_servers.ft_validate.server`) with `PYTHONPATH` pointing at the repo.

## REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/finetune/status` | All training jobs with live progress + last metrics |
| GET | `/api/finetune/jobs/{job_id}` | One job: record + log tail + metric history |
| GET | `/api/finetune/pipeline` | The pipeline snapshot (stages 1–4) for the chat card |
| POST | `/api/finetune/workspace` | Point the status view at a dk-lora workspace |

See also [Finetune launch](finetune-launch.md) for the UI launch flow that
creates the training script, and the [dk-lora](../../mcp_servers/dk_lora) and
[ft-validate](../../mcp_servers/ft_validate) MCP servers that run the training
and verification.
