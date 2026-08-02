"""Fox — AI Science Workbench: FastAPI backend.

Serves the web UI + JSON REST API + WebSocket chat. Each project gets its own
folder under <workbench>/projects with SQLite persistence, artifact storage and a
persistent Python kernel.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agents.approval import ApprovalBroker
from .agents.coordinator import Coordinator
from .agents.reviewer import Reviewer
from .agents.tools import ToolContext
from .artifacts.store import Artifact, ArtifactStore
from .kernels.manager import KernelManager
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TOOL_BASE_URL, LLMClient, LLMError
from .permissions import PermissionManager
from .store import ProjectStore

ROOT = Path(__file__).resolve().parent.parent
WORKBENCH_DIR = Path("/home/fox/WorkBook/AI-Research-Workbench/workbench")
PROJECTS_DIR = WORKBENCH_DIR / "projects"
CONFIG_PATH = WORKBENCH_DIR / "config.json"
FRONTEND_DIR = ROOT / "frontend"

DEFAULT_CONFIG = {
    "llm": {
        "base_url": DEFAULT_BASE_URL,
        "tool_base_url": DEFAULT_TOOL_BASE_URL,
        "model": DEFAULT_MODEL,
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "agent": {"max_iters": 8, "reviewer_enabled": True},
}

DEFAULT_SYSTEM_PROMPT = (
    "You are Fox, an open-source AI science workbench running fully on the user's "
    "machine with local models. You help computational biologists, chemists, "
    "physicists and data scientists run real analyses hands-on.\n\n"
    "Working style:\n"
    "- Solve problems by writing and running code in a persistent, sandboxed Python "
    "kernel (numpy, pandas, scipy, matplotlib). Variables persist across calls.\n"
    "- Use run_python for computation, analysis and figures. Make clear, well-labelled "
    "publication-style matplotlib figures.\n"
    "- Save important results (tables, summaries, data) with save_artifact so they "
    "become auditable artifacts.\n"
    "- Use run_shell only when necessary; it asks the user for permission. Prefer the "
    "Python kernel.\n"
    "- Every figure records its exact code and environment so it can be reproduced.\n"
    "- Be rigorous: cite numbers you actually computed. If you don't know, say so.\n\n"
    "Privacy: everything stays on the user's machine unless they explicitly approve a "
    "network-touching shell command."
)


# ---------------------------------------------------------------- config -----

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["llm"].update(saved.get("llm", {}))
            cfg["agent"].update(saved.get("agent", {}))
            return cfg
        except json.JSONDecodeError:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


CONFIG = load_config()


def make_llm() -> LLMClient:
    llm_cfg = CONFIG["llm"]
    return LLMClient(
        base_url=llm_cfg.get("base_url", DEFAULT_BASE_URL),
        tool_base_url=llm_cfg.get("tool_base_url", DEFAULT_TOOL_BASE_URL),
        model=llm_cfg.get("model", DEFAULT_MODEL),
        temperature=llm_cfg.get("temperature", 0.2),
        max_tokens=llm_cfg.get("max_tokens", 4096),
    )


# ------------------------------------------------------------ project runtime --

class ProjectRuntime:
    def __init__(self, name: str):
        self.name = name
        self.dir = PROJECTS_DIR / name
        self.store = ProjectStore(self.dir)
        self.artifacts = ArtifactStore(self.dir)
        self.kernels = KernelManager(self.dir)
        self.permissions = PermissionManager(self.store)
        self.lock = asyncio.Lock()
        self.llm = make_llm()
        self.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        self.max_iters = CONFIG["agent"].get("max_iters", 8)

    def ctx(self, emit, approval) -> ToolContext:
        return ToolContext(kernels=self.kernels, artifacts=self.artifacts,
                           store=self.store, permissions=self.permissions,
                           approval=approval, emit=emit)

    def build_llm_messages(self) -> list[dict]:
        rows = self.store.list_messages()
        msgs: list[dict] = []
        for r in rows:
            role = r["role"]
            meta = r.get("meta") or {}
            if role == "system":
                continue
            if role == "user":
                msgs.append({"role": "user", "content": r["content"]})
            elif role == "assistant":
                d = {"role": "assistant", "content": r["content"]}
                tcs = meta.get("tool_calls")
                if tcs:
                    d["tool_calls"] = tcs
                msgs.append(d)
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""),
                             "content": r["content"]})
        return sanitize_messages(msgs)

    async def stop(self):
        await self.kernels.stop()


def sanitize_messages(msgs: list[dict]) -> list[dict]:
    """Ensure OpenAI tool-call history is well-formed (tool results follow calls)."""
    clean: list[dict] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            remaining = msgs[i + 1:]
            call_ids = {tc.get("id") for tc in m["tool_calls"]}
            if not any(r.get("role") == "tool" and r.get("tool_call_id") in call_ids
                       for r in remaining):
                m = {"role": "assistant", "content": m.get("content", "")}
        clean.append(m)
    return clean


# ------------------------------------------------------------------ app -----

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for rt in list(runtimes.values()):
        await rt.stop()


app = FastAPI(title="Fox — AI Science Workbench", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

runtimes: dict[str, ProjectRuntime] = {}
_llm_cache: LLMClient | None = None


def get_runtime(name: str) -> ProjectRuntime:
    if name not in runtimes:
        runtimes[name] = ProjectRuntime(name)
    return runtimes[name]


def get_llm() -> LLMClient:
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = make_llm()
    return _llm_cache


# ------------------------------------------------------------- REST: config --

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return {"config": CONFIG}


@app.post("/api/config")
async def set_config(body: dict):
    global _llm_cache
    cfg = body.get("config", {})
    if "llm" in cfg:
        CONFIG["llm"].update(cfg["llm"])
    if "agent" in cfg:
        CONFIG["agent"].update(cfg["agent"])
    save_config(CONFIG)
    _llm_cache = None
    for rt in runtimes.values():
        rt.llm = make_llm()
        rt.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        rt.max_iters = CONFIG["agent"].get("max_iters", 8)
    return {"config": CONFIG}


@app.get("/api/models")
async def list_models():
    try:
        return {"models": await get_llm().list_models()}
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)


# --------------------------------------------------------- REST: projects ---

@app.get("/api/projects")
async def list_projects():
    out = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                rt = get_runtime(d.name)
                msgs = rt.store.list_messages()
                arts = rt.artifacts.list()
                out.append({
                    "name": d.name,
                    "messages": len(msgs),
                    "artifacts": len(arts),
                    "updated": d.stat().st_mtime if hasattr(d, "stat") else 0,
                })
    return {"projects": out}


@app.post("/api/projects")
async def create_project(body: dict):
    name = (body.get("name") or "").strip().replace("/", "_")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    d = PROJECTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    get_runtime(name)
    return {"name": name}


# -------------------------------------------------- REST: project state ------

@app.get("/api/projects/{name}/state")
async def project_state(name: str):
    rt = get_runtime(name)
    msgs = rt.store.list_messages()
    arts = rt.artifacts.list()
    grants = rt.store.list_grants()
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        env = {}
    try:
        vars_ = await rt.kernels.python.list_variables()
    except Exception:  # noqa: BLE001
        vars_ = {}
    return {"name": name, "messages": msgs, "artifacts": arts, "grants": grants,
            "env": env, "variables": vars_}


@app.get("/api/projects/{name}/artifacts")
async def list_artifacts(name: str):
    return {"artifacts": get_runtime(name).artifacts.list()}


@app.get("/artifacts/{artifact_id}")
async def artifact_file(artifact_id: str):
    for rt in runtimes.values():
        data = rt.artifacts.data(artifact_id)
        if data is not None:
            art = rt.artifacts.get(artifact_id)
            media = {"png": "image/png", "svg": "image/svg+xml",
                     "html": "text/html", "text": "text/plain"}.get(
                art.data_type if art else "text", "application/octet-stream")
            return FileResponse(rt.artifacts.artifacts_dir / Path(art.data_path).name,
                                media_type=media)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/projects/{name}/artifacts/{artifact_id}")
async def delete_artifact(name: str, artifact_id: str):
    rt = get_runtime(name)
    return {"deleted": rt.artifacts.delete(artifact_id)}


@app.post("/api/projects/{name}/kernel/reset")
async def reset_kernel(name: str):
    rt = get_runtime(name)
    await rt.kernels.reset()
    return {"ok": True}


@app.get("/api/projects/{name}/grants")
async def list_grants(name: str):
    return {"grants": get_runtime(name).store.list_grants()}


@app.delete("/api/projects/{name}/grants/{grant_id}")
async def delete_grant(name: str, grant_id: str):
    rt = get_runtime(name)
    with rt.store._conn:
        cur = rt.store._conn.execute("DELETE FROM grants WHERE id=?", (int(grant_id),))
    return {"deleted": cur.rowcount > 0}


# ---------------------------------------------------------- regenerate -------

REGEN_PROMPT = """\
You are modifying Python code in a scientific workbench. Here is the original code
that produced a figure:

```python
{code}
```

The user wants this change: "{instruction}"

Respond with ONLY the complete, modified Python code in a single fenced code block.
Do not explain. Preserve any existing variable names so kernel state stays consistent.
"""


@app.post("/api/projects/{name}/regenerate")
async def regenerate(name: str, body: dict):
    rt = get_runtime(name)
    artifact_id = body.get("artifact_id", "")
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return JSONResponse({"error": "instruction required"}, status_code=400)
    art = rt.artifacts.get(artifact_id)
    if not art:
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    code = art.code
    try:
        resp = await rt.llm.complete(
            [{"role": "system", "content": REGEN_PROMPT.format(code=code, instruction=instruction)}],
            temperature=0.1,
        )
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    text = resp.get("content", "")
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    new_code = m.group(1).strip() if m else text.strip()
    if not new_code:
        return JSONResponse({"error": "model returned no code"}, status_code=502)
    env = await rt.kernels.get_env()
    kernel_resp = await rt.kernels.python.run_code(new_code)
    new_art = None
    figs = kernel_resp.get("figures") or []
    if figs:
        import base64

        data = base64.b64decode(figs[0])
        new_art = Artifact(kind="figure", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=data, data_type="png")
    else:
        new_art = Artifact(kind="text", name=art.name + " (regenerated)",
                           description=f"Regenerated from {art.name}: {instruction}",
                           code=new_code, env=env, message_id="")
        rt.artifacts.add_artifact(new_art, data=(kernel_resp.get("output") or "").encode(),
                                  data_type="text")
    return {"artifact": new_art.to_dict(),
            "output": kernel_resp.get("output", ""),
            "error": kernel_resp.get("error", "")}


# ---------------------------------------------------------- WebSocket ---------

@app.websocket("/ws/projects/{name}")
async def ws_chat(ws: WebSocket, name: str):
    await ws.accept()
    rt = get_runtime(name)

    async def emit(event: str, payload: dict):
        try:
            await ws.send_json({"type": event, "payload": payload})
        except Exception:  # noqa: BLE001
            pass

    broker = ApprovalBroker(emit)
    coordinator = Coordinator(rt.llm, rt.ctx(emit, broker), emit=emit,
                              persist=lambda r, c, m: rt.store.add_message(r, c, m),
                              max_iters=rt.max_iters)

    async def receive_loop():
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "approval":
                broker.resolve(msg.get("request_id", ""), bool(msg.get("decision")))
            elif mtype == "ping":
                await emit("pong", {})

    recv_task = asyncio.create_task(receive_loop())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "chat":
                continue
            text = (msg.get("content") or "").strip()
            if not text:
                continue
            async with rt.lock:
                try:
                    mid = rt.store.add_message("user", text)
                    await emit("user_message", {"id": mid, "content": text})
                    llm_msgs = rt.build_llm_messages()
                    result = await coordinator.run_turn(llm_msgs)
                    amid = rt.store.add_message("assistant", result.get("text", ""))
                    await emit("assistant_message", {"id": amid, "content": result.get("text", "")})
                    if rt.reviewer_enabled:
                        await emit("review_start", {})
                        try:
                            findings = await Reviewer(rt.llm, rt.store).review()
                            await emit("review", {"findings": findings})
                        except Exception:  # noqa: BLE001
                            await emit("review", {"findings": []})
                    await emit("done", {})
                except LLMError as e:
                    await emit("error", {"message": str(e)})
                except Exception as e:  # noqa: BLE001
                    await emit("error", {"message": f"{type(e).__name__}: {e}"})
    except WebSocketDisconnect:
        pass
    finally:
        recv_task.cancel()


# ------------------------------------------------------------ static files ---

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
