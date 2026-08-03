"""Project persistence: messages, permission grants, and settings in SQLite."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROLES = {"user", "assistant", "tool", "system"}


class ProjectStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        project_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = project_dir / "workbench.db"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        c = self._conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL, content TEXT NOT NULL,
                created_at REAL, meta TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, pattern TEXT NOT NULL,
                decision TEXT NOT NULL, UNIQUE(kind, pattern))"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL, finished_at REAL,
                title TEXT, status TEXT, pct REAL, stages TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT, reply TEXT, status TEXT,
                started_at REAL, finished_at REAL,
                tool_sequence TEXT, artifact_ids TEXT, metrics TEXT, review TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                metric TEXT PRIMARY KEY, target REAL, higher_better INTEGER,
                label TEXT, created_at REAL)"""
        )
        # Migration: older databases predate the metrics column.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN metrics TEXT")
        except sqlite3.OperationalError:
            pass
        c.commit()

    # -- messages -----------------------------------------------------------
    def add_message(self, role: str, content: str, meta: dict | None = None) -> int:
        if role not in ROLES:
            role = "assistant"
        cur = self._conn.execute(
            "INSERT INTO messages (role, content, created_at, meta) VALUES (?,?,?,?)",
            (role, content, time.time(), json.dumps(meta or {})),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_messages(self, limit: int = 500) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_msg(r) for r in reversed(rows)]

    def get_message(self, mid: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return self._row_msg(row) if row else None

    def _row_msg(self, row) -> dict:
        d = {"id": row["id"], "role": row["role"], "content": row["content"],
             "created_at": row["created_at"]}
        try:
            d["meta"] = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        return d

    def clear_messages(self):
        self._conn.execute("DELETE FROM messages")
        self._conn.commit()

    # -- grants -------------------------------------------------------------
    def get_grant(self, kind: str, pattern: str) -> str | None:
        row = self._conn.execute(
            "SELECT decision FROM grants WHERE kind=? AND pattern=?",
            (kind, pattern)).fetchone()
        return row["decision"] if row else None

    def set_grant(self, kind: str, pattern: str, decision: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO grants (kind, pattern, decision) VALUES (?,?,?)",
            (kind, pattern, decision))
        self._conn.commit()

    def list_grants(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM grants").fetchall()
        return [dict(r) for r in rows]

    # -- settings -----------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        self._conn.commit()

    # -- workflow runs (traceability of pipelines) -------------------------
    def add_workflow_run(self, snapshot: dict):
        """Record a finished/current workflow run for history + traceability."""
        now = time.time()
        stages = snapshot.get("stages") or []
        self._conn.execute(
            "INSERT INTO workflow_runs (started_at, finished_at, title, status, pct, stages)"
            " VALUES (?,?,?,?,?,?)",
            (snapshot.get("started_at", now), now, snapshot.get("title", ""),
             snapshot.get("status", "idle"), snapshot.get("pct", 0),
             json.dumps(stages)))
        self._conn.commit()

    def list_workflow_runs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM workflow_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            try:
                stages = json.loads(r["stages"] or "[]")
            except json.JSONDecodeError:
                stages = []
            out.append({"id": r["id"], "started_at": r["started_at"],
                        "finished_at": r["finished_at"], "title": r["title"],
                        "status": r["status"], "pct": r["pct"], "stages": stages})
        return out

    # -- agent runs (every completed agent turn, for traceability) ---------
    def add_run(self, prompt: str, reply: str, status: str,
                started_at: float, finished_at: float,
                tool_sequence: list | None = None,
                artifact_ids: list | None = None,
                metrics: dict | None = None,
                review: dict | None = None) -> int:
        """Persist one agent turn as a run row (prompt → reply → tool trail)."""
        cur = self._conn.execute(
            "INSERT INTO runs (prompt, reply, status, started_at, finished_at,"
            " tool_sequence, artifact_ids, metrics, review) VALUES (?,?,?,?,?,?,?,?,?)",
            (prompt, reply, status, started_at, finished_at,
             json.dumps(tool_sequence or []), json.dumps(artifact_ids or []),
             json.dumps(metrics or {}), json.dumps(review or {})))
        self._conn.commit()
        return cur.lastrowid

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            out.append(self._row_run(r))
        return out

    def get_run(self, rid: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return self._row_run(row) if row else None

    def update_run_review(self, rid: int, review: dict):
        self._conn.execute(
            "UPDATE runs SET review=? WHERE id=?",
            (json.dumps(review), rid))
        self._conn.commit()

    def _row_run(self, r) -> dict:
        return {"id": r["id"], "prompt": r["prompt"], "reply": r["reply"],
                "status": r["status"], "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "tool_sequence": _jload(r["tool_sequence"], []),
                "artifact_ids": _jload(r["artifact_ids"], []),
                "metrics": _jload(r["metrics"], {}),
                "review": _jload(r["review"], {})}

    # -- goals (target metric + direction, for improvement tracking) --------
    def add_goal(self, metric: str, target: float, higher_better: bool,
                 label: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goals (metric, target, higher_better, label, created_at)"
            " VALUES (?,?,?,?,?)",
            (metric, target, 1 if higher_better else 0, label, time.time()))
        self._conn.commit()

    def list_goals(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM goals ORDER BY created_at").fetchall()
        return [{"metric": r["metric"], "target": r["target"],
                 "higher_better": bool(r["higher_better"]), "label": r["label"],
                 "created_at": r["created_at"]} for r in rows]

    def delete_goal(self, metric: str) -> bool:
        cur = self._conn.execute("DELETE FROM goals WHERE metric=?", (metric,))
        self._conn.commit()
        return cur.rowcount > 0


def _jload(raw: str | None, default):
    try:
        return json.loads(raw or "null") or default
    except json.JSONDecodeError:
        return default
