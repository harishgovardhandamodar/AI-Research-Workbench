"""Audit trail routes: local agent audit-log search, timeline, deviations,
permission tracking, chain verification and export.

Backed by the per-project :class:`audit.store.LocalAuditStore` (see
``backend/audit.py``); the in-app "Audit Trail" view reads from here.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..audit import public_event
from ..state import get_runtime

router = APIRouter()


def _store(name: str):
    return get_runtime(name).audit_store


def _ts(value: str | None):
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


# ------------------------------------------------------------------- summary ---
@router.get("/api/projects/{name}/audit/summary")
async def audit_summary(name: str, since: str | None = None):
    """KPI cards for the Audit Trail Overview."""
    store = _store(name)
    return {"summary": store.summary(_ts(since)),
            "tool_usage": store.tool_usage(),
            "agents": store.agents()}


# -------------------------------------------------------------------- events ---
@router.get("/api/projects/{name}/audit/events")
async def audit_events(name: str, agent: str | None = None, source: str | None = None,
                       tool: str | None = None, severity: str | None = None,
                       session: str | None = None, q: str | None = None,
                       since: str | None = None, until: str | None = None,
                       run_id: str | None = None,
                       limit: int = 200, offset: int = 0):
    """Searchable event list (newest first)."""
    store = _store(name)
    events = store.query(agent_id=agent, source=source, tool_name=tool,
                         severity=severity, session_id=session,
                         run_id=run_id,
                         since=_ts(since), until=_ts(until),
                         limit=min(int(limit), 2000), offset=int(offset))
    if q:
        ql = q.lower()
        events = [e for e in events if ql in json.dumps(e, default=str).lower()]
    return {"events": [public_event(e) for e in events],
            "total": store.count(agent_id=agent, severity=severity,
                                 since=_ts(since), until=_ts(until))}


@router.get("/api/projects/{name}/audit/event/{event_id}")
async def audit_event(name: str, event_id: str):
    ev = _store(name).get(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {"event": public_event(ev.model_dump(mode="json"))}


# ------------------------------------------------------------------ timeline ---
@router.get("/api/projects/{name}/audit/timeline")
async def audit_timeline(name: str, agent: str | None = None, source: str | None = None,
                         severity: str | None = None, tool: str | None = None,
                         since: str | None = None, until: str | None = None,
                         limit: int = 800):
    """Events for a vertical timeline view, latest first."""
    store = _store(name)
    events = store.query(agent_id=agent, source=source, severity=severity,
                         tool_name=tool, since=_ts(since), until=_ts(until),
                         limit=min(int(limit), 4000))
    out = []
    for e in events:  # store.query is already newest-first
        out.append({
            "event_id": e.get("event_id"),
            "timestamp": e.get("timestamp"),
            "agent_id": e.get("agent_id"),
            "source": e.get("source"),
            "tool_name": e.get("tool_name") or e.get("method"),
            "method": e.get("method"),
            "severity": e.get("severity"),
            "duration_ms": e.get("duration_ms"),
            "run_id": e.get("run_id"),
            "trace_id": e.get("trace_id"),
            "policy": (e.get("policy_decision") or {}).get("outcome"),
            "network": bool(e.get("network")),
            "filesystem": bool(e.get("filesystem")),
            "data_access": bool((e.get("result_summary") or {}).get("data_classes")),
            "tags": e.get("tags") or [],
        })
    return {"events": out}


# -------------------------------------------------------------------- agents ---
@router.get("/api/projects/{name}/audit/agents")
async def audit_agents(name: str):
    store = _store(name)
    return {"agents": store.agents()}


@router.get("/api/projects/{name}/audit/agents/{agent_id}/history")
async def audit_agent_history(name: str, agent_id: str, limit: int = 500):
    store = _store(name)
    return {
        "agent_id": agent_id,
        "events": [public_event(e) for e in store.get_agent_history(agent_id, limit)],
        "tool_usage": store.tool_usage(agent_id),
        "data_classes": store.data_classes(agent_id),
        "network_destinations": store.network_destinations(agent_id),
    }


@router.get("/api/projects/{name}/audit/agents/{agent_id}/permissions")
async def audit_agent_permissions(name: str, agent_id: str):
    """Permission vs observed usage drift for one agent."""
    store = _store(name)
    events = store.query(agent_id=agent_id, limit=1000)
    granted: dict[str, dict] = {}
    used: dict[str, int] = {}
    overrides: dict[str, int] = {}
    for e in events:
        pd = e.get("policy_decision") or {}
        pattern = f"{pd.get('rule')}:{pd.get('pattern')}" if pd.get("rule") else ""
        if pattern and pd.get("outcome") in ("ALLOW", "OVERRIDE"):
            g = granted.setdefault(pattern, {"kind": pd.get("rule"),
                                             "pattern": pd.get("pattern"),
                                             "risk_tier": pd.get("risk_tier"),
                                             "granted": True,
                                             "overrides": 0})
            if pd.get("outcome") == "OVERRIDE":
                g["overrides"] = g.get("overrides", 0) + 1
        tool = e.get("tool_name")
        if tool:
            used[tool] = used.get(tool, 0) + 1
        if (e.get("policy_decision") or {}).get("outcome") == "OVERRIDE" and tool:
            overrides[tool] = overrides.get(tool, 0) + 1
    return {"agent_id": agent_id, "grants": list(granted.values()),
            "observed_tools": [{"tool": t, "count": c} for t, c in
                               sorted(used.items(), key=lambda kv: kv[1], reverse=True)],
            "overrides": overrides}


# ---------------------------------------------------------------- deviations ---
@router.get("/api/projects/{name}/audit/deviations")
async def audit_deviations(name: str, agent: str | None = None,
                           reviewed: str | None = None, limit: int = 200):
    store = _store(name)
    rv = None if reviewed is None else (reviewed.lower() == "true")
    return {"deviations": store.list_deviations(agent, rv, int(limit))}


@router.post("/api/projects/{name}/audit/deviations/{deviation_id}/review")
async def audit_deviation_review(name: str, deviation_id: str, body: dict):
    store = _store(name)
    ok = store.mark_deviation_reviewed(
        deviation_id, bool(body.get("reviewed", True)),
        body.get("reviewed_by", ""), bool(body.get("false_positive", False)))
    if not ok:
        raise HTTPException(status_code=404, detail="deviation not found")
    return {"ok": True}


@router.post("/api/projects/{name}/audit/scan")
async def audit_scan(name: str, body: dict | None = None):
    """Trigger a deviation scan now; returns newly recorded deviations."""
    rt = get_runtime(name)
    agent = (body or {}).get("agent")
    count = rt.audit_scanner.scan(agent_id=agent, force=True)
    return {"recorded": count,
            "open": rt.audit_store.count_open_deviations()}


# --------------------------------------------------------------- integrity ---
@router.get("/api/projects/{name}/audit/verify")
async def audit_verify(name: str):
    return {"chain": _store(name).verify_chain()}


# -------------------------------------------------------------------- export ---
@router.get("/api/projects/{name}/audit/export")
async def audit_export(name: str, fmt: str = "json", agent: str | None = None,
                       severity: str | None = None, limit: int = 2000):
    store = _store(name)
    events = store.export_events(limit=int(limit), agent_id=agent, severity=severity)
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        fields = ["event_id", "timestamp", "agent_id", "source", "method",
                  "tool_name", "severity", "duration_ms", "policy_decision",
                  "run_id", "trace_id", "event_hash"]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for e in events:
            writer.writerow({f: e.get(f) for f in fields})
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 'attachment; filename="audit-events.csv"'})
    return Response(json.dumps([public_event(e) for e in events], default=str,
                               indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             'attachment; filename="audit-events.json"'})
