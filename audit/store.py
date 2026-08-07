"""LocalAuditStore: append-only local persistence for AuditEvents.

Two writers are kept in sync:
  * SQLite (primary, queryable) — `audit_events` + `audit_deviations` tables.
  * Optional append-only JSONL with a SHA-256 hash chain (immutability). When
    enabled every event is also appended to ``events.jsonl``; each line carries
    ``prev_hash`` / ``event_hash`` so the chain can be verified with
    :meth:`LocalAuditStore.verify_chain`.

Events are stored as soon as they arrive (synchronous SQLite append). The
workbench emitter wraps this in an async producer/consumer queue so the agent
loop never blocks on the disk write.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import AuditEvent, DeviationRecord, canonical_json

_DB_LOCK = threading.Lock()


class LocalAuditStore:
    def __init__(self, dir_path: Path | str, jsonl_chain: bool = True,
                 db_name: str = "audit.db"):
        self.dir_path = Path(dir_path)
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.jsonl_chain = bool(jsonl_chain)
        self._jsonl_path = self.dir_path / "events.jsonl"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.dir_path / db_name, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()
        self._last_hash = self._load_last_hash()

    # -------------------------------------------------------------- schema ---
    def _init_db(self):
        c = self._conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT,
                source TEXT,
                mcp_server TEXT,
                method TEXT,
                tool_name TEXT,
                severity TEXT,
                trace_id TEXT,
                prev_hash TEXT,
                event_hash TEXT,
                payload TEXT NOT NULL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS audit_deviations (
                deviation_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                rule TEXT,
                severity TEXT,
                explanation TEXT,
                event_ids TEXT,
                detail TEXT,
                created_at REAL,
                reviewed INTEGER DEFAULT 0,
                reviewed_at REAL,
                reviewed_by TEXT,
                false_positive INTEGER DEFAULT 0)"""
        )
        for col in ("timestamp", "agent_id", "tool_name", "severity", "session_id"):
            try:
                c.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_audit_{col} ON audit_events ({col})")
            except sqlite3.OperationalError:
                pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts_agent ON audit_events (ts, agent_id)")
        c.commit()

    def _load_last_hash(self) -> str | None:
        if not self.jsonl_chain or not self._jsonl_path.exists():
            return None
        last = None
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line).get("event_hash") or last
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None
        return last

    # --------------------------------------------------------------- writes ---
    def append(self, event: AuditEvent | dict) -> AuditEvent:
        """Persist one event (SQLite + optional hash-chained JSONL). Returns it."""
        ev = event if isinstance(event, AuditEvent) else AuditEvent.from_dict(event)
        with self._lock:
            ev.compute_hash(self._last_hash)
            payload = ev.model_dump(mode="json")
            self._conn.execute(
                "INSERT OR IGNORE INTO audit_events "
                "(event_id, ts, agent_id, session_id, source, mcp_server, method,"
                " tool_name, severity, trace_id, prev_hash, event_hash, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.event_id, ev.timestamp.timestamp(), ev.agent_id,
                 ev.session_id, ev.source, ev.mcp_server, ev.method,
                 ev.tool_name, ev.severity, ev.trace_id,
                 ev.prev_hash, ev.event_hash, json.dumps(payload)))
            self._conn.commit()
            if self.jsonl_chain:
                self._jsonl_path.open("a", encoding="utf-8").write(
                    json.dumps(payload, ensure_ascii=False) + "\n")
            self._last_hash = ev.event_hash
        return ev

    def append_many(self, events: Iterable[AuditEvent | dict]) -> list[AuditEvent]:
        out = []
        for e in events:
            out.append(self.append(e))
        return out

    def record_deviation(self, dev: DeviationRecord | dict) -> DeviationRecord:
        d = dev if isinstance(dev, DeviationRecord) else DeviationRecord(**dev)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_deviations "
                "(deviation_id, agent_id, rule, severity, explanation, event_ids,"
                " detail, created_at, reviewed, reviewed_at, reviewed_by,"
                " false_positive)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.deviation_id, d.agent_id, d.rule, d.severity, d.explanation,
                 json.dumps(d.event_ids), json.dumps(d.detail),
                 d.created_at.timestamp(), 1 if d.reviewed else 0,
                 d.reviewed_at.timestamp() if d.reviewed_at else None,
                 d.reviewed_by, 1 if d.false_positive else 0))
            self._conn.commit()
        return d

    def mark_deviation_reviewed(self, deviation_id: str, reviewed: bool = True,
                                reviewed_by: str = "",
                                false_positive: bool = False) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audit_deviations SET reviewed=?, reviewed_at=?,"
                " reviewed_by=?, false_positive=? WHERE deviation_id=?",
                (1 if reviewed else 0,
                 datetime.now(timezone.utc).timestamp() if reviewed else None,
                 reviewed_by, 1 if false_positive else 0, deviation_id))
            self._conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- reads ---
    def get(self, event_id: str) -> AuditEvent | None:
        row = self._conn.execute(
            "SELECT payload FROM audit_events WHERE event_id=?", (event_id,)).fetchone()
        return AuditEvent.from_dict(json.loads(row["payload"])) if row else None

    def query(self, agent_id: str | None = None, source: str | None = None,
              tool_name: str | None = None, severity: str | None = None,
              session_id: str | None = None, trace_id: str | None = None,
              since: datetime | float | None = None,
              until: datetime | float | None = None,
              limit: int = 500, offset: int = 0,
              include_payload: bool = True) -> list[dict]:
        """Flexible event search; returns public dicts ordered newest-first."""
        where: list[str] = []
        params: list[Any] = []
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if source:
            where.append("source = ?")
            params.append(source)
        if tool_name:
            where.append("tool_name = ?")
            params.append(tool_name)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if trace_id:
            where.append("trace_id = ?")
            params.append(trace_id)
        if since is not None:
            where.append("ts >= ?")
            params.append(_ts(since))
        if until is not None:
            where.append("ts <= ?")
            params.append(_ts(until))
        sql = "SELECT * FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["payload"])
            if not include_payload:
                d = {k: d.get(k) for k in (
                    "event_id", "timestamp", "agent_id", "source", "tool_name",
                    "method", "severity", "duration_ms", "policy_decision",
                    "tags", "event_hash", "prev_hash")}
            out.append(d)
        return out

    def count(self, agent_id: str | None = None, severity: str | None = None,
              since: datetime | float | None = None,
              until: datetime | float | None = None) -> int:
        where: list[str] = []
        params: list[Any] = []
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if since is not None:
            where.append("ts >= ?")
            params.append(_ts(since))
        if until is not None:
            where.append("ts <= ?")
            params.append(_ts(until))
        sql = "SELECT COUNT(*) AS n FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def summary(self, since: datetime | float | None = None) -> dict:
        """KPI-style summary for the dashboard Overview page."""
        base = "ts >= ?" if since is not None else "1=1"
        params: list[Any] = [since.timestamp() if isinstance(since, datetime) else since] if since is not None else []
        def one(sql: str) -> int:
            row = self._conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0
        return {
            "total": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base}"),
            "critical": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND severity='critical'"),
            "warnings": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND severity='warning'"),
            "overrides": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND "
                             f"(json_extract(payload, '$.policy_decision.outcome')='OVERRIDE')"),
            "denials": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND "
                           f"(json_extract(payload, '$.policy_decision.outcome')='DENY')"),
            "data_access": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND "
                               f"(json_extract(payload, '$.result_summary.data_classes') IS NOT NULL)"),
            "network": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND "
                           f"json_extract(payload, '$.network') IS NOT NULL"),
            "filesystem": one(f"SELECT COUNT(*) AS n FROM audit_events WHERE {base} AND "
                              f"json_extract(payload, '$.filesystem') IS NOT NULL"),
            "open_deviations": self.count_open_deviations(),
            "active_agents": self.active_agents(since),
            "agents": self.active_agents(since),
        }

    def agents(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT agent_id, COUNT(*) AS n, MAX(ts) AS last_ts,"
            " SUM(severity='critical') AS criticals"
            " FROM audit_events GROUP BY agent_id ORDER BY last_ts DESC").fetchall()
        return [{"agent_id": r["agent_id"], "events": r["n"],
                 "last_ts": r["last_ts"], "criticals": r["criticals"] or 0}
                for r in rows]

    def active_agents(self, since: datetime | float | None = None) -> list[str]:
        if since is None:
            rows = self._conn.execute(
                "SELECT DISTINCT agent_id FROM audit_events").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT agent_id FROM audit_events WHERE ts >= ?",
                (_ts(since),)).fetchall()
        return [r["agent_id"] for r in rows]

    def get_agent_history(self, agent_id: str, limit: int = 500) -> list[dict]:
        return self.query(agent_id=agent_id, limit=limit)

    def tool_usage(self, agent_id: str | None = None) -> list[dict]:
        """Per-tool frequency (agent optional) for charts."""
        sql = ("SELECT tool_name, COUNT(*) AS n,"
               " SUM(severity='warning' OR severity='critical') AS flags"
               " FROM audit_events")
        params: list[Any] = []
        if agent_id:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)
        sql += " GROUP BY tool_name ORDER BY n DESC LIMIT 50"
        rows = self._conn.execute(sql, params).fetchall()
        return [{"tool": r["tool_name"], "count": r["n"],
                 "flags": r["flags"] or 0} for r in rows]

    def tool_sequences(self, agent_id: str | None = None) -> list[list[str]]:
        """Ordered tool-name sequences grouped by session/trace for the
        deviation detector's novelty checks."""
        rows = self._conn.execute(
            "SELECT session_id, trace_id, tool_name FROM audit_events"
            + (" WHERE agent_id = ?" if agent_id else "")
            + " ORDER BY ts ASC").fetchall()
        groups: dict[tuple, list[str]] = {}
        for r in rows:
            key = (r["session_id"], r["trace_id"])
            if r["tool_name"]:
                groups.setdefault(key, []).append(r["tool_name"])
        return list(groups.values())

    def data_classes(self, agent_id: str | None = None) -> list[str]:
        rows = self._conn.execute(
            "SELECT payload FROM audit_events"
            + (" WHERE agent_id = ?" if agent_id else "")).fetchall()
        seen: set[str] = set()
        for r in rows:
            d = json.loads(r["payload"])
            classes = (d.get("result_summary") or {}).get("data_classes") or []
            for c in classes:
                if isinstance(c, str):
                    seen.add(c)
        return sorted(seen)

    def network_destinations(self, agent_id: str | None = None) -> list[str]:
        rows = self._conn.execute(
            "SELECT payload FROM audit_events"
            + (" WHERE agent_id = ?" if agent_id else "")).fetchall()
        seen: set[str] = set()
        for r in rows:
            d = json.loads(r["payload"])
            nw = d.get("network") or {}
            if nw.get("destination"):
                seen.add(str(nw["destination"]))
        return sorted(seen)

    # ---------------------------------------------------------- deviations ---
    def list_deviations(self, agent_id: str | None = None,
                        reviewed: bool | None = None, limit: int = 200) -> list[dict]:
        where: list[str] = []
        params: list[Any] = []
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if reviewed is not None:
            where.append("reviewed = ?")
            params.append(1 if reviewed else 0)
        sql = "SELECT * FROM audit_deviations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_deviation(r) for r in rows]

    def count_open_deviations(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_deviations WHERE reviewed=0").fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_deviation(r) -> dict:
        return {
            "deviation_id": r["deviation_id"], "agent_id": r["agent_id"],
            "rule": r["rule"], "severity": r["severity"],
            "explanation": r["explanation"],
            "event_ids": json.loads(r["event_ids"] or "[]"),
            "detail": json.loads(r["detail"] or "{}"),
            "created_at": r["created_at"],
            "reviewed": bool(r["reviewed"]),
            "reviewed_at": r["reviewed_at"],
            "reviewed_by": r["reviewed_by"],
            "false_positive": bool(r["false_positive"]),
        }

    # ----------------------------------------------------- chain integrity ---
    def verify_chain(self) -> dict:
        """Verify the JSONL hash chain; returns {ok, events, broken_at, checked}."""
        if not self.jsonl_chain:
            return {"ok": True, "events": 0, "broken_at": None, "checked": 0,
                    "note": "jsonl chain disabled"}
        if not self._jsonl_path.exists():
            return {"ok": True, "events": 0, "broken_at": None, "checked": 0,
                    "note": "no chained events yet"}
        prev: str | None = None
        checked = 0
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        return {"ok": False, "events": checked, "broken_at": checked,
                                "checked": checked, "error": "corrupt jsonl line"}
                    ev = AuditEvent.from_dict(d)
                    if ev.prev_hash != prev:
                        return {"ok": False, "events": checked, "broken_at": checked,
                                "checked": checked, "error": "prev_hash mismatch"}
                    stored_hash = ev.event_hash
                    if ev.compute_hash(prev) != stored_hash:
                        return {"ok": False, "events": checked, "broken_at": checked,
                                "checked": checked, "error": "event_hash mismatch"}
                    prev = ev.event_hash
                    checked += 1
        except OSError as e:
            return {"ok": False, "events": checked, "broken_at": checked,
                    "checked": checked, "error": str(e)}
        return {"ok": True, "events": checked, "broken_at": None, "checked": checked}

    # -------------------------------------------------------------- export ---
    def export_events(self, limit: int = 5000, **query_kwargs) -> list[dict]:
        return self.query(limit=limit, **query_kwargs)

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def _ts(value: datetime | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def hash_events_in_batch(events: list[AuditEvent]) -> list[str]:
    """Chain a batch of events purely in memory (no store) for testing."""
    hashes: list[str] = []
    prev = None
    for e in events:
        hashes.append(e.compute_hash(prev))
        prev = hashes[-1]
    return hashes


# Kept importable for tests that don't want to spin up sqlite.
def default_jsonl_path(dir_path: Path) -> Path:
    return Path(dir_path) / "events.jsonl"
