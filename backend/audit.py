"""Audit-trail integration for the Fox workbench.

Wires the standalone ``audit`` package into a project runtime:

  * one :class:`audit.store.LocalAuditStore` + :class:`audit.emitter.AuditEmitter`
    per project (stored under ``<project>/audit/``),
  * event builders that classify every agent tool call (data access, network,
    filesystem, policy decisions, risk tiers),
  * a periodic deviation scan after agent turns.

The coordinator, approval broker and MCP caller call ``emit_*`` helpers here;
the REST router (``backend/routers/audit.py``) and the in-app Audit Trail view
read from the same store.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from audit import AuditEmitter, DeviationDetector, LocalAuditStore
from audit.models import AuditEvent
from audit.policy import risk_tier_for, severity_for_tier
from audit.redaction import redact
from audit.store import LocalAuditStore as _Store

# ------------------------------------------------------------------ wiring ---

def make_audit(project_dir: Path) -> tuple[LocalAuditStore, AuditEmitter]:
    """Create (store, emitter) for a project directory."""
    store = LocalAuditStore(project_dir / "audit")
    emitter = AuditEmitter(store)
    return store, emitter


# ----------------------------------------------------------------- classify ---

# Tools that touch the network by definition.
_NETWORK_TOOLS = {"run_shell", "github__commit", "github__push", "github__pull",
                  "arxiv__ingest_arxiv_paper", "kaggle__import", "editor__open"}
_NETWORK_RE = re.compile(
    r"\b(https?://|www\.|git\s+(clone|fetch|pull)|pip\s+install|npm\s+install|"
    r"curl|wget|nc\b|ssh|scp|rsync|apt-get|apt\b|brew\b)\b", re.IGNORECASE)

# Tools that access the filesystem by definition.
_FS_TOOLS = {"editor__edit_file", "editor__read_file", "editor__list_files",
             "save_artifact", "create_notebook", "run_notebook",
             "editor__open"}
_FS_OPS = {"editor__edit_file": "write", "editor__read_file": "read",
           "editor__list_files": "list", "save_artifact": "write",
           "create_notebook": "write", "run_notebook": "read"}

# Keywords hinting that a call deals with a sensitive/structured data class.
_DATA_HINTS = {
    "dataframe": "dataframe", "csv": "csv", "pandas": "dataframe",
    "table": "table", "sqlite": "database", "database": "database",
    "db": "database", "credit_card": "pii", "pii": "pii", "password": "pii",
    "email": "pii", "clinical": "clinical", "patient": "clinical",
    "biomarker": "clinical", "sequence": "sequence", "protein": "sequence",
    "paper": "literature", "arxiv": "literature", "graph": "graph",
    "knowledge_graph": "graph", "notebook": "notebook", "kernel": "kernel",
}


def _network_from(tool: str, args: dict | None) -> dict | None:
    args = args or {}
    if tool in _NETWORK_TOOLS or _NETWORK_RE.search(" ".join(map(str, args.values()))):
        dest = ""
        for v in args.values():
            m = re.search(r"https?://([^/\s'\"]+)", str(v))
            if m:
                dest = m.group(1).split("?")[0]
                break
        if not dest:
            m = re.search(r"https?://([^/\s'\"]+)", tool)
            dest = m.group(1).split("?")[0] if m else (tool.split("__")[0] if "__" in tool else tool)
        return {"destination": dest or "local",
                "method": "shell" if tool == "run_shell" else "mcp",
                "status_code": None, "bytes": None}
    return None


def _filesystem_from(tool: str, args: dict | None) -> dict | None:
    args = args or {}
    op = _FS_OPS.get(tool)
    if op is None and tool == "run_shell":
        path = _shell_path(args.get("command", ""))
        if path:
            return {"path": path, "operation": "read/write", "permissions_at_time": None}
        return None
    path = args.get("path") or args.get("name") or args.get("notebook") or ""
    if op is not None:
        return {"path": str(path)[:500] or ".", "operation": op,
                "permissions_at_time": None}
    return None


_SHELL_FS_RE = re.compile(r"(?:^|\s)(?:rm|mv|cp|touch|mkdir|chmod|chown|cat|>)\s+(\S+)")
_SHELL_NET_RE = re.compile(r"\bhttps?://\S+")


def _shell_path(command: str) -> str | None:
    m = _SHELL_FS_RE.search(command or "")
    return m.group(1).strip("'\"") if m else None


def _data_classes_from(args: dict | None, result: str | None = None) -> list[str]:
    args = args or {}
    text = " ".join([f"{k}={v}" for k, v in args.items()]) + " " + (result or "")[:500]
    low = text.lower()
    found: set[str] = set()
    for hint, label in _DATA_HINTS.items():
        if hint in low:
            found.add(label)
    return sorted(found)


def _result_size(result: str | None) -> int | None:
    return len(result) if result else None


def _result_status(result: str, ok: bool) -> str:
    if not ok:
        return "error"
    if result and result.startswith("[denied"):
        return "denied"
    return "ok"


# ------------------------------------------------------------------- events ---

async def emit_tool_audit(emitter: AuditEmitter, *, agent_id: str,
                          session_id: str | None, trace_id: str | None,
                          run_id: str | None = None,
                          tool_name: str, method: str | None,
                          args: dict | None, result: str | None,
                          ok: bool, duration_ms: float,
                          source: str = "coordinator",
                          mcp_server: str | None = None) -> None:
    """Record one agent/MCP tool call as an audit event."""
    if emitter is None:
        return
    args = args or {}
    tier = risk_tier_for(tool_name)
    network = _network_from(tool_name, args)
    filesystem = _filesystem_from(tool_name, args)
    data_classes = _data_classes_from(args, result)
    if not data_classes and filesystem:
        data_classes = [filesystem["operation"]]
    event = AuditEvent(
        agent_id=agent_id, source=source, session_id=session_id,
        trace_id=trace_id, run_id=run_id, mcp_server=mcp_server,
        method=method or tool_name, tool_name=tool_name,
        arguments_redacted=redact(args),
        result_summary=AuditEvent.result_summary_for(
            status=_result_status(result or "", ok),
            data_classes=data_classes,
            size=_result_size(result),
            error=(result[:2000] if result and not ok else None)),
        network=network, filesystem=filesystem,
        duration_ms=round(duration_ms, 2),
        severity="critical" if not ok else severity_for_tier(tier),
        tags=["workbench", tool_name, tier],
    )
    try:
        await emitter.emit(event)
    except Exception:  # noqa: BLE001
        pass


async def emit_policy_event(emitter: AuditEmitter, *, agent_id: str,
                            session_id: str | None, trace_id: str | None,
                            run_id: str | None = None,
                            kind: str, command: str, decision: str,
                            temporary: bool, reason: str = "",
                            risk_tier: str | None = None) -> None:
    """Record a permission decision / override as an audit event."""
    if emitter is None:
        return
    outcome = decision.upper()
    if outcome == "ALLOW" and temporary:
        outcome = "OVERRIDE"  # one-shot grants are overrides of the policy
    event = AuditEvent(
        agent_id=agent_id, source="approval", session_id=session_id,
        trace_id=trace_id, run_id=run_id, tool_name=kind or "permission", method="permission",
        arguments_redacted=None,
        result_summary=None,
        policy_decision={
            "outcome": outcome, "rule": kind, "pattern": command[:500],
            "risk_tier": risk_tier or risk_tier_for(kind),
            "override_reason": reason or ("one-time grant" if temporary else ""),
            "temporary": bool(temporary),
        },
        severity="warning" if outcome == "OVERRIDE" else "info",
        tags=["permissions", outcome.lower()],
    )
    try:
        await emitter.emit(event)
    except Exception:  # noqa: BLE001
        pass


async def emit_session_event(emitter: AuditEmitter, *, agent_id: str,
                             session_id: str | None, trace_id: str | None,
                             run_id: str | None = None,
                             kind: str, tool_name: str | None = None,
                             payload: dict | None = None,
                             severity: str = "info") -> None:
    """Misc system/session audit event (e.g. agent turn started/finished)."""
    if emitter is None:
        return
    event = AuditEvent(
        agent_id=agent_id, source="system", session_id=session_id,
        trace_id=trace_id, run_id=run_id, method=kind, tool_name=tool_name,
        result_summary=({"status": "ok", "data_classes": [],
                         "size": None, "error": None} | (payload or {})),
        severity=severity, tags=["session", kind],
    )
    try:
        await emitter.emit(event)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------ deviations ---

class ProjectDeviationScanner:
    """Runs the deviation detector against a project's audit store and persists
    any new findings as deviation records."""

    def __init__(self, store: LocalAuditStore, min_events: int = 3):
        self.store = store
        self.detector = DeviationDetector()
        self.min_events = min_events
        self._last_scan: dict[str, float] = {}

    def scan(self, agent_id: str | None = None, force: bool = False) -> int:
        """Scan recent events; returns the number of new deviations recorded.

        The first scan seeds baselines from the full history; each later scan
        checks only events newer than the previous scan against that baseline,
        so genuinely new tools/paths/destinations are flagged.
        """
        now = time.time()
        key = agent_id or "*"
        last = self._last_scan.get(key, 0)
        if not force and self.store.count(agent_id=agent_id) < self.min_events:
            return 0
        since = last if last > 0 else None
        records = self.detector.run(self.store, agent_id=agent_id,
                                    limit=500, since=since)
        self._last_scan[key] = now
        if records:
            try:
                import logging
                logging.getLogger("fox.audit").warning(
                    "deviation scan found %d new deviation(s) (agent=%r)",
                    len(records), agent_id or "*")
            except Exception:  # noqa: BLE001
                pass
        return len(records)


# -------------------------------------------------------------------- api ---

def public_event(ev: dict) -> dict:
    """Trim a stored event for UI display (safe defaults for missing fields)."""
    return {
        "event_id": ev.get("event_id"),
        "timestamp": ev.get("timestamp"),
        "agent_id": ev.get("agent_id"),
        "session_id": ev.get("session_id"),
        "trace_id": ev.get("trace_id"),
        "run_id": ev.get("run_id"),
        "source": ev.get("source"),
        "mcp_server": ev.get("mcp_server"),
        "method": ev.get("method"),
        "tool_name": ev.get("tool_name"),
        "arguments_redacted": ev.get("arguments_redacted"),
        "result_summary": ev.get("result_summary"),
        "network": ev.get("network"),
        "filesystem": ev.get("filesystem"),
        "policy_decision": ev.get("policy_decision"),
        "duration_ms": ev.get("duration_ms"),
        "severity": ev.get("severity"),
        "tags": ev.get("tags"),
        "event_hash": ev.get("event_hash"),
        "prev_hash": ev.get("prev_hash"),
    }
