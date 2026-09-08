"""Reinforcement loop — gathers ledger, computes reward, updates memory/rank.

Narrow AGI reinforcement: per-workbench reward = f(feedback, tool success,
paper rank, artifact). Loop is local, deterministic, audited via ledger.

This is the "learn continually" invariant: every execution is scored, high-reward
patterns are persisted to memory and used to bias next `hive experiment run`
(prompt template, rank weights).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from hive.ledger.store import LEDGER_DB, init_ledger, query_ledger, ledger_stats
from hive.learn.memory import add_memory, query_memory
from hive.config import CONFIG_DIR


SNAPSHOT_DIR = CONFIG_DIR / "learn_snapshots"


def _compute_reward(exec_row: dict) -> float:
    """Heuristic reward 0-5 from status, tool, feedback."""
    base = 3.0
    cmd = exec_row.get("command", "")
    status = exec_row.get("status", "ok")
    reward = exec_row.get("reward")
    if reward is not None:
        try:
            base = float(reward)
        except Exception:
            pass
    # tool success bonus
    if status == "ok":
        base += 0.3
    elif status == "error":
        base -= 1.0
    if "llm_chat" in cmd or "tool:" in cmd:
        base += 0.2
    # clamp
    return max(0.0, min(5.0, round(base, 2)))


def learn_status() -> dict:
    init_ledger()
    stats = ledger_stats()
    # memory
    from hive.learn.memory import memory_stats

    mstats = memory_stats()
    # snapshots
    snaps = []
    if SNAPSHOT_DIR.exists():
        for p in sorted(SNAPSHOT_DIR.glob("*.json"))[-5:]:
            try:
                j = json.loads(p.read_text())
                snaps.append({"file": p.name, "ts": j.get("ts"), "workbench": j.get("workbench")})
            except Exception:
                continue
    return {"ledger": stats, "memory": mstats, "snapshots": snaps}


def run_loop(workbench: str = "default", iterations: int = 10, reward_threshold: float = 4.0, learning_rate: float = 0.3, dry: bool = False) -> dict:
    """Run reinforcement loop: score recent executions, persist high-reward memory.

    Returns {scored, promoted, snapshot}.
    """
    init_ledger()
    rows = query_ledger(limit=iterations * 3, workbench=workbench if workbench != "default" else None)
    if workbench == "default":
        # also consider all workbenches
        rows = query_ledger(limit=iterations * 3)
    scored = 0
    promoted = 0
    details: list[dict] = []
    for r in rows[:iterations]:
        reward = _compute_reward(r)
        scored += 1
        # update ledger reward if missing or stale
        con = sqlite3.connect(LEDGER_DB)
        # only update if reward differs significantly
        cur = con.execute("SELECT reward FROM executions WHERE id=?", (r["id"],))
        row = cur.fetchone()
        old = row[0] if row else None
        if old is None or abs(float(old) - reward) > 0.25:
            con.execute("UPDATE executions SET reward=? WHERE id=?", (reward, r["id"]))
            con.commit()
        con.close()
        # promote high-reward to memory (continual learning)
        if reward >= reward_threshold and not dry:
            # avoid duplicate
            existing = query_memory(workbench=r["workbench"], limit=20)
            content_sig = f"{r['command']}:{r['args'][:80]}"
            if not any(content_sig in m["content"] for m in existing):
                add_memory(
                    workbench=r["workbench"],
                    kind="reinforced",
                    content=f"{content_sig} → reward {reward} ({r.get('provider') or ''} {r.get('model') or ''})",
                    score=reward,
                )
                promoted += 1
        details.append({"id": r["id"], "command": r["command"], "reward": reward, "workbench": r["workbench"]})
    # snapshot for rollback
    snap_path = None
    if not dry:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snap_path = SNAPSHOT_DIR / f"{workbench}_{int(time.time())}.json"
        snap_path.write_text(
            json.dumps({"workbench": workbench, "ts": time.time(), "scored": scored, "promoted": promoted, "reward_threshold": reward_threshold, "learning_rate": learning_rate, "details": details}, indent=2)
        )
    return {"workbench": workbench, "scored": scored, "promoted": promoted, "details": details, "snapshot": str(snap_path) if snap_path else None}


def rollback(snapshot: str | None = None) -> dict:
    """Rollback last snapshot: remove its promoted memory entries."""
    if not SNAPSHOT_DIR.exists():
        return {"ok": False, "msg": "no snapshots"}
    snaps = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not snaps:
        return {"ok": False, "msg": "no snapshots"}
    target = Path(snapshot) if snapshot else snaps[-1]
    if not target.exists():
        # try by name
        cand = SNAPSHOT_DIR / snapshot if snapshot else None
        if cand and cand.exists():
            target = cand
        else:
            return {"ok": False, "msg": f"snapshot not found: {snapshot}"}
    try:
        j = json.loads(target.read_text())
        # remove memory added after snapshot ts (heuristic: score 4.0+ within 60s)
        # simpler: delete last promoted count
        promoted = j.get("promoted", 0)
        if promoted:
            import sqlite3 as _s

            con = _s.connect(LEDGER_DB)
            # delete most recent promoted memories for workbench
            con.execute(
                "DELETE FROM memory WHERE id IN (SELECT id FROM memory WHERE workbench=? AND kind='reinforced' ORDER BY ts DESC LIMIT ?)",
                (j.get("workbench", "default"), promoted),
            )
            con.commit()
            con.close()
        target.unlink(missing_ok=True)
        return {"ok": True, "snapshot": str(target), "promoted_rolled_back": promoted}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
