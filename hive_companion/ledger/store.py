"""Ledger store — SQLite + hash chain (reuse audit pattern).

Gathering invariant: every CLI command, research workflow, machine tool, and web
action is logged here with workbench, command, args, provenance, and optional
reward/feedback. Local-only by default; redacts via hive.machine.sensitive.

DB: ~/.hive/ledger.db (separate from hive.db + audit.db for clear ownership,
but query_ledger() unions all three for dashboard).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from hive.config import CONFIG_DIR, DB_FILE
from hive.machine import AUDIT_DB
from hive.machine.sensitive import redact, classify  # type: ignore

LEDGER_DB = CONFIG_DIR / "ledger.db"


def init_ledger() -> None:
    LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(LEDGER_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS executions (
        id TEXT PRIMARY KEY,
        ts REAL,
        workbench TEXT,
        command TEXT,
        args TEXT,
        provider TEXT,
        model TEXT,
        status TEXT,
        reward REAL,
        feedback TEXT,
        prev_hash TEXT,
        hash TEXT,
        meta TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        execution_id TEXT,
        reward INTEGER,
        note TEXT,
        ts REAL,
        FOREIGN KEY(execution_id) REFERENCES executions(id))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS memory (
        id TEXT PRIMARY KEY,
        workbench TEXT,
        kind TEXT,
        content TEXT,
        score REAL,
        ts REAL)"""
    )
    # index
    con.execute("CREATE INDEX IF NOT EXISTS idx_exec_ts ON executions(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exec_wb ON executions(workbench)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_mem_wb ON memory(workbench)")
    con.commit()
    con.close()


def _hash_entry(prev: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(prev.encode())
    h.update(json.dumps(payload, sort_keys=True).encode())
    return h.hexdigest()[:16]


def log_execution(
    command: str,
    args: dict | None = None,
    workbench: str = "default",
    provider: str | None = None,
    model: str | None = None,
    status: str = "ok",
    reward: float | None = None,
    meta: dict | None = None,
) -> str:
    """Log a top-level CLI execution. Redacts args, hash-chains. Returns id."""
    init_ledger()
    eid = uuid.uuid4().hex[:8]
    ts = time.time()
    # redact sensitive before persist
    raw_args = json.dumps(args or {}, ensure_ascii=False)
    redacted_args = redact(raw_args) if raw_args else "{}"
    # classify for trust-boundary
    # not used for blocking, just stored
    _ = classify(f"{command} {redacted_args}")

    con = sqlite3.connect(LEDGER_DB)
    # prev hash
    cur = con.execute("SELECT hash FROM executions ORDER BY ts DESC LIMIT 1")
    row = cur.fetchone()
    prev = row[0] if row else "0" * 16
    payload = {
        "command": command,
        "args": redacted_args,
        "workbench": workbench,
        "provider": provider,
        "model": model,
        "ts": ts,
    }
    h = _hash_entry(prev, payload)
    con.execute(
        "INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid,
            ts,
            workbench,
            command,
            redacted_args,
            provider,
            model,
            status,
            reward,
            None,
            prev,
            h,
            json.dumps(meta or {}, ensure_ascii=False)[:4000],
        ),
    )
    con.commit()
    con.close()
    return eid


def log_tool(
    tool: str,
    args: dict | None = None,
    workbench: str = "default",
    status: str = "ok",
    meta: dict | None = None,
) -> str:
    """Thin wrapper for tool-level events (machine tools, paper fetch, rank)."""
    return log_execution(
        command=f"tool:{tool}", args=args, workbench=workbench, status=status, meta=meta
    )


def add_feedback(execution_id: str, reward: int, note: str = "") -> str:
    """Human / auto feedback → updates executions.reward and appends feedback row."""
    assert 1 <= reward <= 5, "reward 1-5"
    init_ledger()
    fid = uuid.uuid4().hex[:8]
    ts = time.time()
    con = sqlite3.connect(LEDGER_DB)
    con.execute(
        "INSERT INTO feedback VALUES (?,?,?,?,?)", (fid, execution_id, reward, note[:2000], ts)
    )
    # also update executions.reward as moving avg or last
    cur = con.execute("SELECT reward FROM executions WHERE id=?", (execution_id,))
    row = cur.fetchone()
    if row is not None:
        # simple: average of existing reward and new (if existing)
        old = row[0]
        new_reward = float(reward) if old is None else (float(old) + float(reward)) / 2
        con.execute(
            "UPDATE executions SET reward=?, feedback=? WHERE id=?",
            (new_reward, note[:1000], execution_id),
        )
    con.commit()
    con.close()
    return fid


def get_execution(eid: str) -> dict | None:
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    cur = con.execute("SELECT * FROM executions WHERE id=?", (eid,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    cols = [
        "id",
        "ts",
        "workbench",
        "command",
        "args",
        "provider",
        "model",
        "status",
        "reward",
        "feedback",
        "prev_hash",
        "hash",
        "meta",
    ]
    return dict(zip(cols, row))


def query_ledger(
    limit: int = 50,
    workbench: str | None = None,
    command_like: str | None = None,
    min_reward: float | None = None,
) -> list[dict]:
    """Unified query: ledger + audit + sessions for dashboard."""
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    q = "SELECT id, ts, workbench, command, args, provider, model, status, reward, feedback, hash FROM executions WHERE 1=1"
    params: list[Any] = []
    if workbench:
        q += " AND workbench=?"
        params.append(workbench)
    if command_like:
        q += " AND command LIKE ?"
        params.append(f"%{command_like}%")
    if min_reward is not None:
        q += " AND reward >= ?"
        params.append(min_reward)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    cur = con.execute(q, params)
    rows = cur.fetchall()
    con.close()
    cols = ["id", "ts", "workbench", "command", "args", "provider", "model", "status", "reward", "feedback", "hash"]
    out = [dict(zip(cols, r)) for r in rows]
    # also merge lightweight session markers if ledger sparse
    if len(out) < limit:
        try:
            import sqlite3 as _sq
            if DB_FILE.exists():
                c2 = _sq.connect(DB_FILE)
                cur2 = c2.execute(
                    "SELECT id, topic, created_at FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (limit - len(out),),
                )
                for sid, topic, ts in cur2.fetchall():
                    out.append(
                        {
                            "id": sid,
                            "ts": ts,
                            "workbench": "research",
                            "command": f"session:{topic[:60]}",
                            "args": "{}",
                            "provider": None,
                            "model": None,
                            "status": "ok",
                            "reward": None,
                            "feedback": None,
                            "hash": sid,
                        }
                    )
                c2.close()
        except Exception:
            pass
    out.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return out[:limit]


def ledger_stats() -> dict:
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    total = con.execute("SELECT count(*) FROM executions").fetchone()[0]
    by_wb = con.execute("SELECT workbench, count(*), avg(reward) FROM executions GROUP BY workbench").fetchall()
    by_cmd = con.execute("SELECT command, count(*) FROM executions GROUP BY command ORDER BY count(*) DESC LIMIT 10").fetchall()
    avg_reward = con.execute("SELECT avg(reward) FROM executions WHERE reward IS NOT NULL").fetchone()[0]
    con.close()
    return {
        "total": total,
        "by_workbench": [{"workbench": w, "count": c, "avg_reward": r} for w, c, r in by_wb],
        "by_command": [{"command": c, "count": n} for c, n in by_cmd],
        "avg_reward": avg_reward,
    }


def verify_ledger() -> tuple[bool, str]:
    """Verify hash chain."""
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    cur = con.execute("SELECT id, ts, workbench, command, args, provider, model, prev_hash, hash FROM executions ORDER BY ts ASC")
    rows = cur.fetchall()
    con.close()
    prev = "0" * 16
    for r in rows:
        _id, ts, wb, cmd, args, prov, model, ph, h = r
        if ph != prev:
            return False, f"chain break at { _id }: expected {prev} got {ph}"
        payload = {"command": cmd, "args": args, "workbench": wb, "provider": prov, "model": model, "ts": ts}
        exp = _hash_entry(prev, payload)
        if exp != h:
            return False, f"hash mismatch at {_id}: expected {exp} got {h}"
        prev = h
    return True, f"ok {len(rows)} entries chain verified"
