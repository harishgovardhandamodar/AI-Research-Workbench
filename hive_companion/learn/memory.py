"""Memory — SQLite-backed per-workbench store for continual learning."""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from hive.ledger.store import LEDGER_DB, init_ledger


def add_memory(workbench: str, kind: str, content: str, score: float = 0.0) -> str:
    init_ledger()
    mid = uuid.uuid4().hex[:8]
    con = sqlite3.connect(LEDGER_DB)
    con.execute(
        "INSERT INTO memory VALUES (?,?,?,?,?,?)",
        (mid, workbench, kind, content[:4000], float(score), time.time()),
    )
    con.commit()
    con.close()
    return mid


def query_memory(workbench: str | None = None, kind: str | None = None, limit: int = 20) -> list[dict]:
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    q = "SELECT id, workbench, kind, content, score, ts FROM memory WHERE 1=1"
    params: list = []
    if workbench:
        q += " AND workbench=?"
        params.append(workbench)
    if kind:
        q += " AND kind=?"
        params.append(kind)
    q += " ORDER BY score DESC, ts DESC LIMIT ?"
    params.append(limit)
    cur = con.execute(q, params)
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "workbench": r[1], "kind": r[2], "content": r[3], "score": r[4], "ts": r[5]}
        for r in rows
    ]


def memory_stats() -> dict:
    init_ledger()
    con = sqlite3.connect(LEDGER_DB)
    total = con.execute("SELECT count(*) FROM memory").fetchone()[0]
    by_wb = con.execute("SELECT workbench, count(*), avg(score) FROM memory GROUP BY workbench").fetchall()
    con.close()
    return {
        "total": total,
        "by_workbench": [{"workbench": w, "count": c, "avg_score": s} for w, c, s in by_wb],
    }
