"""Project persistence: messages, permission grants, and settings in SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

ROLES = {"user", "assistant", "tool", "system"}

# Sentinel so callers can explicitly clear an optional field (e.g. goal_target,
# plan) — None means "leave unchanged", _UNSET clears the column.
_UNSET = object()

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
                experiment_id INTEGER, config TEXT, label TEXT, kind TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, hypothesis TEXT,
                goal_metric TEXT, goal_target REAL, higher_better INTEGER,
                status TEXT, created_at REAL, updated_at REAL, plan TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                metric TEXT PRIMARY KEY, target REAL, higher_better INTEGER,
                label TEXT, created_at REAL, experiment_id INTEGER)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS approval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, command TEXT, decision TEXT, temporary INTEGER,
                created_at REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER, source_run_id INTEGER, run_id INTEGER,
                title TEXT, action TEXT, prompt TEXT,
                status TEXT DEFAULT 'pending',
                baseline_value REAL, outcome_value REAL, delta REAL,
                improved INTEGER, created_at REAL, applied_at REAL)"""
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
        # Migration: older databases predate per-run variant labels.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN label TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate per-goal experiment scoping.
        try:
            c.execute("ALTER TABLE goals ADD COLUMN experiment_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate the run kind (agent_run / notebook /
        # privacy_workflow / ...) used by the Experiments traceability UI.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN kind TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate git-style run branching (which run
        # a run was derived from, for the branch-history graph).
        try:
            c.execute("ALTER TABLE runs ADD COLUMN parent_run_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate the experiment plan (B1: explicit
        # experiment-plan step the agent records when creating an experiment).
        try:
            c.execute("ALTER TABLE experiments ADD COLUMN plan TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate the per-run model label (which LLM
        # produced the run) used by chat bubbles and the Experiments timeline.
        try:
            c.execute("ALTER TABLE runs ADD COLUMN model TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: older databases predate the per-experiment pinned model
        # (which LLM should run this experiment's turns).
        try:
            c.execute("ALTER TABLE experiments ADD COLUMN model TEXT")
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

    def delete_message(self, mid: int) -> bool:
        """Delete one message row. Returns True if a row was removed."""
        cur = self._conn.execute("DELETE FROM messages WHERE id=?", (mid,))
        self._conn.commit()
        return cur.rowcount > 0

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
                config: dict | None = None,
                label: str | None = None,
                kind: str = "agent_run",
                parent_run_id: int | None = None,
                model: str | None = None) -> int:
        """Persist one agent turn as a run row (prompt → reply → tool trail).

        `kind` tags the source of the record (agent_run, notebook, workflow,
        privacy_workflow, ...) so the Experiments UI can render it generically.
        `parent_run_id` links a run to the run it was derived from (improve
        loops, reruns, branching) for the branch-history graph.
        `model` records which LLM produced the run, for the chat/timeline label.
        """
        cur = self._conn.execute(
            "INSERT INTO runs (prompt, reply, status, started_at, finished_at,"
            " tool_sequence, artifact_ids, metrics, review, experiment_id, config,"
            " label, kind, parent_run_id, model)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (prompt, reply, status, started_at, finished_at,
             json.dumps(tool_sequence or []), json.dumps(artifact_ids or []),
             json.dumps(metrics or {}), json.dumps(review or {}),
             experiment_id, json.dumps(config or {}), label or None,
             kind or "agent_run", parent_run_id, model or None))
        if experiment_id is not None:
            # A fresh run means the experiment is active now: bump updated_at so
            # "most recently active" experiment selection reflects real activity.
            self._conn.execute(
                "UPDATE experiments SET updated_at=? WHERE id=?",
                (time.time(), experiment_id))
        self._conn.commit()
        return cur.lastrowid

    def set_run_experiment(self, rid: int, experiment_id: int | None,
                           config: dict | None = None, label: str | None = None):
        self._conn.execute(
            "UPDATE runs SET experiment_id=?, config=?, label=? WHERE id=?",
            (experiment_id, json.dumps(config or {}), label or None, rid))
        self._conn.commit()

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            out.append(self._row_run(r))
        return out

    def count_runs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        return row["n"] if row else 0

    def get_run(self, rid: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return self._row_run(row) if row else None

    def update_run_review(self, rid: int, review: dict):
        self._conn.execute(
            "UPDATE runs SET review=? WHERE id=?",
            (json.dumps(review), rid))
        self._conn.commit()

    # -- suggestions (first-class reviewer suggestion records) ---------------
    def add_suggestions(self, experiment_id: int | None, source_run_id: int | None,
                        review: dict) -> list[int]:
        """Persist the reviewer's suggestions as first-class rows and return
        their ids (attached to the suggestions so the UI can reference them)."""
        ids = []
        for s in (review or {}).get("suggestions") or []:
            title = str(s.get("title") or "") if isinstance(s, dict) else ""
            action = str(s.get("action") or "") if isinstance(s, dict) else str(s)
            prompt = str(s.get("prompt") or "") if isinstance(s, dict) else str(s)
            if not title and not prompt:
                continue
            cur = self._conn.execute(
                "INSERT INTO suggestions (experiment_id, source_run_id, title,"
                " action, prompt, status, created_at)"
                " VALUES (?,?,?,?,?,'pending',?)",
                (experiment_id, source_run_id, title, action, prompt, time.time()))
            ids.append(cur.lastrowid)
        self._conn.commit()
        return ids

    def get_suggestion(self, sid: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        return self._row_suggestion(row) if row else None

    def list_suggestions(self, experiment_id: int | None = None,
                         status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM suggestions"
        conds, vals = [], []
        if experiment_id is not None:
            conds.append("experiment_id=?"); vals.append(experiment_id)
        if status:
            conds.append("status=?"); vals.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, vals).fetchall()
        return [self._row_suggestion(r) for r in rows]

    def mark_suggestion_applied(self, sid: int, run_id: int | None = None) -> None:
        self._conn.execute(
            "UPDATE suggestions SET status='applied', run_id=?, applied_at=?"
            " WHERE id=?", (run_id, time.time(), sid))
        self._conn.commit()

    def resolve_suggestion_outcome(self, sid: int) -> dict | None:
        """Regression check: compare the applied run's goal metric against the
        source run's. Sets baseline/outcome/delta/improved and the final status
        (accepted if it improved, rejected otherwise). Returns the suggestion."""
        sug = self.get_suggestion(sid)
        if sug is None or sug["status"] != "applied" or sug["run_id"] is None:
            return sug
        source = self.get_run(sug["source_run_id"]) if sug["source_run_id"] else None
        applied = self.get_run(sug["run_id"])
        exp = self.get_experiment(sug["experiment_id"]) if sug["experiment_id"] else None
        metric = (exp or {}).get("goal_metric") or ""
        higher = bool((exp or {}).get("higher_better", True))
        if not metric or applied is None:
            return sug
        m0 = (source or {}).get("metrics") or {}
        m1 = applied.get("metrics") or {}
        try:
            base = float(m0.get(metric))
        except (TypeError, ValueError):
            base = None
        try:
            out = float(m1.get(metric))
        except (TypeError, ValueError):
            out = None
        improved = None
        delta = None
        if base is not None and out is not None:
            delta = out - base
            improved = 1 if (out > base if higher else out < base) else 0
        status = "accepted" if improved else "rejected" if improved is not None else "applied"
        self._conn.execute(
            "UPDATE suggestions SET baseline_value=?, outcome_value=?, delta=?,"
            " improved=?, status=? WHERE id=?",
            (base, out, delta, improved, status, sid))
        self._conn.commit()
        return self.get_suggestion(sid)

    def _row_suggestion(self, r) -> dict:
        return {"id": r["id"], "experiment_id": r["experiment_id"],
                "source_run_id": r["source_run_id"], "run_id": r["run_id"],
                "title": r["title"], "action": r["action"], "prompt": r["prompt"],
                "status": r["status"], "baseline_value": r["baseline_value"],
                "outcome_value": r["outcome_value"], "delta": r["delta"],
                "improved": r["improved"], "created_at": r["created_at"],
                "applied_at": r["applied_at"]}

    def _row_run(self, r) -> dict:
        return {"id": r["id"], "prompt": r["prompt"], "reply": r["reply"],
                "status": r["status"], "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "tool_sequence": _jload(r["tool_sequence"], []),
                "artifact_ids": _jload(r["artifact_ids"], []),
                "metrics": _jload(r["metrics"], {}),
                "review": _jload(r["review"], {}),
                "experiment_id": r["experiment_id"],
                "config": _jload(r["config"], {}),
                "label": r["label"],
                "kind": r["kind"] or "agent_run",
                "parent_run_id": r["parent_run_id"],
                "model": r["model"]}

    # -- experiments (a family of runs around one research goal) ------------
    def create_experiment(self, name: str, hypothesis: str = "",
                          goal_metric: str = "", goal_target: float | None = None,
                          higher_better: bool = True, plan: str = "",
                          model: str = "") -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO experiments (name, hypothesis, goal_metric, goal_target,"
            " higher_better, status, created_at, updated_at, plan, model)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, hypothesis, goal_metric, goal_target,
             1 if higher_better else 0, "active", now, now, plan or None,
             model or None))
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

    def update_experiment(self, eid: int, *, name: str | None = None,
                          hypothesis: str | None = None,
                          goal_metric: str | None = None,
                          goal_target: float | str | None = None,
                          higher_better: bool | None = None,
                          plan: str | None = None,
                          model: str | None = None) -> None:
        """Edit an experiment's objective fields in place (co-design: the user can
        refine a hypothesis/goal without recreating the experiment and orphaning
        its runs). Bumps updated_at. Pass _UNSET to clear an optional field."""
        exp = self.get_experiment(eid)
        if exp is None:
            return
        sets, vals = [], []
        if name is not None:
            sets.append("name=?"); vals.append(name.strip() or exp["name"])
        if hypothesis is not None:
            sets.append("hypothesis=?"); vals.append(hypothesis)
        if goal_metric is not None:
            sets.append("goal_metric=?"); vals.append(goal_metric)
        if goal_target is not None:
            sets.append("goal_target=?")
            vals.append(None if goal_target is _UNSET else goal_target)
        if higher_better is not None:
            sets.append("higher_better=?"); vals.append(1 if higher_better else 0)
        if plan is not None:
            sets.append("plan=?"); vals.append(None if plan is _UNSET else plan)
        if model is not None:
            sets.append("model=?"); vals.append(None if model is _UNSET else model)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(time.time())
        self._conn.execute(
            f"UPDATE experiments SET {', '.join(sets)} WHERE id=?", (*vals, eid))
        self._conn.commit()

    def _row_experiment(self, r) -> dict:
        return {"id": r["id"], "name": r["name"], "hypothesis": r["hypothesis"],
                "goal_metric": r["goal_metric"], "goal_target": r["goal_target"],
                "higher_better": bool(r["higher_better"]), "status": r["status"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "plan": r["plan"] or "", "model": r["model"] or ""}

    # -- goals (target metric + direction, for improvement tracking) --------
    def add_goal(self, metric: str, target: float, higher_better: bool,
                 label: str = "", experiment_id: int | None = None) -> None:
        """Add/refresh a goal. experiment_id scopes it to one experiment;
        None means project-wide. One row per metric (INSERT OR REPLACE)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO goals (metric, target, higher_better, label,"
            " created_at, experiment_id) VALUES (?,?,?,?,?,?)",
            (metric, target, 1 if higher_better else 0, label, time.time(),
             experiment_id))
        self._conn.commit()

    def list_goals(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM goals ORDER BY created_at").fetchall()
        return [{"metric": r["metric"], "target": r["target"],
                 "higher_better": bool(r["higher_better"]), "label": r["label"],
                 "created_at": r["created_at"],
                 "experiment_id": r["experiment_id"]} for r in rows]

    def goals_for_experiment(self, eid: int) -> list[dict]:
        """Goals that apply to an experiment: its own scoped goals plus any
        project-wide (unscoped) goals."""
        return [g for g in self.list_goals()
                if g["experiment_id"] is None or g["experiment_id"] == eid]

    def delete_goal(self, metric: str, experiment_id: int | None = None) -> bool:
        """Delete a goal. With experiment_id, only the scoped row for that
        experiment is removed; otherwise the project-wide (unscoped) row."""
        if experiment_id is None:
            cur = self._conn.execute(
                "DELETE FROM goals WHERE metric=? AND experiment_id IS NULL",
                (metric,))
        else:
            cur = self._conn.execute(
                "DELETE FROM goals WHERE metric=? AND experiment_id=?",
                (metric, experiment_id))
        self._conn.commit()
        return cur.rowcount > 0


def _jload(raw: str | None, default):
    try:
        return json.loads(raw or "null") or default
    except json.JSONDecodeError:
        return default
