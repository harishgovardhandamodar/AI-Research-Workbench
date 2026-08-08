# Configuration

The workbench is configured through environment variables and an in-app
**Settings** modal (⚙ in the top bar). Settings changes (model, reviewer,
management repo, Kaggle credentials) are persisted to `config.json` via
`POST /api/config`.

## LLM

| Setting | Env | Default | Meaning |
|---|---|---|---|
| Gateway URL | `FOX_BASE_URL` | `http://localhost:8081/v1` | Plain-chat/model-listing endpoint |
| Tool URL | `FOX_TOOL_BASE_URL` | `http://127.0.0.1:11434/v1` | Tool-calling endpoint (direct Ollama) |
| Model | `FOX_MODEL` | `qwen3.6:latest` | Default model |
| Temperature | — | 0.2 | Sampling temperature |
| Reviewer | — | on | Background reviewer after each turn |
| Max iterations | — | 8 | Tool-loop budget per turn |

Routing policy: plain chat goes to the gateway; tool-calling turns go to the
direct Ollama endpoint (because some gateways strip `tools`).

## Per-experiment model pinning

Each experiment can pin a model (edit ✎ → *Pinned model*). The coordinator
resolves the pinned model per turn (experiment → focus → global default) and
records it on every run.

## Server / Docker

| Env | Default | Meaning |
|---|---|---|
| `FOX_BIND` | `127.0.0.1` | Server bind address (`0.0.0.0` to expose) |
| `FOX_EDITOR_URL` | `http://127.0.0.1:8787` | In-browser editor URL |
| `FOX_EDITOR_PROBE_URL` | `http://code-server:8080` | Editor reachability probe |
| `CODE_SERVER_BIND` | `0.0.0.0` | code-server bind |
| `CODE_SERVER_AUTH` | `none` | `password` to require `CODE_SERVER_PASSWORD` |
| `FOX_GPU_DEVICES` | `void` | `all` to expose the GPU to nvidia-smi/RKG |
| `FOX_KERNEL_URL` | — | Point at a remote `fox-kernel` server |

## Experiment management repo

- **Repo path** — a sibling git worktree that snapshots every experiment run
  (`fox/<project>/experiments.json`, `runs/<id>.json`, `artifacts/`, `data/`).
- **Auto-commit / auto-push** — commit (and optionally push) after each
  experiment run; `/commit` and `/push` do it manually.
- **GitHub remote** — `owner/repo` for the `origin` remote; commits get web URLs.

## Kaggle

Set a **Kaggle username + key** in Settings to enable `/kaggle <owner/dataset>`,
which imports a public dataset into the project's `data/`.

## UI switches

- `?flat=1` plain bubbles (default) · `?sets=1` grouped collapsible sets
- Type `/flat` or `/sets` in chat to toggle without the URL.

## Troubleshooting

- **"Cannot reach LLM server"** — Ollama down or wrong `FOX_TOOL_BASE_URL`;
  verify with `ollama list` and `curl <tool_url>/models`.
- **Models missing from the dropdown** — the gateway (`FOX_BASE_URL`) is used for
  listing; the native Ollama `/api/tags` enriches size/quantization labels.
- **Slow first run** — the kernel + model both load lazily; subsequent turns are
  fast.
- **Editor unreachable** — `docker compose up -d code-server`, then hard-refresh
  and re-open the 🖊 Editor tab.
