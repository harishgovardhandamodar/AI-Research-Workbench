"""Project persistence: messages, permission grants, and settings in SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

ROLES = {"user", "assistant", "tool", "system"}

# A single connection per project database, shared by ProjectStore and
# ArtifactStore. SQLite is opened in WAL mode so reads don't block the writer
# and the connection survives both store instances for the process lifetime.
_PROJECT_DB_CACHE: dict[str, sqlite3.Connection] = {}
_DB_CACHE_LOCK = threading.Lock()


def connect_project_db(project_dir: Path) -> sqlite3.Connection:
    """Open (or return the cached) connection for a project's workbench.db."""
    key = str(Path(project_dir).resolve())
    with _DB_CACHE_LOCK:
        conn = _PROJECT_DB_CACHE.get(key)
        if conn is None:
            project_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(project_dir / "workbench.db")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            _PROJECT_DB_CACHE[key] = conn
        return conn


def close_project_db(project_dir: Path) -> None:
    """Close and drop the cached connection for a project db (e.g. on delete)."""
    key = str(Path(project_dir).resolve())
    with _DB_CACHE_LOCK:
        conn = _PROJECT_DB_CACHE.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


class ProjectStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.db_path = project_dir / "workbench.db"
        self._conn = connect_project_db(project_dir)
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
                tool_sequence TEXT, artifact_ids TEXT, metrics TEXT, review TEXT,
                experiment_id INTEGER, config TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, hypothesis TEXT,
                goal_metric TEXT, goal_target REAL, higher_better INTEGER,
                status TEXT, created_at REAL, updated_at REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                metric TEXT PRIMARY KEY, target REAL, higher_better INTEGER,
                label TEXT, created_at REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS approval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, command TEXT, decision TEXT, temporary INTEGER,
                created_at REAL)"""
        )
        # Migration: older databases predate the metrics column.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN metrics TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate per-run experiment linkage.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN experiment_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE runs ADD COLUMN config TEXT")
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

    # -- approval audit log ---------------------------------------------------
    def log_approval(self, kind: str, command: str, decision: str,
                     temporary: bool = False):
        self._conn.execute(
            "INSERT INTO approval_log (kind, command, decision, temporary, created_at)"
            " VALUES (?,?,?,?,?)",
            (kind, command, decision, 1 if temporary else 0, time.time()))
        self._conn.commit()

    def list_approvals(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approval_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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
                review: dict | None = None,
                experiment_id: int | None = None,
                config: dict | None = None) -> int:
        """Persist one agent turn as a run row (prompt → reply → tool trail)."""
        cur = self._conn.execute(
            "INSERT INTO runs (prompt, reply, status, started_at, finished_at,"
            " tool_sequence, artifact_ids, metrics, review, experiment_id, config)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (prompt, reply, status, started_at, finished_at,
             json.dumps(tool_sequence or []), json.dumps(artifact_ids or []),
             json.dumps(metrics or {}), json.dumps(review or {}),
             experiment_id, json.dumps(config or {})))
        self._conn.commit()
        return cur.lastrowid

    def set_run_experiment(self, rid: int, experiment_id: int | None,
                           config: dict | None = None):
        self._conn.execute(
            "UPDATE runs SET experiment_id=?, config=? WHERE id=?",
            (experiment_id, json.dumps(config or {}), rid))
        self._conn.commit()

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
                "review": _jload(r["review"], {}),
                "experiment_id": r["experiment_id"],
                "config": _jload(r["config"], {})}

    # -- experiments (a family of runs around one research goal) ------------
    def create_experiment(self, name: str, hypothesis: str = "",
                          goal_metric: str = "", goal_target: float | None = None,
                          higher_better: bool = True) -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO experiments (name, hypothesis, goal_metric, goal_target,"
            " higher_better, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name, hypothesis, goal_metric, goal_target,
             1 if higher_better else 0, "active", now, now))
        self._conn.commit()
        return cur.lastrowid

    def list_experiments(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM experiments ORDER BY id").fetchall()
        out = []
        for r in rows:
            exp = self._row_experiment(r)
            exp["runs"] = len(self.experiment_runs(r["id"]))
            out.append(exp)
        return out

    def get_experiment(self, eid: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        return self._row_experiment(row) if row else None

    def experiment_runs(self, eid: int, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE experiment_id=? ORDER BY id DESC LIMIT ?",
            (eid, limit)).fetchall()
        return [self._row_run(r) for r in reversed(rows)]

    def update_experiment_status(self, eid: int, status: str):
        self._conn.execute(
            "UPDATE experiments SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), eid))
        self._conn.commit()

    def _row_experiment(self, r) -> dict:
        return {"id": r["id"], "name": r["name"], "hypothesis": r["hypothesis"],
                "goal_metric": r["goal_metric"], "goal_target": r["goal_target"],
                "higher_better": bool(r["higher_better"]), "status": r["status"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

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
