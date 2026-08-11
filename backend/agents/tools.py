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

# Upper bound on ephemeral kernels a single parameter sweep may spawn; larger
# grids run their excess points sequentially on the main kernel.
MAX_SWEEP_KERNELS = 8


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
    last_dataset: str | None = None
    # God mode: when set, shell/agent work is confined to this quarantined
    # folder (full-access turns).
    quarantine_dir: str = ""
    # Audit emitter for the local agent audit trail (optional).
    audit: "Any | None" = None
    # The run this turn derives from (improve loops / reruns / branching).
    parent_run_id: "int | None" = None
    # Artifact ids produced by the most recent tool call, so the coordinator can
    # record exact run↔artifact linkage instead of parsing tool text.
    last_artifact_ids: list = dataclasses.field(default_factory=list)
    variant: dict | None = None
    finished_variants: list = dataclasses.field(default_factory=list)
    # Ephemeral kernels (e.g. parameter-sweep pools) owned by in-flight tools.
    # Registered so a cancelled/aborted turn still stops them (see
    # ``stop_kernels``) instead of leaking subprocesses.
    active_kernels: list = dataclasses.field(default_factory=list)
    # Cooperative abort signal (the coordinator's check_abort), consulted by
    # long-running tools (sweeps) so a stopped turn unwinds between points.
    check_abort: Callable[[], bool] | None = None

    async def stop_kernels(self) -> None:
        """Stop any ephemeral kernels still registered (sweep pools on abort).
        Idempotent; safe to call from a ``finally`` block."""
        if not self.active_kernels:
            return
        kerns, self.active_kernels = list(self.active_kernels), []
        stop = getattr(self.kernels, "stop_pool", None)
        if stop and kerns:
            await asyncio.shield(stop(kerns))

    def register_kernels(self, kernels: list) -> None:
        """Track ephemeral kernels so they're stopped if the turn is aborted."""
        self.active_kernels.extend(kernels)

    def unregister_kernels(self, kernels: list) -> None:
        for k in kernels:
            try:
                self.active_kernels.remove(k)
            except ValueError:
                pass


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
            "name": "run_sweep",
            "description": (
                "Run a parameter sweep: execute the SAME code once per config "
                "point, in parallel kernels, and record one run per config. The "
                "code must read its parameters from a `config` dict and report "
                "its headline metric(s) via report_metric(name, value). Use for "
                "grid searches (e.g. sweep eps over [0.5, 1, 2]) — preferred "
                "over repeating start_run/finish_run for a grid."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string",
                             "description": "Python code that uses `config` and calls report_metric(...)"},
                    "configs": {"type": "array",
                                "items": {"type": "object"},
                                "description": "List of config dicts, one per sweep point"},
                    "label_prefix": {"type": "string",
                                     "description": "Prefix for each run's label (optional)"},
                },
                "required": ["code", "configs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_finetune",
            "description": (
                "Set up a finetune launch for the active experiment: build the "
                "finetune config (base model, dataset, hyperparameters) and record "
                "a kind=finetune run with a generated training script, so the "
                "pipeline and advisor track it. The script is not executed — "
                "run it afterwards with run_python to actually train."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "base_model": {"type": "string",
                                   "description": "HuggingFace base model id to finetune from (e.g. distilbert-base-uncased)"},
                    "dataset": {"type": "string",
                                "description": "Path to the training dataset file (CSV readable by pandas)"},
                    "epochs": {"type": "integer", "description": "Number of training epochs (default 3)"},
                    "learning_rate": {"type": "number", "description": "Learning rate (default 2e-5)"},
                    "batch_size": {"type": "integer", "description": "Per-device batch size (default 8)"},
                    "lora_r": {"type": "integer",
                               "description": "LoRA rank; 0 (default) = full finetune, >0 = LoRA adapter"},
                    "task": {"type": "string",
                             "description": "Task type, e.g. classification (default)"},
                },
                "required": ["base_model", "dataset"],
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
    {
        "type": "function",
        "function": {
            "name": "rkg__query_rag",
            "description": (
                "Query the shared Research Knowledge Graph (arXiv corpus + notes) using "
                "retrieval-augmented generation. Returns a grounded answer plus the source "
                "papers it is drawn from. Use this to ground claims in published literature, "
                "find related work, or answer literature questions from the corpus."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string",
                                            "description": "Research question to ask the knowledge graph"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rkg__paper_notes",
            "description": (
                "Look up a paper in the shared Research Knowledge Graph by arXiv id and "
                "return its title, abstract, tags, concepts and the vault notes summary. "
                "Use this before citing a paper so citations are grounded in the corpus."
            ),
            "parameters": {
                "type": "object",
                "properties": {"paper_id": {"type": "string",
                                            "description": "arXiv id, e.g. '2401.12345'"}},
                "required": ["paper_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rkg__scenario_status",
            "description": (
                "Get the live status of a Research Workbench scenario (a domain-scoped "
                "autoresearch loop over the knowledge graph): current phase, progress, "
                "corpus size, best report score. Scenario ids include "
                "'autonomous-agents-security' and 'enterprise-ai-security'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"scenario_id": {"type": "string",
                                               "description": "Scenario id, e.g. 'autonomous-agents-security'"}},
                "required": ["scenario_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rkg__scenario_report",
            "description": (
                "Read the current best research report of a Research Workbench scenario. "
                "The report is a Markdown literature knowledge map with sections and "
                "[arXiv:xxxx] citations. Use this to incorporate domain survey findings "
                "into your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {"scenario_id": {"type": "string",
                                               "description": "Scenario id, e.g. 'autonomous-agents-security'"}},
                "required": ["scenario_id"],
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
    ctx.last_dataset = resp.get("dataset") or None
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


async def _run_sweep(ctx: ToolContext, code: str, configs: list,
                     label_prefix: str = "") -> str:
    """Parallel parameter sweep: run `code` (which reads `config` and calls
    report_metric) once per config on independent kernels, recording one run per
    config under the active experiment. Falls back to sequential on the main
    kernel when the kernel manager has no pool (remote mode)."""
    import asyncio

    configs = [dict(c) for c in configs or []]
    if not configs:
        return "[error] run_sweep needs at least one config"
    label_prefix = (label_prefix or "").strip()
    eid = int(ctx.experiment_id) if str(ctx.experiment_id).isdigit() else None
    parent_id = ctx.parent_run_id
    if parent_id is None and eid is not None:
        runs = ctx.store.experiment_runs(eid) if ctx.store else []
        if runs:
            exp = ctx.store.get_experiment(eid) if ctx.store else None
            goal = (exp or {}).get("goal_metric")
            higher = bool((exp or {}).get("higher_better", True))
            best, best_id = None, None
            if goal:
                for r in runs:
                    m = (r.get("metrics") or {}).get(goal)
                    if m is None:
                        continue
                    try:
                        m = float(m)
                    except (TypeError, ValueError):
                        continue
                    if best is None or (m > best if higher else m < best):
                        best, best_id = m, r.get("id")
                parent_id = best_id
            if parent_id is None:
                parent_id = runs[-1].get("id")

    # Labels must be unique per sweep so run rows are distinguishable; a
    # configured label that would collide gets an index suffix.
    _used_labels: set = set()

    def _label(i: int, cfg: dict) -> str:
        if label_prefix:
            return f"{label_prefix}·{i}"
        base = str(cfg.get("label") or f"point {i}")
        if base not in _used_labels:
            _used_labels.add(base)
            return base
        disamb = f"{base}#{i}"
        _used_labels.add(disamb)
        return disamb

    # Per-sweep environment snapshot (cached by the kernel manager — cheap).
    env = {}
    try:
        env = await ctx.kernels.get_env()
    except Exception:  # noqa: BLE001
        pass

    kernels = []
    t0 = time.time()
    rows = []
    parallel_n = 0
    # Sequential points run on the main kernel and overwrite its `config`
    # variable; capture the prior value so it can be restored afterwards.
    prev_config = None
    try:
        _vars = await ctx.kernels.python.list_variables()
        prev_config = _vars.get("config")
    except Exception:  # noqa: BLE001
        prev_config = None
    try:
        if ctx.check_abort is not None and ctx.check_abort():
            return "[error] aborted before the sweep started"
        pool = getattr(ctx.kernels, "pool", None)
        # Cap the pool so a huge grid can't spawn hundreds of subprocesses; the
        # excess points run sequentially on the main kernel.
        if pool:
            parallel_n = min(len(configs), MAX_SWEEP_KERNELS)
            kernels = list(pool(parallel_n))
            if kernels:
                ctx.register_kernels(kernels)
        if kernels:
            async def one(k, i, cfg):
                return await _sweep_point(ctx, k, code, cfg, _label(i, cfg),
                                          parent_id, eid, i, env)
            rows = await asyncio.gather(
                *[one(k, i, c) for i, (k, c) in enumerate(
                    zip(kernels, configs[:parallel_n]), 1)])
        for i, cfg in enumerate(configs[parallel_n:], parallel_n + 1):
            if i == parallel_n + 1 and parallel_n and ctx.emit is not None:
                # Let the user know the pool was capped and the rest run serially.
                try:
                    await ctx.emit("notice", {"message": (
                        f"Sweep pool capped at {parallel_n} — the remaining "
                        f"{len(configs) - parallel_n} point(s) run sequentially.")})
                except Exception:  # noqa: BLE001
                    pass
            if ctx.check_abort is not None and ctx.check_abort():
                if i <= 1:
                    return "[error] aborted before the sweep started"
                rows.append({"index": i, "label": _label(i, cfg), "config": cfg,
                             "metrics": {}, "error": "aborted by user"})
                break
            rows.append(await _sweep_point(
                ctx, ctx.kernels.python, code, cfg, _label(i, cfg),
                parent_id, eid, i, env))
    finally:
        if kernels:
            ctx.unregister_kernels(kernels)
            stop = getattr(ctx.kernels, "stop_pool", None)
            if stop:
                await asyncio.shield(stop(kernels))
        # Undo config pollution on the main kernel from sequential points.
        if parallel_n < len(configs):
            try:
                if prev_config is None:
                    await ctx.kernels.python.run_code("config = None")
                else:
                    await ctx.kernels.python.run_code(
                        "config = " + json.dumps(prev_config, default=str))
            except Exception:  # noqa: BLE001
                pass

    if parallel_n and parallel_n < len(configs):
        mode = f"parallel {parallel_n} + sequential {len(configs) - parallel_n}"
    elif parallel_n:
        mode = "parallel"
    else:
        mode = "sequential"
    lines = [f"## Parameter sweep — {len(configs)} points ({mode})", ""]
    cols = sorted({k for r in rows for k in r.get("metrics", {})})
    lines.append("| point | label | config | " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * (3 + len(cols)))
    for r in rows:
        mvals = [f"{r.get('metrics', {}).get(c):.4g}" if r.get("metrics", {}).get(c) is not None else "—" for c in cols]
        lines.append(f"| {r['index']} | {r['label']} | {json.dumps(r['config'], sort_keys=True)} | " + " | ".join(mvals) + " |")
    lines.append("")
    for r in rows:
        if r.get("error"):
            lines.append(f"- point {r['index']} ({r['label']}) failed: {r['error']}")
    lines.append(f"- Runs recorded: {len(rows)} under experiment #{eid or '(none)'}; "
                 f"derived from run #{parent_id or '(none)'}.")
    return "\n".join(lines)


def _record_tool_run(ctx: ToolContext, *, prompt: str, reply: str,
                     status: str = "done", kind: str, label: str | None = None,
                     experiment_id: int | None = None,
                     parent_run_id: int | None = None, model: str | None = None,
                     metrics: dict | None = None, config: dict | None = None,
                     code: dict | list | None = None, env: dict | None = None,
                     dataset: str | None = None, error: str | None = None,
                     tool_sequence: list | None = None,
                     artifact_ids: list | None = None,
                     plan_id: str | None = None,
                     plan_step_id: str | None = None) -> int | None:
    """Persist a tool-produced run through the two-phase lifecycle (begin/finish)
    so it is first-class: pre-created row gives run_id for audit linkage, and
    finish_run records the structured error + integrity hash. Returns the run id,
    or None when no store is attached (pure sandbox contexts)."""
    if ctx.store is None:
        return None
    started = time.time()
    try:
        rid = ctx.store.begin_run(
            prompt=prompt, started_at=started, kind=kind,
            experiment_id=experiment_id, parent_run_id=parent_run_id,
            model=model,
            message_id=(int(ctx.message_id)
                        if str(getattr(ctx, "message_id", "")).isdigit() else None),
            plan_id=plan_id, plan_step_id=plan_step_id)
        if not rid:
            return None
        ctx.store.finish_run(
            rid=rid, reply=reply, status=status, finished_at=time.time(),
            tool_sequence=tool_sequence, artifact_ids=artifact_ids,
            metrics=metrics, config=config, label=label, code=code, env=env,
            dataset=dataset, error=error or None)
        return rid
    except Exception:  # noqa: BLE001
        return None


async def _sweep_point(ctx: ToolContext, kernel, code: str, cfg: dict,
                       label: str, parent_id, eid, index: int,
                       env: dict | None = None) -> dict:
    """Run one sweep point on `kernel`: inject `config`, run the code, record a
    run with the returned metrics + full code + env. Store writes stay on the
    event loop."""
    started = time.time()
    rid = None
    try:
        if ctx.store is not None:
            rid = ctx.store.begin_run(
                prompt=code, started_at=started, kind="sweep",
                experiment_id=eid, parent_run_id=parent_id,
                model=getattr(ctx, "model", None),
                message_id=(int(ctx.message_id)
                            if str(getattr(ctx, "message_id", "")).isdigit()
                            else None))
        await kernel.run_code("config = " + json.dumps(cfg))
        resp = await kernel.run_code(code)
        metrics = resp.get("metrics") or {}
        error = resp.get("error") or ""
        if error:
            metrics = {}
        # First-class point run: finish the pre-created row (integrity hash +
        # structured error). If begin_run was unavailable, fall back to the
        # two-phase helper which does begin+finish in one call.
        if rid:
            ctx.store.finish_run(
                rid=rid, reply=resp.get("output", "")[:2000],
                status="done" if not error else "failed",
                tool_sequence=[{"name": "run_sweep", "ok": not error,
                                "args": {"config": cfg}, "result": error or "ok"}],
                metrics=metrics, config=cfg, label=label,
                code=[{"name": "run_sweep", "code": code}], env=env or {},
                error=error or None)
        else:
            rid = _record_tool_run(
                ctx, prompt=code, reply=resp.get("output", "")[:2000],
                status="done" if not error else "failed",
                tool_sequence=[{"name": "run_sweep", "ok": not error,
                                "args": {"config": cfg}, "result": error or "ok"}],
                metrics=metrics, experiment_id=eid, config=cfg, label=label,
                kind="sweep", parent_run_id=parent_id,
                model=getattr(ctx, "model", None),
                code=[{"name": "run_sweep", "code": code}], env=env or {},
                error=error or None)
        # Traceability: each sweep point is an audit event linked to its own
        # run_id (previously only the parent run_sweep tool call was audited).
        if rid and ctx.audit is not None:
            try:
                from ..audit import emit_tool_audit
                session = getattr(ctx.artifacts, "project_dir", None)
                await emit_tool_audit(
                    ctx.audit, agent_id="Fox",
                    session_id=(Path(session).name if session else None),
                    trace_id=ctx.message_id or None, run_id=str(rid),
                    tool_name="run_sweep", method="sweep_point",
                    args={"config": cfg}, result=error or "ok", ok=not error,
                    duration_ms=(time.time() - started) * 1000.0,
                    source="coordinator")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return {"index": index, "label": label, "config": cfg,
                "metrics": {}, "error": f"{type(e).__name__}: {e}"}
    return {"index": index, "label": label, "config": cfg,
            "metrics": metrics, "error": error or "", "run_id": rid}


async def _run_finetune(ctx: ToolContext, base_model: str, dataset: str,
                        epochs: int = 3, learning_rate: float = 2e-5,
                        batch_size: int = 8, lora_r: int = 0,
                        task: str = "classification") -> str:
    """Set up a finetune launch for the active experiment.

    Builds the finetune config from the inputs, records a `kind="finetune"` run
    under the active experiment with the generated training script as its code
    (so the pipeline shows the full setup), and returns a human summary. The
    script itself is not executed here — run it via run_python to actually train.
    """
    from ..finetune import (finetune_script, finetune_summary,
                            normalize_finetune_config, validate_finetune)
    cfg = normalize_finetune_config({
        "base_model": base_model, "dataset": dataset, "epochs": epochs,
        "learning_rate": learning_rate, "batch_size": batch_size,
        "lora_r": lora_r, "task": task,
    })
    err = validate_finetune(cfg)
    if err:
        return f"[error] {err}"
    eid = int(ctx.experiment_id) if str(ctx.experiment_id).isdigit() else None
    script = finetune_script(cfg)
    env = {}
    try:
        env = await ctx.kernels.get_env()
    except Exception:  # noqa: BLE001
        pass
    _record_tool_run(
        ctx, prompt=f"finetune {cfg['base_model']} on {cfg['dataset']}",
        reply=finetune_summary(cfg), status="done",
        tool_sequence=[{"name": "run_finetune", "ok": True,
                        "args": {"config": cfg}, "result": "setup recorded"}],
        metrics={}, experiment_id=eid, config=cfg, label="finetune",
        kind="finetune", parent_run_id=ctx.parent_run_id,
        model=getattr(ctx, "model", None),
        code=[{"name": "run_finetune", "code": script}], env=env or {},
        dataset=cfg["dataset"] or None)
    return finetune_summary(cfg) + \
        "\n\nTraining script:\n```python\n" + script + "\n```"


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
    shell_cwd = ctx.quarantine_dir or str(ctx.kernels.workspace_dir)
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", command,
        cwd=shell_cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _read_bounded(limit: int = 100_000) -> tuple[bytes, str]:
        """Read stdout incrementally so a command dumping gigabytes can't OOM the
        server; the process is killed once the cap is exceeded or it times out.
        Returns (bytes, status) with status in ok|timeout|truncated."""
        chunks: list[bytes] = []
        total = 0
        assert proc.stdout is not None
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return b"".join(chunks), "timeout"
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                proc.kill()
                return b"".join(chunks), "truncated"
        return b"".join(chunks), "ok"

    try:
        out, status = await _read_bounded()
        rc = await proc.wait()
    except Exception:  # noqa: BLE001
        proc.kill()
        return "[error] shell command failed"
    text = out.decode(errors="replace").rstrip()
    if status == "timeout":
        text += "\n[error] shell command timed out"
    elif status == "truncated":
        text += "\n[truncated: output exceeded 100k bytes]"
    elif rc != 0:
        text += f"\n[exit code {rc}]"
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
        _record_tool_run(
            ctx, prompt=f"run notebook {notebook}",
            reply=result, status="done",
            artifact_ids=[a["id"] for a in collected],
            metrics=metrics, kind="notebook", label=notebook)
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


# --------------------------------------------------------------- rkg tools ----
# The agent bridges into the shared Research Knowledge Graph. The Organizer and
# Research Workbench are the SAME lazily-built singletons the /api/rkg dashboard
# uses (see research_knowledge_graphs/router.py), so agent + dashboard share one
# corpus, graph and RAG index. Tools are best-effort: if RKG isn't initialized
# (e.g. no data root / Ollama down) they return a clear message instead of
# crashing the agent turn.

def _rkg_runtime():
    """Lazily resolve the shared RKG Organizer + Research Workbench."""
    from ..research_knowledge_graphs.router import get_org, get_workbench

    return get_org(), get_workbench()


async def _rkg_query_rag(ctx: ToolContext, question: str) -> str:
    question = (question or "").strip()
    if not question:
        return "[error] a question is required"
    try:
        org, _ = _rkg_runtime()
        result = await asyncio.to_thread(org.query_rag, question)
    except Exception as e:  # noqa: BLE001
        return f"[error] RKG unavailable: {type(e).__name__}: {e}"
    answer = (result.get("answer") or "").strip()
    sources = result.get("sources") or []
    lines = [answer or "(no answer)"]
    if sources:
        lines.append("\nSources:")
        lines.extend(f"  [{s.get('id')}] {s.get('title')}" for s in sources[:8])
    return "\n".join(lines)[:10_000]


async def _rkg_paper_notes(ctx: ToolContext, paper_id: str) -> str:
    paper_id = (paper_id or "").strip()
    if not paper_id:
        return "[error] a paper id is required"
    try:
        _, wb = _rkg_runtime()
        info = await asyncio.to_thread(wb.paper_notes, paper_id)
    except Exception as e:  # noqa: BLE001
        return f"[error] RKG unavailable: {type(e).__name__}: {e}"
    if not info.get("found"):
        return f"Paper {paper_id} is not in the knowledge graph."
    lines = [
        f"### {info.get('title')}  [arXiv:{info.get('id')}]",
        f"Published: {info.get('published') or 'n/a'}",
        f"Abstract: {info.get('abstract') or ''}",
    ]
    if info.get("tags"):
        lines.append("Tags: " + ", ".join(info["tags"]))
    if info.get("concepts"):
        lines.append("Concepts: " + ", ".join(info["concepts"]))
    if info.get("notes"):
        lines.append(f"Notes: {info['notes']}")
    return "\n".join(lines)[:10_000]


async def _rkg_scenario_status(ctx: ToolContext, scenario_id: str) -> str:
    scenario_id = (scenario_id or "").strip()
    if not scenario_id:
        return "[error] a scenario id is required"
    try:
        _, wb = _rkg_runtime()
        st = await asyncio.to_thread(wb.status, scenario_id)
    except Exception as e:  # noqa: BLE001
        return f"[error] RKG unavailable: {type(e).__name__}: {e}"
    status = st.get("status") or {}
    return (
        f"Scenario '{st.get('id')}': phase={status.get('phase_label')}, "
        f"progress={status.get('progress')}, corpus_size={st.get('corpus_size')}, "
        f"best_score={st.get('best_score')}, report_exists={st.get('report_exists')}"
    )


async def _rkg_scenario_report(ctx: ToolContext, scenario_id: str) -> str:
    scenario_id = (scenario_id or "").strip()
    if not scenario_id:
        return "[error] a scenario id is required"
    try:
        _, wb = _rkg_runtime()
        report = await asyncio.to_thread(wb.report, scenario_id)
    except Exception as e:  # noqa: BLE001
        return f"[error] RKG unavailable: {type(e).__name__}: {e}"
    if not report:
        return f"No report yet for scenario '{scenario_id}'."
    return report[:20_000]


def build_tools(ctx: ToolContext) -> dict[str, ToolFn]:
    return {
        "run_python": lambda code: _run_python(ctx, code),
        "run_sweep": lambda code, configs, label_prefix="":
            _run_sweep(ctx, code, configs, label_prefix),
        "run_finetune": lambda base_model, dataset, epochs=3,
            learning_rate=2e-5, batch_size=8, lora_r=0, task="classification":
            _run_finetune(ctx, base_model, dataset, epochs, learning_rate,
                          batch_size, lora_r, task),
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
        "rkg__query_rag": lambda question: _rkg_query_rag(ctx, question),
        "rkg__paper_notes": lambda paper_id: _rkg_paper_notes(ctx, paper_id),
        "rkg__scenario_status": lambda scenario_id: _rkg_scenario_status(ctx, scenario_id),
        "rkg__scenario_report": lambda scenario_id: _rkg_scenario_report(ctx, scenario_id),
    }
