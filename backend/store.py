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
