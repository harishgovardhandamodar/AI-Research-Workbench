# Fox — Setup Instructions

Setup, running (locally and via Docker), LAN serving, and how to cluster local
Ollama serving to use **all** available compute — including multi-node
orchestration over your LAN.

---

## 1. Prerequisites

- **Python 3.12+** and a virtualenv (`.venv`)
- A local **Ollama** server (or any OpenAI-compatible endpoint) with the models you
  want to use (`ollama pull llama3.2:3b`, `qwen3.6:latest`, …)
- **Git** (and **Git-LFS** if you pull the demo datasets — see below)
- Optional: an NVIDIA/AMD GPU with CUDA/ROCm, an in-browser VS Code (`code-server`),
  and Docker for the containerized path

> **Git-LFS:** the `examples/autoresearch/creditcard/data/creditcard.csv` dataset is
> a Git-LFS file. Install git-lfs first:
> ```bash
> # Ubuntu/Debian: sudo apt install git-lfs   ·   macOS: brew install git-lfs
> git lfs install
> ```

---

## 2. Run locally (Python)

```bash
git clone <your-fork>  # or cd into the repo
cd AI-Research-Workbench

# 1. Create the environment and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install numpy pandas scipy matplotlib scikit-learn

# 2. Make sure Ollama is up with a model
ollama serve &            # background
ollama pull qwen3.6:latest

# 3. Start Fox
./run.sh                  # -> http://127.0.0.1:8765
```

`run.sh` is `uvicorn backend.main:app --host 127.0.0.1 --port 8765`. Open
**Settings** and set the gateway / tool endpoints:

| Setting | Default | Purpose |
|---|---|---|
| Gateway base URL | `http://localhost:8081/v1` | Chat + model list (a hive gateway, or Ollama directly) |
| Direct Ollama URL | `http://127.0.0.1:11434/v1` | Tool-calling turns (Ollama keeps `tools`) |
| Model | — | e.g. `qwen3.6:latest` |

If you have no hive gateway, point both at Ollama
(`http://127.0.0.1:11434/v1`). See §6 for clustering.

### Run as a Jupyter addon (`/fox`)

```bash
./run-jupyter.sh     # Jupyter server extension; Fox at http://localhost:8888/fox/
```

---

## 3. Run via Docker

```bash
# Build the fox image (installs the scientific stack + MCP + git)
FOX_BIND=0.0.0.0 docker compose up -d --build fox
# fox        -> http://127.0.0.1:8765
# code-server-> http://127.0.0.1:8787 (optional in-browser editor)
# ollama-relay-> 11435 (forwards to host Ollama on 11434)

# Bring everything up (editor + relay):
docker compose up -d
```

Environment overrides (see `docker-compose.yml`):

| Var | Default | Purpose |
|---|---|---|
| `FOX_BASE_URL` | `http://host.docker.internal:8081/v1` | Gateway for chat/model list |
| `FOX_TOOL_BASE_URL` | `http://host.docker.internal:11435/v1` | Direct endpoint for tool calls |
| `FOX_MODEL` | `qwen3.6:latest` | Default model |
| `FOX_BIND` | `127.0.0.1` | Bind address (see §4) |

Persistent data (projects, kernels, config) lives in the `fox_data` volume at
`/app/workbench`.

---

## 4. Serve via LAN

The compose default binds to `127.0.0.1` only. To expose the UI to the LAN:

```bash
FOX_BIND=0.0.0.0 docker compose up -d --build fox
# now reachable from any device on the LAN:  http://<host-lan-ip>:8765
```

For a **local** (non-Docker) run, start with `--host 0.0.0.0`:

```bash
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8765
```

Notes:

- The WebSocket origin check requires the browser to hit the **same host:port**
  it was loaded from, so use the machine's LAN IP consistently.
- Find your IP: `hostname -I` (Linux) / `ipconfig getifaddr en0` (macOS).
- If you run behind a reverse proxy, also set
  `FOX_ALLOWED_ORIGINS=<comma-separated origins>`.

---

## 5. Platform-specific setup

The generic sections 2–4 apply everywhere; this section covers each platform's
specifics (Python, Ollama + GPU acceleration, and quirks).

### 5.1 macOS (Apple Silicon)

- **Python:** `brew install python@3.12` (or python.org installer).
- **Ollama:** `brew install ollama` then `ollama serve`. Uses **Metal** (MPS)
  automatically — no CUDA. Everything runs on unified memory, so budget model
  size + context against your RAM (16/32/64 GB).
- **Tune for unified memory:**
  ```bash
  export OLLAMA_MAX_LOADED_MODELS=3
  export OLLAMA_NUM_PARALLEL=2
  export OLLAMA_KV_CACHE_TYPE=q8_0     # smaller KV cache -> bigger models fit
  export OLLAMA_HOST=0.0.0.0:11434     # optional: serve to the LAN
  ollama serve
  ```
- **Run Fox:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && ./run.sh`
- **Docker:** Docker Desktop (arm64 images work natively); `host.docker.internal`
  resolves to the Mac. `FOX_BIND=0.0.0.0` exposes the UI to the LAN.
- **MLX alternative:** `pip install mlx-lm` serves Apple-optimized models. In a
  hive cluster map tags so forwarding works: `MESH_MODEL_MAP="qwen3.6:latest-mlx->qwen3.6:latest"`.

### 5.2 Linux — NVIDIA DGX Spark (Arm64 / GB10)

- The DGX Spark is **Arm64** with an integrated Grace-Blackwell GPU (Ubuntu 22.04,
  ~128 GB unified memory). Install the NVIDIA driver + CUDA build for aarch64
  (Ollama ships an aarch64 CUDA build).
- **Verify:** `nvidia-smi` shows the GB10 GPU.
- **Ollama + env:**
  ```bash
  ollama serve &
  export OLLAMA_MAX_LOADED_MODELS=4
  export OLLAMA_NUM_PARALLEL=4
  export OLLAMA_FLASH_ATTENTION=1
  export OLLAMA_KV_CACHE_TYPE=q8_0
  ```
- **Run Fox:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && ./run.sh`
- **Docker:** `docker compose up -d --build` (arm64 image). This machine defaults
  to the hive gateway for chat and direct Ollama for tools — adjust under
  **Settings** if you run plain Ollama.
- **LAN:** `FOX_BIND=0.0.0.0 docker compose up -d --build fox`.

### 5.3 Linux — NVIDIA GPU (x86_64)

- Install the NVIDIA driver + CUDA toolkit (or let Ollama pull its CUDA runtime).
- Ollama auto-detects every GPU and splits a model across them when it doesn't
  fit one card. Tune (see also §6):
  ```bash
  export CUDA_VISIBLE_DEVICES=0,1,2,3        # which GPUs Ollama uses
  export OLLAMA_MAX_LOADED_MODELS=4
  export OLLAMA_NUM_PARALLEL=4
  export OLLAMA_FLASH_ATTENTION=1
  export OLLAMA_GPU_OVERLAP=1
  export OLLAMA_KV_CACHE_TYPE=q8_0
  ollama serve
  ```
- Verify all GPUs are used: `nvidia-smi` while a model is loaded, or
  `curl localhost:11434/api/ps`.
- **Run Fox:** same as §2. **Docker:** same as §3 (x86_64 image).
- Multi-node: add this machine to a hive mesh (see §7).

### 5.4 Windows — WSL2

- **Recommended:** run everything inside WSL2 Ubuntu (best GPU + tooling story).
  ```powershell
  wsl --install -d Ubuntu
  wsl
  # inside WSL:
  sudo apt update && sudo apt install -y python3-venv git
  curl -fsSL https://ollama.com/install.sh | sh
  ollama serve &
  ```
- NVIDIA drivers on the Windows host are passed through to WSL automatically
  (WSL2 CUDA). Verify with `nvidia-smi` inside WSL.
- **Run Fox in WSL:** clone the repo under `~/`, create `.venv`, `./run.sh`; the
  UI is reachable from Windows at `http://localhost:8765`.
- **Docker:** Docker Desktop with the WSL2 backend; `host.docker.internal` works.
  `FOX_BIND=0.0.0.0` exposes the UI to the LAN.

### 5.5 Windows — native (PowerShell)

- **Python:** `winget install Python.Python.3.12` (check "Add to PATH"), or
  python.org.
- **Ollama:** `winget install Ollama.Ollama` — the native Windows Ollama uses your
  NVIDIA GPU via CUDA (AMD/Intel NPU on newer builds). `ollama serve` runs as a
  background service.
- **Git:** `winget install Git.Git` (includes Git-LFS).
- **Run Fox:**
  ```powershell
  git clone <your-fork>; cd AI-Research-Workbench
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
  .venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
  ```
- **Docker:** Docker Desktop (Windows containers off, Linux containers on);
  `host.docker.internal` resolves to the host.
- **LAN:** Docker `FOX_BIND=0.0.0.0`, or start uvicorn with `--host 0.0.0.0`.
- **Tool calls:** native Windows Ollama serves `tools` fine — set
  `FOX_TOOL_BASE_URL` to `http://127.0.0.1:11434/v1`.

---

## 6. Ollama serving — use ALL local compute (multi-GPU)

Ollama already uses every visible GPU on one host for a single model, splitting
layers across GPUs when the model is too big for one card. Tune for maximum
throughput with environment variables (set them before `ollama serve`):

```bash
# Single host, all GPUs
export CUDA_VISIBLE_DEVICES=0,1,2,3        # constrain which GPUs Ollama sees
export OLLAMA_MAX_LOADED_MODELS=4          # keep several models resident
export OLLAMA_NUM_PARALLEL=4               # parallel requests per model
export OLLAMA_MAX_QUEUE=512                # don't drop bursts
export OLLAMA_GPU_OVERLAP=1                # overlap compute/copy (newer builds)
export OLLAMA_FLASH_ATTENTION=1            # faster KV cache on supported GPUs
ollama serve
```

Check GPU usage: `nvidia-smi` (NVIDIA) or `rocm-smi` (AMD). Verify Ollama sees all
devices: `curl http://localhost:11434/api/ps` after loading a model, or the
`/api/tags` + dashboard.

> **Why it matters for Fox:** tool-calling turns go straight to Ollama
> (`FOX_TOOL_BASE_URL`), so a beefy local Ollama makes the agent's tool loop much
> faster. The gateway (chat path) can point at the same Ollama or at a cluster.

---

## 7. Compute orchestration over LAN (cluster several machines)

To use **every machine's GPUs on the network**, run one **Hive Server Go**
gateway on the LAN and point Fox at it. Hive is an OpenAI-compatible inference
orchestrator that queues jobs, discovers peers over UDP, and auto-forwards models
to whichever node has them.

### Architecture

```
Fox (workbench)
 ├─ FOX_BASE_URL      -> http://<hive>:8081/v1      (chat, model list, load-balancing)
 ├─ FOX_TOOL_BASE_URL -> http://<worker>:11434/v1    (tool calls → one Ollama)
 └─ /v1/models        -> union of every peer's models (auto-forwarding)

Hive Server Go (on a node, e.g. 192.168.1.50)
 ├─ :8081 HTTP API (OpenAI-compatible) + dashboard
 ├─ :8082/UDP mesh discovery
 └─ peers: worker A (RTX 4090), worker B (2× A100), worker C (Mac MPS, via model map)
```

### Set up workers

Each worker just runs Ollama with its GPUs and advertises itself (see §5):

```bash
# worker node
ollama serve &                    # bind 0.0.0.0 or a LAN IP
curl http://<worker-lan-ip>:11434/api/tags    # sanity check
```

### Set up the Hive gateway

From the hive cluster repo
(`Ollama-local-hives-cluster`, or `hive-serving-local-Cluster`):

```bash
# Docker (recommended) — on the gateway node
docker run -d --name hive-server \
  -p 8081:8081 -p 8082:8082/udp \
  -v hive-data:/data \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e OLLAMA_MODEL=qwen3.6:latest \
  -e MESH_ENABLED=true \
  -e MESH_SEED_PEERS="192.168.1.51:8081,192.168.1.52:8081" \
  -e MESH_ANNOUNCE_ADDRESS="192.168.1.50:8081" \
  hive-server-go:latest

# or build/run locally
cd Ollama-local-hives-cluster && go build -o hive-server-go ./hive-server-go/
OLLAMA_BASE_URL=http://localhost:11434 MESH_ENABLED=true \
  MESH_SEED_PEERS="192.168.1.51:8081" MESH_ANNOUNCE_ADDRESS="192.168.1.50:8081" \
  ./hive-server-go
```

Open `http://<gateway>:8081` for the live dashboard (queue, mesh topology, token
usage). Confirm peers: `curl http://<gateway>:8081/api/status`.

### Point Fox at the cluster

- **Local Fox:** Settings → Gateway base URL = `http://<gateway-ip>:8081/v1`
- **Docker Fox:** set `FOX_BASE_URL=http://<gateway-ip>:8081/v1` and rebuild

```bash
FOX_BASE_URL=http://192.168.1.50:8081/v1 \
FOX_TOOL_BASE_URL=http://192.168.1.51:11434/v1 \
docker compose up -d --build fox
```

### Cross-platform model map

If a peer runs MLX (Apple) with a different tag, map names so forwarding works:

```bash
-e MESH_MODEL_MAP="gemma4:31b-mlx->gemma4:31b,llama3.2:3b-mlx->llama3.2:3b"
```

### Firewall / networking

- Open `8081/tcp` (API) and `8082/udp` (discovery) between peers.
- Ollama workers: open `11434/tcp` for the gateway (or use the `ollama-relay`).
- Use static IPs or hostnames in `MESH_SEED_PEERS`; UDP broadcast discovery covers
  peers on the same subnet even without seeds.

---

## 8. Verify everything

```bash
curl http://<host>:8765/api/health          # workbench
curl http://<gateway>:8081/api/status       # hive cluster + peers
curl http://localhost:11434/api/tags        # a worker's models
```

Then open the workbench, pick a model, run a chat turn and an experiment — both
loops (improve + autoresearch) stream progress to the workflow panel and record
runs on the Experiments timeline.

---

## Troubleshooting

- **"git-lfs: not found"** — install git-lfs (`sudo apt install git-lfs` /
  `brew install git-lfs`) then `git lfs install`.
- **No models / connection fails** — Ollama up? `curl localhost:11434/api/tags`.
  Then Settings → Test connection.
- **Tool calls not working** — the gateway may strip `tools`; set
  `FOX_TOOL_BASE_URL` to a real Ollama.
- **LAN clients refused** — the server was started bound to `127.0.0.1`; use
  `FOX_BIND=0.0.0.0` (Docker) or `--host 0.0.0.0` (local).
- **Cluster models missing** — check Hive `/api/status`; ensure `MESH_SEED_PEERS`
  are reachable and `MESH_MODEL_MAP` covers cross-platform tags.
