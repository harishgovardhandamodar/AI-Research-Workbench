"""Workflow progress tracking for long-running pipelines (arXiv replication, …).

Each project holds one :class:`WorkflowTracker`. As the agent's tool calls
advance a pipeline, stages move through states
(pending → running → waiting_approval → done/failed) and every change is pushed
live to the connected chat windows via the subscribed ``emit`` callables
(WebSocket ``workflow`` events). The tracker also keeps the latest snapshot so
any page/section load can fetch it with ``GET /api/projects/<name>/workflow`` —
the UI is event-driven, and also self-heals on load.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

Emit = Callable[[str, dict], Awaitable[None]]

# ----------------------------------------------------------------- stages ----

# The arXiv replication pipeline, in the order the agent is expected to run it.
# `run` is advanced by the built-in code-execution tools (run_python/notebook/
# shell) so the "re-implement and run the experiment" stage also shows progress.
ARXIV_STAGES: list[dict] = [
    {"id": "ingest",     "label": "Ingest paper"},
    {"id": "extract",    "label": "Extract text"},
    {"id": "notes",      "label": "Structured notes"},
    {"id": "summarize",  "label": "Summarize"},
    {"id": "experiment", "label": "Experiment spec"},
    {"id": "run",        "label": "Run experiment"},
    {"id": "compare",    "label": "Compare results"},
    {"id": "report",     "label": "Replication report"},
    {"id": "graph",      "label": "Knowledge graph"},
]

# Maps a namespaced tool name -> stage id. ArXiv tools drive the pipeline; the
# code-execution tools feed the "run" stage while an arXiv workflow is active.
STAGE_BY_TOOL: dict[str, str] = {

    "arxiv__ingest_arxiv_paper": "ingest",
    "arxiv__extract_paper_text": "extract",
    "arxiv__extract_structured_notes": "notes",
    "arxiv__summarize_paper": "summarize",
    "arxiv__craft_experiment_from_notes": "experiment",
    "arxiv__compare_results": "compare",
    "arxiv__prepare_replication_report": "report",
    "arxiv__build_knowledge_graph_from_notes": "graph",
    "arxiv__query_knowledge_graph": "graph",
    "arxiv__merge_knowledge_graphs": "graph",
    "arxiv__export_knowledge_graph": "graph",
}
RUN_TOOLS = {"run_python", "run_notebook", "run_shell"}


class WorkflowTracker:
    """Tracks one pipeline run per project, broadcasting every change."""

    def __init__(self, persist: Callable[[dict], None] | None = None,
                 record: Callable[[dict], None] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._subs: list[Emit] = []
        self._title = ""
        self._status = "idle"          # idle | running | done | failed
        self._message = ""
        self._stages: list[dict] = []  # {id,label,state,detail,pct}
        self._pct = 0.0
        self._updated = time.time()
        self._started = 0.0
        # `persist` saves every snapshot (e.g. latest state to the project DB so
        # page/section loads see it after a restart); `record` archives finished
        # runs to history (traceability).
        self._persist = persist
        self._record = record

    # ------------------------------------------------------------- restore --
    def restore(self, snap: dict | None) -> None:
        """Load a previously persisted snapshot (startup self-heal)."""
        snap = snap or {}
        self._title = snap.get("title", "")
        self._status = snap.get("status", "idle")
        self._message = snap.get("message", "")
        self._stages = snap.get("stages", [])
        self._pct = snap.get("pct", 0.0) or 0.0
        self._updated = snap.get("updated_at", time.time())
        self._started = snap.get("started_at", 0.0)

    # ------------------------------------------------------------- subscribe --
    def subscribe(self, emit: Emit):
        if emit not in self._subs:
            self._subs.append(emit)

    def unsubscribe(self, emit: Emit):
        if emit in self._subs:
            self._subs.remove(emit)

    # ------------------------------------------------------------- snapshot --
    def snapshot(self) -> dict:
        return {
            "title": self._title,
            "status": self._status,
            "message": self._message,
            "pct": round(self._pct),
            "stages": [dict(s) for s in self._stages],
            "started_at": self._started,
            "updated_at": self._updated,
        }

    def stage_for_tool(self, name: str) -> str | None:
        return STAGE_BY_TOOL.get(name)

    def _recompute(self) -> None:
        total = len(self._stages) or 1
        done = sum(1 for s in self._stages if s["state"] == "done")
        self._pct = done / total * 100

    async def _broadcast(self) -> None:
        snap = self.snapshot()
        if self._persist is not None:
            try:
                self._persist(snap)
            except Exception:  # noqa: BLE001
                pass
        for emit in list(self._subs):
            try:
                await emit("workflow", snap)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- actions --
    async def start(self, title: str = "arXiv replication",
                    stages: list[dict] | None = None) -> None:
        """Reset to a fresh running pipeline (idempotent for the current run)."""
        async with self._lock:
            running = self._status == "running"
            if running and self._title == title:
                return  # already the live run
            self._title = title
            self._status = "running"
            self._message = "Starting…"
            self._started = time.time()
            self._stages = [
                {"id": s["id"], "label": s["label"],
                 "state": "pending", "detail": "", "pct": 0}
                for s in (stages or ARXIV_STAGES)
            ]
            self._recompute()
            self._updated = time.time()
        await self._broadcast()

    async def update_stage(self, stage_id: str, state: str,
                           detail: str | None = None, pct: float | None = None,
                           message: str | None = None) -> None:
        async with self._lock:
            for s in self._stages:
                if s["id"] == stage_id:
                    s["state"] = state
                    if detail is not None:
                        s["detail"] = detail
                    if pct is not None:
                        s["pct"] = round(pct)
            self._recompute()
            if message is not None:
                self._message = message
            self._updated = time.time()
        await self._broadcast()

    async def set_status(self, message: str) -> None:
        async with self._lock:
            self._message = message
            self._updated = time.time()
        await self._broadcast()

    async def on_tool_start(self, name: str) -> None:
        """Advance stages as a tool begins executing."""
        stage = STAGE_BY_TOOL.get(name)
        if stage:
            if name.startswith("arxiv__"):
                await self.start()  # begin/reset the arXiv pipeline
            await self.update_stage(stage, "running", message=f"{name} …")
        elif name in RUN_TOOLS and self._status == "running":
            await self.update_stage("run", "running", message=f"{name} …")

    async def on_tool_end(self, name: str, ok: bool) -> None:
        stage = STAGE_BY_TOOL.get(name)
        if stage:
            await self.update_stage(stage, "done" if ok else "failed")
        elif name in RUN_TOOLS and self._status == "running":
            await self.update_stage("run", "done" if ok else "failed")

    async def finish(self) -> None:
        """Called when the agent turn ends: freeze the pipeline as done/failed."""
        async with self._lock:
            if self._status != "running":
                return
            failed = any(s["state"] == "failed" for s in self._stages)
            self._status = "failed" if failed else "done"
            self._message = ("Pipeline finished with a failed stage."
                             if failed else "Pipeline complete.")
            if not failed:
                # A completed pipeline must not leave stages queued/running: the
                # agent may finish a stage without the exact matching tool call
                # (e.g. summarizing inline), so resolve leftovers as done so the
                # stage list matches the "Pipeline complete" status.
                for s in self._stages:
                    if s["state"] in ("pending", "running"):
                        s["state"] = "done"
            self._recompute()
            self._updated = time.time()
        await self._broadcast()
        self._archive()

    def _archive(self) -> None:
        """Record the finished run into the project's history (traceability)."""
        if self._record is None:
            return
        try:
            self._record(self.snapshot())
        except Exception:  # noqa: BLE001
            pass

    async def clear(self) -> None:
        async with self._lock:
            self._title = ""
            self._status = "idle"
            self._message = ""
            self._stages = []
            self._pct = 0.0
            self._updated = time.time()
        await self._broadcast()
