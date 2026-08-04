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
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import editor as editor_cfg
from ..artifacts.store import Artifact, ArtifactStore
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
    experiment_id: str = ""
    experiment_config: dict | None = None
    last_metrics: dict | None = None
    # Artifact ids produced by the most recent tool call, so the coordinator can
    # record exact run↔artifact linkage instead of parsing tool text.
    last_artifact_ids: list = dataclasses.field(default_factory=list)
    variant: dict | None = None
    finished_variants: list = dataclasses.field(default_factory=list)


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
    {
        "type": "function",
        "function": {
            "name": "create_experiment",
            "description": (
                "Create a structured experiment: a family of runs grouped around one "
                "research question with a hypothesis, optional goal metric/target, and "
                "config variations. Call this BEFORE running the experiment so runs are "
                "attached to it and can be compared across variants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short experiment name, e.g. 'DP vs synthetic on income'"},
                    "hypothesis": {"type": "string", "description": "One-sentence hypothesis the experiment tests"},
                    "plan": {"type": "string", "description": "Concise experiment plan: hypothesis, the goal metric/target, the configs or variables to try (explicit list), and the stopping criteria for the experiment"},
                    "goal_metric": {"type": "string", "description": "Headline metric name, e.g. 'accuracy'"},
                    "goal_target": {"type": "number", "description": "Target value for the goal metric"},
                    "higher_better": {"type": "boolean", "description": "Whether larger goal_metric is better", "default": True},
                    "config": {"type": "object", "description": "Baseline config (hyperparameters/parameters) for the first run"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_run",
            "description": (
                "Start an explicit run variant of the current experiment. Call this "
                "before running the code for a single configuration point so that run "
                "gets its own label and config, making it comparable against other "
                "variants (and the baseline). A baseline run is started automatically "
                "with the experiment's config; use start_run to mark a specific variant "
                "instead. Pair with finish_run once the variant's code has run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short variant label, e.g. 'eps=1.0', 'batch-64', 'baseline'"},
                    "config": {"type": "object", "description": "This variant's config (hyperparameters/parameters)"},
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_run",
            "description": (
                "Finish the run variant started by start_run. Records the metrics "
                "reported via report_metric during the variant's code and any notes, "
                "and closes the variant so the next start_run begins a fresh one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {"type": "string", "description": "Optional free-text notes on this variant's result"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editor__list_files",
            "description": (
                "List the files in this project's workspace (the folder the in-browser "
                "VS Code editor shows): artifacts, notebooks, knowledge_graphs, project "
                "files. Use before reading/editing so paths are exact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Relative folder to list (default '.')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editor__read_file",
            "description": (
                "Read a generated-content file (report, notebook, knowledge-graph JSON, "
                "project file) in the VS Code workspace. Returns the text (capped)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'artifacts/1_report.md'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editor__edit_file",
            "description": (
                "Apply a text replacement to a file in the VS Code workspace so generated "
                "content can be fixed/improved in place. The exact `old` text must appear "
                "exactly once. Writes require user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the file to edit"},
                    "old": {"type": "string", "description": "Exact existing text to replace"},
                    "new": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editor__open",
            "description": (
                "Open the in-browser VS Code editor at this project's workspace (optionally "
                "on a specific file), e.g. to let the user review or continue editing "
                "generated content by hand."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Optional relative file/folder to focus, e.g. 'artifacts/1_report.md'"},
                },
            },
        },
    },
]


def get_tool_schemas() -> list[dict]:
    return json.loads(json.dumps(TOOL_SCHEMAS))


async def _run_python(ctx: ToolContext, code: str) -> str:
    env = await ctx.kernels.get_env()
    resp = await ctx.kernels.python.run_code(code)
    ctx.last_metrics = resp.get("metrics") or {}
    parts = []
    if resp.get("output"):
        parts.append(resp["output"].rstrip())
    if resp.get("error"):
        parts.append(f"[error] {resp['error']}")
    metrics = ctx.last_metrics
    if metrics:
        parts.append("[metrics] " + json.dumps(metrics))
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
        ctx.last_artifact_ids.extend(artifact_ids)
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
    ctx.last_artifact_ids.append(art.id)
    if ctx.emit:
        await ctx.emit("artifact", {"artifact": art.to_dict()})
    return f"Saved artifact {art.id} ({kind}: {name})"


async def _create_experiment(ctx: ToolContext, name: str, hypothesis: str = "",
                             goal_metric: str = "", goal_target: float | None = None,
                             higher_better: bool = True,
                             config: dict | None = None,
                             plan: str = "") -> str:
    name = (name or "").strip()
    if not name:
        return "[error] experiment name is required"
    try:
        eid = ctx.store.create_experiment(
            name, hypothesis or "", goal_metric or "",
            float(goal_target) if goal_target is not None else None,
            bool(higher_better), plan=plan or "")
    except Exception as e:  # noqa: BLE001
        return f"[error] could not create experiment: {e}"
    ctx.experiment_id = str(eid)
    ctx.experiment_config = dict(config or {})
    line = (f"Experiment #{eid} created: {name!r} (runs will be attached to it).\n"
            f"Hypothesis: {hypothesis or '(none)'}\n"
            f"Plan: {(plan or '(none)').strip()}\n"
            f"Goal: {goal_metric or '(none)'}"
            + (f" target={goal_target}, higher_better={higher_better}" if goal_target is not None else ""))
    if ctx.experiment_config:
        line += "\nConfig: " + json.dumps(ctx.experiment_config)
    line += ("\nUse report_metric('" + (goal_metric or "my_metric")
             + "', value) inside run_python code so each run records its headline metric.")
    return line


async def _list_vars(ctx: ToolContext) -> str:
    vars_ = await ctx.kernels.python.list_variables()
    return json.dumps(vars_, indent=2) if vars_ else "(kernel has no user variables)"


async def _start_run(ctx: ToolContext, label: str, config: dict | None = None) -> str:
    label = (label or "").strip()
    if not label:
        return "[error] a variant label is required (e.g. 'baseline', 'eps=1.0')"
    if ctx.variant is not None:
        return ("[error] a variant run is already active: " +
                f"{ctx.variant.get('label') or '(unlabeled)'}. "
                "Call finish_run first to close it before starting a new one.")
    ctx.variant = {"label": label, "config": dict(config or {}), "metrics": {},
                   "notes": ""}
    lines = [f"Started variant run: {label}"]
    if ctx.variant["config"]:
        lines.append("Config: " + json.dumps(ctx.variant["config"]))
    lines.append("Run the variant's code now, then call finish_run.")
    return "\n".join(lines)


async def _finish_run(ctx: ToolContext, notes: str = "") -> str:
    v = ctx.variant
    if v is None:
        return ("[error] no variant run is active. Call start_run with a label "
                "before running the variant's code.")
    v["notes"] = (notes or "").strip()
    metrics = dict(v.get("metrics") or {})
    lines = [f"Finished variant run: {v.get('label') or '(unlabeled)'}"]
    if metrics:
        lines.append("Metrics: " + json.dumps(metrics))
    if v["notes"]:
        lines.append("Notes: " + v["notes"])
    lines.append("The run will be recorded under this variant's label/config.")
    ctx.finished_variants.append(v)
    ctx.variant = None
    return "\n".join(lines)


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
        ctx.last_artifact_ids.append(art.id)
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

    # Record the run in the project's runs table (same source of truth as agent
    # runs and notebook-intent runs) so it appears on the Experiments timeline
    # and graph with its metrics and artifacts.
    try:
        metrics = await _notebook_metrics(ctx)
        ctx.store.add_run(
            prompt=f"run notebook {notebook}",
            reply=result,
            status="done",
            started_at=time.time(), finished_at=time.time(),
            artifact_ids=[a["id"] for a in collected],
            metrics=metrics,
            kind="notebook",
            label=notebook,
        )
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


# ------------------------------------------------------------- editor tools --
# The in-browser VS Code (code-server) works on the same volume as the workbench
# projects, so the agent's editor__* tools operate on the same generated content
# the user sees in the Editor tab.

def _editor_root(ctx: ToolContext) -> Path:
    return Path(ctx.artifacts.project_dir)


def _editor_safe(ctx: ToolContext, rel: str) -> Path | None:
    root = _editor_root(ctx).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        return None
    return p


async def _editor_list_files(ctx: ToolContext, path: str = ".") -> str:
    base = _editor_safe(ctx, path)
    if base is None or not base.is_dir():
        return f"[error] not a valid workspace folder: {path}"
    skip = {"workbench.db", "workbench.db-wal", "workbench.db-shm"}
    lines = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if p.name in skip:
            continue
        rel = p.relative_to(_editor_root(ctx))
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        lines.append(f"{rel} ({size} B)")
    if not lines:
        return "(workspace has no files yet)"
    head = f"Workspace files under '{path}' ({len(lines)}):"
    return "\n".join([head] + lines)[:50_000]


async def _editor_read_file(ctx: ToolContext, path: str) -> str:
    p = _editor_safe(ctx, path)
    if p is None or not p.is_file():
        return f"[error] file not found in workspace: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[error] could not read {path}: {e}"
    if len(text) > 40_000:
        text = text[:40_000] + "\n…[truncated]"
    return f"--- {path} ---\n{text}"


async def _editor_edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    p = _editor_safe(ctx, path)
    if p is None or not p.is_file():
        return f"[error] file not found in workspace: {path}"
    decision = ctx.permissions.check("editor_edit_file", path)
    if decision == "deny":
        return "[denied] Editing this file is blocked by the permission policy."
    if decision == "ask":
        if ctx.approval is None:
            return "[denied] This edit requires approval but no approval channel is available."
        ok, temporary = await ctx.approval.request(
            "editor_edit_file", path, f"Apply an edit to {path} in the VS Code workspace")
        if not ok:
            return "[denied by user]"
        if not temporary:
            ctx.permissions.record("editor_edit_file", path, "allow")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"[error] could not read {path}: {e}"
    count = text.count(old)
    if count == 0:
        return f"[error] the `old` text was not found in {path} (0 matches)."
    if count > 1:
        return f"[error] the `old` text matches {count} times; make it more specific."
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
    except OSError as e:
        return f"[error] could not write {path}: {e}"
    return (f"Edited {path}: replaced 1 occurrence "
            f"(removed {len(old)} chars, added {len(new)} chars). "
            f"The change is visible in the Editor tab.")


async def _editor_open(ctx: ToolContext, path: str | None = None) -> str:
    base = editor_cfg.editor_url().rstrip("/")
    folder = editor_cfg.editor_folder()
    url = f"{base}/?folder={folder}"
    if path:
        safe = _editor_safe(ctx, path)
        if safe is None or not safe.exists():
            return f"[error] file not found in workspace: {path}"
        url = f"{base}/?folder={folder}#{path}"
    return (f"Open the in-browser VS Code editor to edit generated content: {url}\n"
            f"(Also available via the 'Editor' tab in the top bar. If it shows a login, "
            f"the code-server password is the one set by CODE_SERVER_PASSWORD.)")


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
        "create_experiment": lambda name, hypothesis="", goal_metric="",
            goal_target=None, higher_better=True, config=None, plan="":
            _create_experiment(ctx, name, hypothesis, goal_metric, goal_target,
                               higher_better, config, plan),
        "start_run": lambda label, config=None: _start_run(ctx, label, config),
        "finish_run": lambda notes="": _finish_run(ctx, notes),
        "editor__list_files": lambda path=".": _editor_list_files(ctx, path),
        "editor__read_file": lambda path: _editor_read_file(ctx, path),
        "editor__edit_file": lambda path, old, new: _editor_edit_file(ctx, path, old, new),
        "editor__open": lambda path=None: _editor_open(ctx, path),
    }
