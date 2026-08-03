"""Tool registry: schemas for the LLM + execution functions used by the coordinator.

Every tool execution is recorded in the conversation as a `tool` message so the
workflow is fully auditable. Figures produced by kernels are auto-registered as
artifacts with full provenance (code + environment snapshot).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..artifacts.store import Artifact, ArtifactStore
from ..experiments import record_experiment
from ..kernels.manager import KernelManager
from ..notebooks import NotebookError, NotebookService
from ..permissions import PermissionManager
from ..store import ProjectStore

ToolFn = Callable[..., Awaitable[str]]


@dataclasses.dataclass
class ToolContext:
    kernels: KernelManager
    artifacts: ArtifactStore
    store: ProjectStore
    permissions: PermissionManager
    approval: "ApprovalBroker | None" = None
    emit: Callable[[str, dict], Awaitable[None]] | None = None
    notebooks: NotebookService | None = None
    workflow: "WorkflowTracker | None" = None
    message_id: str = ""
    run_id: str = ""


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code in the persistent scientific kernel "
                "(numpy/pandas/scipy/matplotlib). Variables persist between calls. "
                "Figures rendered with matplotlib are automatically saved as artifacts. "
                "Use this for computations, data analysis, and plots."
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Python code to run"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_r",
            "description": "Execute R code. Requires Rscript to be installed.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "R code to run"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command in the session workspace directory. "
                "Network access and destructive commands require explicit user approval. "
                "Prefer run_python for data work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command line"},
                    "timeout": {"type": "number", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_artifact",
            "description": (
                "Save a named, human-readable text/table artifact (e.g. a results table, "
                "a summary, a data CSV) with full provenance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short artifact name"},
                    "description": {"type": "string", "description": "What this artifact is"},
                    "content": {"type": "string", "description": "Text/CSV/JSON content"},
                    "kind": {"type": "string", "description": "table|text|data", "default": "text"},
                },
                "required": ["name", "description", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_kernel_variables",
            "description": "List variables currently held in the persistent Python kernel.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_notebook",
            "description": (
                "Execute code cells of a Jupyter notebook (.ipynb) experiment and store "
                "the results (outputs, figures, errors) in the notebook. Notebooks live in "
                "the project's notebooks/ folder, or in examples/notebooks/. Prefer this over "
                "run_python when the user wants results held in a notebook."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "notebook": {"type": "string",
                                 "description": "Notebook name, e.g. 'my_experiment' or '01_simple_decay_fit'"},
                    "cells": {"type": "string",
                              "description": "'all' or comma-separated 0-based cell indices (default 'all')"},
                },
                "required": ["notebook"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_notebook",
            "description": (
                "Create a new Jupyter notebook in the project's notebooks/ folder with a "
                "markdown title cell and one code cell containing `code`. Returns the notebook name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Notebook name (no extension needed)"},
                    "code": {"type": "string", "description": "Initial Python code for the first code cell"},
                },
                "required": ["name", "code"],
            },
        },
    },
]


def get_tool_schemas() -> list[dict]:
    return json.loads(json.dumps(TOOL_SCHEMAS))


async def _run_python(ctx: ToolContext, code: str) -> str:
    env = await ctx.kernels.get_env()
    resp = await ctx.kernels.python.run_code(code)
    parts = []
    if resp.get("output"):
        parts.append(resp["output"].rstrip())
    if resp.get("error"):
        parts.append(f"[error] {resp['error']}")
    artifact_ids = []
    for i, fig in enumerate(resp.get("figures") or [], start=1):
        data = base64.b64decode(fig)
        art = Artifact(kind="figure", name=f"figure-{i}",
                       description="Matplotlib figure generated by kernel code",
                       code=code, env=env,
                       message_id=ctx.message_id, run_id=ctx.run_id)
        ctx.artifacts.add_artifact(art, data=data, data_type="png")
        artifact_ids.append(art.id)
    if artifact_ids:
        parts.append("Figures generated (artifacts): " + ", ".join(artifact_ids))
        if ctx.emit:
            for aid in artifact_ids:
                art = ctx.artifacts.get(aid)
                if art:
                    await ctx.emit("artifact", {"artifact": art.to_dict()})
    if resp.get("variables"):
        parts.append("Kernel variables: " + json.dumps(resp["variables"]))
    if not parts:
        parts.append("(no output)")
    return "\n".join(parts)


async def _run_r(ctx: ToolContext, code: str) -> str:
    from ..kernels.r_kernel import RUnavailableError
    try:
        resp = await ctx.kernels.r.run_code(code)
    except RUnavailableError as e:
        return f"[error] {e}"
    parts = []
    if resp.get("output"):
        parts.append(resp["output"].rstrip())
    if resp.get("error"):
        parts.append(f"[error] {resp['error']}")
    if not parts:
        parts.append("(no output)")
    return "\n".join(parts)


async def _run_shell(ctx: ToolContext, command: str, timeout: float = 30.0) -> str:
    decision = ctx.permissions.check("run_shell", command)
    if decision == "deny":
        return "[denied] This command is blocked by the permission policy."
    if decision == "ask":
        if ctx.approval is None:
            return "[denied] This command requires approval but no approval channel is available."
        ok, temporary = await ctx.approval.request("run_shell", command,
                                                   "Shell command requires approval")
        if not ok:
            return "[denied by user]"
        if not temporary:
            ctx.permissions.record("run_shell", command, "allow")
    # Run via a real shell so quoting, pipes and redirects work as the model expects.
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", command,
        cwd=str(ctx.kernels.workspace_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "[error] shell command timed out"
    text = out.decode(errors="replace")
    if err:
        text += ("\n[stderr]\n" + err.decode(errors="replace"))
    return text[:50_000] if text else "(no output)"


async def _save_artifact(ctx: ToolContext, name: str, description: str,
                         content: str, kind: str = "text") -> str:
    env = await ctx.kernels.get_env()
    art = Artifact(kind=kind, name=name, description=description,
                   code="# saved manually", env=env,
                   message_id=ctx.message_id, run_id=ctx.run_id)
    ctx.artifacts.add_artifact(art, data=content.encode(), data_type="text")
    if ctx.emit:
        await ctx.emit("artifact", {"artifact": art.to_dict()})
    return f"Saved artifact {art.id} ({kind}: {name})"


async def _list_vars(ctx: ToolContext) -> str:
    vars_ = await ctx.kernels.python.list_variables()
    return json.dumps(vars_, indent=2) if vars_ else "(kernel has no user variables)"


async def _notebook_metrics(ctx: ToolContext) -> dict:
    """Best-effort numeric metrics from the last notebook execution.

    Mirrors the chat-rerun path (run_notebook_intent): any structured result
    the notebook helper exposed (e.g. clean/robust accuracy) plus a fallback
    that pulls labelled numbers straight from the kernel.
    """
    metrics: dict = {}
    try:
        resp = await ctx.kernels.python.run_code(
            "import json\n"
            "try:\n"
            "    from examples.adversarial import adversarial_data as _ad\n"
            "    _d = getattr(_ad, 'LAST_RESULT', {}) or {}\n"
            "except Exception:\n"
            "    _d = {}\n"
            "print(json.dumps(_d))")
        out = (resp.get("output") or "").strip()
        if out:
            parsed = json.loads(out.splitlines()[-1])
            if isinstance(parsed, dict):
                metrics = {k: v for k, v in parsed.items()
                           if isinstance(v, (int, float))}
    except Exception:  # noqa: BLE001
        pass
    return metrics


async def _run_notebook(ctx: ToolContext, notebook: str, cells: str = "all") -> str:
    if ctx.notebooks is None:
        return "[error] notebook support is unavailable for this session"
    indices = None
    if cells and cells.strip() != "all":
        try:
            indices = [int(x) for x in cells.split(",") if x.strip()]
        except ValueError:
            return "[error] cells must be 'all' or comma-separated indices like '0,2'"
    collected = []

    async def on_artifact(fig_b64: str, source: str):
        env = await ctx.kernels.get_env()
        data = base64.b64decode(fig_b64)
        art = Artifact(kind="figure", name="notebook-figure",
                       description=f"Figure produced by notebook '{notebook}'",
                       code=source, env=env,
                       message_id=ctx.message_id, run_id=ctx.run_id)
        ctx.artifacts.add_artifact(art, data=data, data_type="png")
        collected.append(art.to_dict())
        if ctx.emit:
            await ctx.emit("artifact", {"artifact": art.to_dict()})
        return art

    try:
        res = await ctx.notebooks.execute(notebook, indices,
                                          on_artifact=on_artifact)
    except NotebookError as e:
        return f"[error] {e}"
    nb = res["notebook"]
    lines = [f"Executed {len(res['report'])} code cell(s) in notebook '{notebook}'."]
    for r in res["report"]:
        status = "ok" if r["ok"] else "FAILED"
        extra = f" ({r['figures']} figure(s), {r['outputs']} output(s))" if r["ok"] else f": {r['error'][:200]}"
        lines.append(f"  cell {r['index']}: {status}{extra}")
    result = "\n".join(lines)

    # Record the experiment the same way as the chat-rerun path so the agent's
    # notebook runs appear on the Experiments timeline/graph too.
    try:
        metrics = await _notebook_metrics(ctx)
        record_experiment({
            "id": f"nb-tool-{int(time.time())}",
            "kind": "notebook",
            "label": notebook,
            "seed": None,
            "fresh": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "artifacts": [{"name": a["name"], "id": a["id"]} for a in collected],
            "source": "agent-tool",
        })
    except Exception:  # noqa: BLE001
        pass
    return result


async def _create_notebook(ctx: ToolContext, name: str, code: str) -> str:
    if ctx.notebooks is None:
        return "[error] notebook support is unavailable for this session"
    from ..notebooks import new_notebook
    safe = ctx.notebooks._safe(name)
    nb = new_notebook([
        {"cell_type": "markdown", "source": f"# {safe}\n"},
        {"cell_type": "code", "source": code},
    ], safe)
    ctx.notebooks.save(safe, nb)
    return (f"Created notebook '{safe}' in the project's notebooks/ folder with "
            f"a title cell and one code cell. Run it with the run_notebook tool.")


def build_tools(ctx: ToolContext) -> dict[str, ToolFn]:
    return {
        "run_python": lambda code: _run_python(ctx, code),
        "run_r": lambda code: _run_r(ctx, code),
        "run_shell": lambda command, timeout=30: _run_shell(ctx, command, timeout),
        "save_artifact": lambda name, description, content, kind="text":
            _save_artifact(ctx, name, description, content, kind),
        "list_kernel_variables": lambda: _list_vars(ctx),
        "run_notebook": lambda notebook, cells="all": _run_notebook(ctx, notebook, cells),
        "create_notebook": lambda name, code="": _create_notebook(ctx, name, code),
    }
