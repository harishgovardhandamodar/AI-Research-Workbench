# Environment variables

The most commonly used environment variables (Docker overrides live in
`docker-compose.yml`).

## LLM

| Variable | Default | Meaning |
|---|---|---|
| `FOX_BASE_URL` | `http://localhost:8081/v1` | Gateway (plain chat, model listing) |
| `FOX_TOOL_BASE_URL` | `http://127.0.0.1:11434/v1` | Direct Ollama (tool calling) |
| `FOX_MODEL` | `qwen3.6:latest` | Default model |

## Server

| Variable | Default | Meaning |
|---|---|---|
| `FOX_BIND` | `127.0.0.1` | HTTP bind address |
| `FOX_KERNEL_URL` | — | Remote `fox-kernel` server (headless kernel) |
| `FOX_ORCHESTRATOR` | `classic` | `langgraph` to use the LangGraph state machine |
| `FOX_ORCHESTRATOR_RELIABILITY` | `1` | QA-gate on final answers (langgraph) |

## Editor

| Variable | Default | Meaning |
|---|---|---|
| `FOX_EDITOR_ENABLED` | `1` | Enable the in-browser editor |
| `FOX_EDITOR_URL` | `http://127.0.0.1:8787` | Editor UI URL |
| `FOX_EDITOR_PROBE_URL` | `http://code-server:8080` | Editor reachability probe |
| `FOX_EDITOR_FOLDER` | `/home/coder/workbench` | Folder opened by code-server |
| `CODE_SERVER_BIND` | `0.0.0.0` | code-server bind |
| `CODE_SERVER_AUTH` | `none` | `password` to require a password |
| `CODE_SERVER_PASSWORD` | `fox-workspace` | Password when auth is enabled |

## GPU / environment

| Variable | Default | Meaning |
|---|---|---|
| `FOX_GPU_DEVICES` | `void` | `all` exposes the GPU to nvidia-smi / RKG |
| `FOX_GPU_CAPABILITIES` | `compute,utility` | NVIDIA driver capabilities |
| `FOX_GPU_COUNT` | `1` | GPU reservation count |

## LLM retries (round 12)

`LLMClient` retries transient failures (connection/timeout) with backoff before
raising. Retries/backoff are constructor parameters (`retries=2`,
`retry_backoff=1.0`) — tune in code or by subclassing.
