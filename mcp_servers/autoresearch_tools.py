"""Autonomous research (autoresearch) MCP server.

Exposes the workbench's autonomous research loop as MCP tools so any host — the
Fox agent itself or an external MCP client — can bootstrap a research folder and
run the loop: an experimentation agent proposes edits to ``research/experiment.py``,
the harness runs it under a fixed wall-clock budget, keeps a change only when the
goal metric improves (otherwise reverts), and logs every attempt.

Tools are writable (they run experiments), so the host asks for approval the first
time — matching the workbench permission model. Runs use the host's local LLM and
project runtime, resolved from the shared FOX_WORKBENCH_DIR.

Run standalone (stdio):

    .venv/bin/python mcp_servers/autoresearch_tools.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.server import MCPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FOX_WORKBENCH_DIR",
                      os.environ.get("FOX_WORKBENCH_DIR", str(ROOT / "workbench")))

mcp = MCPServer("fox-autoresearch", version="0.1.0")


def _runtime(project: str):
    from backend.state import get_runtime

    return get_runtime(project)


def _coordinator(rt):
    """An approval-free coordinator (the loop is autonomous)."""
    from backend.agents.coordinator import Coordinator
    from backend.agents.tools import ToolContext
    from backend.permissions import AllowAllPermissionManager

    ctx = ToolContext(kernels=rt.kernels, artifacts=rt.artifacts, store=rt.store,
                      permissions=AllowAllPermissionManager())
    return Coordinator(rt.llm, ctx, persist=lambda r, c, m: None,
                       record=lambda r: None, max_iters=20, mcp=None)


@mcp.tool()
def research_setup(project: str) -> str:
    """Create the <project>/research/ folder (program.md, experiment.py, log.md)
    for a project. Edit program.md to steer the agent."""
    from backend.autoresearch import ensure_research_dir

    rt = _runtime(project)
    files = ensure_research_dir(rt)
    return (f"research ready in {files['dir']} — edit program.md to steer the "
            "agent, then call research_run")


@mcp.tool()
def research_status(project: str) -> str:
    """Show the current research state: experiment.py head, best goal metric from
    autoresearch runs, and the tail of the research log."""
    rt = _runtime(project)
    rdir = rt.dir / "research"
    if not rdir.exists():
        return (f"no research folder for project {project!r} — call research_setup first")
    lines = [f"project: {project}"]
    exp = rdir / "experiment.py"
    if exp.exists():
        head = "\n".join(exp.read_text().splitlines()[:6])
        lines += ["experiment.py head:", head]
    else:
        lines.append("experiment.py: (missing)")
    ar = [r for r in rt.store.list_runs() if r.get("kind") == "autoresearch"]
    if ar:
        vals = [r.get("metrics", {}).get("accuracy") for r in ar if r.get("metrics")]
        vals = [v for v in vals if v is not None]
        if vals:
            lines.append(f"autoresearch runs: {len(ar)} · best accuracy: {max(vals):.4f}")
    log = rdir / "log.md"
    if log.exists() and log.read_text().strip():
        tail = log.read_text().strip().splitlines()[-5:]
        lines += ["log tail:"] + ["  " + l for l in tail]
    return "\n".join(lines)


@mcp.tool()
def research_log(project: str, n: int = 20) -> str:
    """Return the last n lines of the project's research log."""
    rt = _runtime(project)
    log = rt.dir / "research" / "log.md"
    if not log.exists():
        return "no research log yet"
    lines = log.read_text().strip().splitlines()
    return "\n".join(lines[-max(1, int(n)):])


@mcp.tool()
async def research_run(project: str, goal_metric: str = "accuracy",
                       higher_better: bool = True, goal_target: float | None = None,
                       max_iters: int = 8, per_iter_budget: int = 30) -> str:
    """Run the autonomous research loop for a project.

    The experimentation agent proposes edits to research/experiment.py; each
    proposal is run under per_iter_budget seconds and kept only when the goal
    metric improves (otherwise reverted). Returns the loop summary. Writable —
    approval required by the host."""
    from backend.autoresearch import run_autoresearch_loop

    rt = _runtime(project)
    coordinator = _coordinator(rt)
    cfg = {"goal_metric": goal_metric, "higher_better": bool(higher_better),
           "goal_target": goal_target, "max_iters": int(max_iters),
           "per_iter_budget": int(per_iter_budget)}
    result = await run_autoresearch_loop(
        rt, coordinator, rt.build_llm_messages, cfg,
        emit=lambda *_a, **_k: None, workflow=None)
    return result["summary"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
