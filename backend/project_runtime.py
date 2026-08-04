"""ProjectRuntime: per-project session state (SQLite store, kernels, notebooks,
workflow tracker) plus the message-context helpers the chat handler uses.

Moved out of main.py so the API routers and the WebSocket handler share it
without a circular import, and so project behaviour can be unit-tested in
isolation.
"""

from __future__ import annotations

import asyncio
import json
import time

from .agents.tools import ToolContext
from .artifacts.store import ArtifactStore
from .kernels.manager import KernelManager
from .notebooks import NotebookService
from .permissions import PermissionManager
from .paths import PROJECTS_DIR
from .state import CONFIG, make_llm
from .store import ProjectStore
from .workflows import WorkflowTracker


class ProjectRuntime:
    def __init__(self, name: str):
        self.name = name
        self.dir = PROJECTS_DIR / name
        self.store = ProjectStore(self.dir)
        self.artifacts = ArtifactStore(self.dir)
        self.kernels = KernelManager(self.dir)
        self.notebooks = NotebookService(self.dir, self.kernels.python)
        self.permissions = PermissionManager(self.store)
        self.lock = asyncio.Lock()
        self.llm = make_llm()
        self.reviewer_enabled = CONFIG["agent"].get("reviewer_enabled", True)
        self.max_iters = CONFIG["agent"].get("max_iters", 8)
        self.workflow = WorkflowTracker(
            persist=lambda snap: self.store.set_setting(
                "workflow_latest", json.dumps(snap)),
            record=self.store.add_workflow_run,
        )
        try:
            latest = self.store.get_setting("workflow_latest", "")
            self.workflow.restore(json.loads(latest) if latest else None)
        except Exception:  # noqa: BLE001
            pass

    def ctx(self, emit, approval) -> ToolContext:
        return ToolContext(kernels=self.kernels, artifacts=self.artifacts,
                           store=self.store, permissions=self.permissions,
                           approval=approval, emit=emit, notebooks=self.notebooks,
                           workflow=self.workflow)

    def build_llm_messages(self) -> list[dict]:
        from .agents.coordinator import SYSTEM_PROMPT
        from .skills import skills_context

        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        summary = self.store.get_setting("context_summary", "")
        rows = self.store.list_messages()
        msgs: list[dict] = []
        for r in rows:
            if r["id"] <= cutoff:
                continue
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
                    d["tool_calls"] = wire_tool_calls(tcs)
                msgs.append(d)
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": meta.get("tool_call_id", ""),
                             "content": r["content"]})
        sk = skills_context()
        system = SYSTEM_PROMPT + ("\n\n" + sk if sk else "")
        if summary:
            system += ("\n\n## Summary of earlier conversation (compacted)\n"
                       "The following is a persistent summary of turns that were "
                       "compacted out of the live context:\n" + summary)
        msgs.insert(0, {"role": "system", "content": system})
        return sanitize_messages(msgs)

    # Number of fresh messages kept before older turns get compacted away.
    COMPACTION_LIMIT = 60
    # Always keep this many of the most recent messages fresh in the context.
    COMPACTION_KEEP = 24

    async def maybe_compact(self):
        """Summarize older turns into a persistent summary once the conversation
        grows past COMPACTION_LIMIT fresh messages.

        The summary + the message-id cutoff are stored in settings, so the
        compaction survives restarts and is only performed once per block.
        """
        rows = self.store.list_messages()
        cutoff = int(self.store.get_setting("context_cutoff", "0") or 0)
        fresh = [r for r in rows if r["id"] > cutoff]
        if len(fresh) <= self.COMPACTION_LIMIT:
            return
        block = fresh[:-self.COMPACTION_KEEP]
        if not block:
            return
        prev = self.store.get_setting("context_summary", "")
        summary = await _summarize_conversation(self.llm, prev, block)
        new_cutoff = block[-1]["id"]
        self.store.set_setting("context_summary", summary)
        self.store.set_setting("context_cutoff", str(new_cutoff))

    async def stop(self):
        await self.kernels.stop()


def wire_tool_calls(tcs: list) -> list:
    """Normalize stored tool_calls to the OpenAI wire format (arguments as JSON string)."""
    out = []
    for tc in tcs or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append({
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": json.dumps(args)},
        })
    return out


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


def _conversation_digest(rows: list[dict], limit: int = 120) -> str:
    """Deterministic fallback summary: one compacted line per message."""
    out: list[str] = []
    for r in rows:
        role = r["role"]
        content = " ".join((r["content"] or "").split())
        if role == "user":
            out.append(f"user: {content[:limit]}")
        elif role == "assistant":
            out.append(f"assistant: {content[:limit]}")
        elif role == "tool":
            meta = r.get("meta") or {}
            out.append(f"tool({meta.get('name', 'tool')}): {content[:100]}")
    return "\n".join(out[:300])


async def _summarize_conversation(llm, prev: str, rows: list[dict]) -> str:
    """Produce (or extend) a persistent summary of compacted conversation turns.

    Best-effort: an LLM summary when available, otherwise a deterministic
    digest of the message contents.
    """
    transcript = _conversation_digest(rows)
    if prev:
        transcript = f"Existing summary:\n{prev}\n\nNew turns to fold in:\n{transcript}"
    prompt = (
        "You maintain a persistent summary of an agentic research conversation. "
        "Read the turns below and produce a compact summary capturing: the user's "
        "research goal and constraints, what experiments/analyses were run, key "
        "results and metric values, and any open questions or next steps. Plain "
        "sentences or short bullets, no markdown headings, keep it under 400 words.\n\n"
        + transcript[:8000])
    try:
        resp = await llm.complete([{"role": "user", "content": prompt}],
                                  temperature=0.2, tools=None)
        text = (resp.get("content") or "").strip()
        if text:
            return text[:4000]
    except Exception:  # noqa: BLE001
        pass
    return _conversation_digest(rows, limit=160)
