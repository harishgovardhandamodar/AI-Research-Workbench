# Quick start

## Requirements

- **Python 3.12+** (local install) or **Docker** (containerized)
- A **local Ollama server** (or any OpenAI-compatible endpoint) with a
  tool-calling model (e.g. `qwen3.6:latest`, `glm-4.7-flash:bf16`, `gemma4:31b`;
  tiny models like `llama3.2:3b` work but are less reliable)
- Linux / macOS / Windows (WSL2 recommended for kernels)

## Option A — local (Python)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install numpy pandas scipy matplotlib scikit-learn   # kernel stack
./run.sh                                                            # http://127.0.0.1:8765
```

Open http://127.0.0.1:8765 in a browser. The first chat turn asks Fox to do
something; a persistent Python kernel starts on demand.

## Option B — Docker

```bash
docker compose up -d                        # fox, code-server, ollama-relay
docker compose logs -f fox                  # watch the server boot
```

- Workbench UI: http://localhost:8765 (or `http://<host>:8765`)
- In-browser VS Code: http://localhost:8787

> Docker reaches the host's Ollama via `host.docker.internal` (the
> `fox-ollama-relay` service bridges the tool-calling endpoint). Override with
> `FOX_BASE_URL` / `FOX_TOOL_BASE_URL` if your models live elsewhere.

## First steps

1. **Ask something in chat** — e.g. "load data.csv, explore it, and fit a model".
   Fox runs code in the persistent kernel, produces figures (auto-saved as
   artifacts), and records each turn as a *run*.
2. **Create an experiment** — in the 🧪 Experiments tab, or ask Fox to
   "plan and create an experiment to improve accuracy".
3. **Improve it** — click **🔁 Improve** on an experiment card (or `/improve`)
   to run the reviewer-driven improve loop.
4. **Run a campaign** — click **🧭 Campaign** (or `/campaign <question>`) to plan
   and execute a multi-step investigation in the background.
5. **Generate documentation** — the **📄 Report** button writes the project
   report; **▶ Next** proposes the next research agenda.

## Troubleshooting

- **Chat stuck / "Cannot reach LLM"** — confirm Ollama is running and the model
  exists (`ollama list`); check `FOX_BASE_URL`/`FOX_TOOL_BASE_URL`.
- **Editor "refused to connect"** — ensure the code-server container is up
  (`docker compose up -d code-server`) and reachable on port 8787.
- **Kernel errors** — check the server log (`docker compose logs fox`); the
  kernel auto-restarts on timeout.

See [Troubleshooting](configuration.md#troubleshooting) for more.
