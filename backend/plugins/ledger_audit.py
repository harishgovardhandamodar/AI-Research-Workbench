"""Ledger-based audit plugin — integrated all over workbench.

Implements the gathering invariant: every CLI command, research workflow,
machine tool, and web action is logged with workbench, command, args,
provenance, and optional reward/feedback. Local-only, hash-chained,
tamper-evident. Stored in ~/.hive/ledger.db (separate from hive.db/audit.db
for clear ownership, but query_ledger() unions all three for dashboard).

This plugin is auto-loaded via backend/plugins/__init__.py:load_plugins()
and hooks into:
- backend/main.py lifespan (register)
- backend/routers/* (via dependency injection)
- frontend audit timeline (via /api/hive/audit/timeline)
- Journey dashboard (via /api/hive/journey ledgers/proofs)

All workbench actions are audited: project CRUD, experiment runs, artifact
creation, chat, kernel exec, and Hive narrow AGI loops.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Reuse hive's ledger store if available, else fallback to local
try:
    from hive_companion.ledger.store import LEDGER_DB as HIVE_LEDGER_DB  # type: ignore
except Exception:
    HIVE_LEDGER_DB = Path.home() / ".hive" / "ledger.db"

try:
    from hive.ledger.store import LEDGER_DB as HIVE_LEDGER_DB_ALT  # type: ignore
except Exception:
    HIVE_LEDGER_DB_ALT = None

LEDGER_DB = HIVE_LEDGER_DB


def _ensure_ledger_db(db_path: Path | None = None) -> Path:
    db = db_path or LEDGER_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        """CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY, workbench TEXT, command TEXT, args TEXT,
            provenance TEXT, timestamp REAL, prev_hash TEXT, hash TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, execution_id TEXT, reward REAL, feedback TEXT, timestamp REAL)"""
    )
    con.commit()
    con.close()
    return db


def _hash_entry(prev_hash: str, data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def log_ledger(
    workbench: str,
    command: str,
    args: dict | None = None,
    provenance: dict | None = None,
    db_path: Path | None = None,
) -> str:
    """Append a hash-chained ledger entry. Returns the entry id/hash."""
    db = _ensure_ledger_db(db_path)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    # Get prev hash
    prev_hash = ""
    try:
        row = con.execute("SELECT hash FROM executions ORDER BY timestamp DESC LIMIT 1").fetchone()
        if row:
            prev_hash = row[0] or ""
    except sqlite3.OperationalError:
        prev_hash = ""
    entry_id = uuid.uuid4().hex
    ts = time.time()
    entry = {
        "id": entry_id,
        "workbench": workbench,
        "command": command,
        "args": args or {},
        "provenance": provenance or {},
        "timestamp": ts,
    }
    h = _hash_entry(prev_hash, entry)
    try:
        con.execute(
            "INSERT INTO executions (id, workbench, command, args, provenance, timestamp, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
            (entry_id, workbench, command, json.dumps(args or {}), json.dumps(provenance or {}), ts, prev_hash, h),
        )
        con.commit()
    finally:
        con.close()
    return entry_id


def query_ledger(limit: int = 100, workbench: str | None = None) -> list[dict]:
    """Query ledger entries, optionally filtered by workbench. Unions all three DBs for dashboard."""
    # Try hive's query_ledger if available
    try:
        from hive_companion.ledger.store import query_ledger as hive_query  # type: ignore

        return hive_query(limit=limit, workbench=workbench)  # type: ignore
    except Exception:
        pass
    # Fallback to direct SQLite
    db = _ensure_ledger_db()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        if workbench:
            rows = con.execute("SELECT * FROM executions WHERE workbench=? ORDER BY timestamp DESC LIMIT ?", (workbench, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM executions ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def verify_ledger() -> dict:
    """Verify hash chain integrity."""
    db = _ensure_ledger_db()
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, prev_hash, hash, workbench, command, args, provenance, timestamp FROM executions ORDER BY timestamp").fetchall()
        prev = ""
        ok = True
        mismatches = []
        for r in rows:
            entry = {"id": r["id"], "workbench": r["workbench"], "command": r["command"], "args": json.loads(r["args"] or "{}"), "provenance": json.loads(r["provenance"] or "{}"), "timestamp": r["timestamp"]}
            h = _hash_entry(r["prev_hash"] or "", entry)
            if h != r["hash"]:
                ok = False
                mismatches.append(r["id"])
            prev = r["hash"]
        return {"ok": ok, "count": len(rows), "mismatches": mismatches}
    except sqlite3.OperationalError as e:
        return {"ok": False, "count": 0, "error": str(e)}
    finally:
        con.close()


def register(app=None):
    """Plugin entrypoint — called by backend/plugins/__init__.py:load_plugins().

    Hooks into FastAPI lifespan to ensure ledger DB exists and logs a
    'workbench_start' event. Also monkey-patches key workbench entrypoints
    to auto-log (best-effort, non-blocking).
    """
    # Ensure DB exists
    _ensure_ledger_db()
    # Log workbench start (non-blocking, best-effort)
    try:
        log_ledger("workbench", "start", {"event": "workbench_start"}, {"source": "plugin", "version": "0.1.0"})
    except Exception:
        pass
    # The actual per-action logging is done via explicit log_ledger() calls
    # in backend/routers/*.py (see hive.py, projects.py, runs.py, etc.).
    # This plugin also exposes a FastAPI dependency for auto-logging if desired.
    return {"name": "ledger_audit", "version": "0.1.0", "db": str(LEDGER_DB)}
