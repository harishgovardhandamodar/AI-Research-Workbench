"""MRM persistence: SQLite-backed inventory, approvals, evidence and audit log.

Authoritative model + simulation + dataset inventory with SR 11-7 lifecycle
states, risk tiering (1/2/3), maker-checker approvals and an **append-only**
audit log (INSERT only — UPDATE/DELETE are never exposed). One SQLite file
keeps the MVP self-contained; the schema maps 1:1 to a Postgres deployment.

The store root is ``$FOX_MRM_STORE`` (the MCP host injects it, mirroring
``FOX_PLAN_STORE``), defaulting to ``~/.fox/mrm``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import MRM_STORE_ENV

# --------------------------------------------------------------------------- RBAC --

ROLES = ("developer", "validator", "auditor", "admin")
"""1st line develops/uses, 2nd line validates/challenges, 3rd line audits."""

# Roles allowed to *decide* on maker-checker approval requests (2nd/3rd line).
_CHECKER_ROLES = ("validator", "auditor", "admin")
# Roles allowed to change a model's tier / status / retirement without a fresh
# human approval on record. Everyone else must go through request_approval.
_TIER_ROLES = ("validator", "admin")

MODEL_STATUSES = (
    "development", "proposed", "validation", "approved", "monitoring",
    "retirement_pending", "retired",
)

APPROVAL_GATED_ACTIONS = ("tier", "status", "retire", "deploy", "use_synthetic")


class PermissionError_(PermissionError):
    """Raised when a maker/checker or RBAC rule blocks an action."""


def require_role(actor_role: str, allowed: tuple[str, ...], action: str) -> None:
    """Raise unless ``actor_role`` is in ``allowed`` (mirrors purpose binding)."""
    role = (actor_role or "developer").strip().lower()
    if role not in ROLES:
        raise PermissionError_(f"unknown role '{role}' — must be one of {list(ROLES)}")
    if role not in allowed:
        raise PermissionError_(
            f"role '{role}' is not allowed to {action}; requires "
            f"{list(allowed)}")


# -------------------------------------------------------------------------- store --

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  tier INTEGER NOT NULL DEFAULT 3,
  status TEXT NOT NULL DEFAULT 'development',
  description TEXT DEFAULT '',
  owner TEXT DEFAULT '1st-line',
  validator TEXT DEFAULT '',
  synthetic_used INTEGER NOT NULL DEFAULT 0,
  tstr_completed INTEGER NOT NULL DEFAULT 0,
  use_limitations TEXT DEFAULT '',
  assumptions TEXT DEFAULT '[]',
  created_at REAL,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS simulations (
  id TEXT PRIMARY KEY,
  model_id TEXT DEFAULT '',
  name TEXT NOT NULL,
  generator TEXT DEFAULT '',
  dataset_id TEXT DEFAULT '',
  seed INTEGER,
  version TEXT DEFAULT '1.0',
  parameters TEXT DEFAULT '{}',
  status TEXT DEFAULT 'proposed',
  created_by TEXT DEFAULT '',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS datasets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'synthetic',
  source TEXT DEFAULT '',
  privacy_epsilon REAL,
  rows INTEGER NOT NULL DEFAULT 0,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS validation_reports (
  id TEXT PRIMARY KEY,
  model_id TEXT DEFAULT '',
  name TEXT DEFAULT '',
  metrics TEXT DEFAULT '{}',
  findings TEXT DEFAULT '[]',
  status TEXT DEFAULT 'pending',
  evidence TEXT DEFAULT '[]',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  model_id TEXT DEFAULT '',
  action TEXT NOT NULL,
  rationale TEXT DEFAULT '',
  requested_by TEXT DEFAULT '',
  requested_role TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT DEFAULT '',
  decided_role TEXT DEFAULT '',
  decided_at REAL,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS challenges (
  id TEXT PRIMARY KEY,
  model_id TEXT DEFAULT '',
  finding TEXT NOT NULL,
  severity TEXT DEFAULT 'medium',
  disposition TEXT DEFAULT 'open',
  logged_by TEXT DEFAULT '',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  model_id TEXT DEFAULT '',
  kind TEXT DEFAULT 'report',
  description TEXT DEFAULT '',
  path TEXT DEFAULT '',
  created_by TEXT DEFAULT '',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT DEFAULT '',
  role TEXT DEFAULT '',
  purpose TEXT DEFAULT '',
  tool TEXT NOT NULL,
  model_id TEXT DEFAULT '',
  params_hash TEXT DEFAULT '',
  result_summary TEXT DEFAULT '',
  result_status TEXT NOT NULL DEFAULT 'ok'
);
"""

_conns: dict[str, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


def store_root() -> Path:
    root = Path(os.environ.get(MRM_STORE_ENV, "~/.fox/mrm")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return store_root() / "mrm.db"


def _conn() -> sqlite3.Connection:
    path = str(db_path())
    with _conn_lock:
        if path not in _conns:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _conns[path] = conn
        return _conns[path]


def close_conn() -> None:
    """Close all cached connections (tests rotate FOX_MRM_STORE)."""
    with _conn_lock:
        for c in _conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        _conns.clear()


def reset_store() -> None:
    """Drop all rows (used by tests between scenarios)."""
    conn = _conn()
    for t in ("audit_log", "evidence", "challenges", "approvals",
              "validation_reports", "datasets", "simulations", "models"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


def _hash(params: dict) -> str:
    blob = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- models CRUD --

def list_models(status: str = "", tier: int | None = None,
                category: str = "") -> list[dict]:
    q = "SELECT * FROM models"
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if tier:
        clauses.append("tier = ?")
        args.append(int(tier))
    if category:
        clauses.append("category = ?")
        args.append(category)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY tier, name"
    return [dict(r) for r in _conn().execute(q, args).fetchall()]


def get_model(model_id: str) -> dict | None:
    return _row(_conn().execute(
        "SELECT * FROM models WHERE id = ?", (model_id,)).fetchone())


def require_model(model_id: str) -> dict:
    m = get_model(model_id)
    if m is None:
        raise ValueError(f"unknown model '{model_id}' — register it first")
    return m


def register_model(name: str, category: str, tier: int = 3, status: str = "development",
                   description: str = "", owner: str = "1st-line",
                   validator: str = "", synthetic_used: bool = False,
                   use_limitations: str = "", assumptions: list | None = None) -> dict:
    if status not in MODEL_STATUSES:
        raise ValueError(f"invalid status '{status}' — must be one of {list(MODEL_STATUSES)}")
    if tier not in (1, 2, 3):
        raise ValueError("tier must be 1, 2 or 3")
    m = {
        "id": _new_id("mdl"),
        "name": name, "category": category, "tier": int(tier), "status": status,
        "description": description, "owner": owner, "validator": validator,
        "synthetic_used": 1 if synthetic_used else 0,
        "tstr_completed": 0, "use_limitations": use_limitations,
        "assumptions": json.dumps(assumptions or [], default=str),
        "created_at": _now(), "updated_at": _now(),
    }
    _conn().execute(
        "INSERT INTO models (id, name, category, tier, status, description, owner, "
        "validator, synthetic_used, tstr_completed, use_limitations, assumptions, "
        "created_at, updated_at) VALUES (:id,:name,:category,:tier,:status,:description,"
        ":owner,:validator,:synthetic_used,:tstr_completed,:use_limitations,"
        ":assumptions,:created_at,:updated_at)", m)
    _conn().commit()
    return get_model(m["id"])


def update_model(model_id: str, **fields) -> dict:
    require_model(model_id)
    allowed = {"name", "category", "tier", "status", "description", "owner",
               "validator", "synthetic_used", "tstr_completed", "use_limitations",
               "assumptions"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("tier",) and v not in (1, 2, 3):
            raise ValueError("tier must be 1, 2 or 3")
        if k == "status" and v not in MODEL_STATUSES:
            raise ValueError(f"invalid status '{v}'")
        if k in ("synthetic_used", "tstr_completed"):
            v = 1 if v else 0
        if k == "assumptions":
            v = json.dumps(v if v is not None else [], default=str)
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return get_model(model_id)
    args.append(model_id)
    _conn().execute(
        f"UPDATE models SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
        args[:-1] + [_now(), model_id])
    _conn().commit()
    return get_model(model_id)


# ----------------------------------------------------------- simulations CRUD --

def register_simulation(model_id: str = "", name: str = "", generator: str = "",
                        dataset_id: str = "", seed: int | None = None,
                        version: str = "1.0", parameters: dict | None = None,
                        status: str = "proposed", created_by: str = "") -> dict:
    s = {
        "id": _new_id("sim"),
        "model_id": model_id, "name": name or "untitled simulation",
        "generator": generator, "dataset_id": dataset_id, "seed": seed,
        "version": version, "parameters": json.dumps(parameters or {}, default=str),
        "status": status, "created_by": created_by, "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO simulations (id, model_id, name, generator, dataset_id, seed, "
        "version, parameters, status, created_by, created_at) VALUES "
        "(:id,:model_id,:name,:generator,:dataset_id,:seed,:version,:parameters,"
        ":status,:created_by,:created_at)", s)
    _conn().commit()
    return dict(s)


def list_simulations(model_id: str = "") -> list[dict]:
    if model_id:
        rows = _conn().execute(
            "SELECT * FROM simulations WHERE model_id = ? ORDER BY created_at",
            (model_id,)).fetchall()
    else:
        rows = _conn().execute("SELECT * FROM simulations ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parameters"] = json.loads(d.get("parameters") or "{}")
        out.append(d)
    return out


# ------------------------------------------------------------- datasets CRUD --

def register_dataset(name: str, path: str = "", kind: str = "synthetic",
                     source: str = "", privacy_epsilon: float | None = None,
                     rows: int = 0) -> dict:
    if kind not in ("real", "synthetic"):
        raise ValueError("kind must be 'real' or 'synthetic'")
    d = {
        "id": _new_id("ds"),
        "name": name, "path": path, "kind": kind, "source": source,
        "privacy_epsilon": privacy_epsilon, "rows": int(rows),
        "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO datasets (id, name, path, kind, source, privacy_epsilon, rows, "
        "created_at) VALUES (:id,:name,:path,:kind,:source,:privacy_epsilon,:rows,"
        ":created_at)", d)
    _conn().commit()
    return dict(d)


def list_datasets(kind: str = "") -> list[dict]:
    if kind:
        rows = _conn().execute(
            "SELECT * FROM datasets WHERE kind = ? ORDER BY created_at", (kind,)).fetchall()
    else:
        rows = _conn().execute("SELECT * FROM datasets ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ approvals (maker) --

def request_approval(model_id: str, action: str, rationale: str = "",
                     requested_by: str = "agent", requested_role: str = "developer") -> dict:
    if action not in APPROVAL_GATED_ACTIONS:
        raise ValueError(
            f"action '{action}' is not approval-gated; must be one of "
            f"{list(APPROVAL_GATED_ACTIONS)}")
    a = {
        "id": _new_id("apv"),
        "model_id": model_id, "action": action, "rationale": rationale,
        "requested_by": requested_by, "requested_role": requested_role,
        "status": "pending", "decided_by": "", "decided_role": "",
        "decided_at": None, "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO approvals (id, model_id, action, rationale, requested_by, "
        "requested_role, status, decided_by, decided_role, decided_at, created_at) "
        "VALUES (:id,:model_id,:action,:rationale,:requested_by,:requested_role,"
        ":status,:decided_by,:decided_role,:decided_at,:created_at)", a)
    _conn().commit()
    return dict(a)


def list_approvals(model_id: str = "", status: str = "") -> list[dict]:
    q = "SELECT * FROM approvals"
    clauses, args = [], []
    if model_id:
        clauses.append("model_id = ?")
        args.append(model_id)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in _conn().execute(q, args).fetchall()]


def latest_approved(model_id: str, action: str) -> dict | None:
    rows = _conn().execute(
        "SELECT * FROM approvals WHERE model_id = ? AND action = ? AND status = 'approved' "
        "ORDER BY decided_at DESC LIMIT 1", (model_id, action)).fetchall()
    return dict(rows[0]) if rows else None


def decide_approval(approval_id: str, decision: str, decided_by: str = "validator",
                    decided_role: str = "validator") -> dict:
    """Checker half of maker-checker. Only 2nd/3rd-line roles may decide."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    require_role(decided_role, _CHECKER_ROLES, "decide on approval requests")
    row = _row(_conn().execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
    if row is None:
        raise ValueError(f"unknown approval '{approval_id}'")
    if row["status"] != "pending":
        raise ValueError(f"approval already '{row['status']}'")
    new_status = "approved" if decision == "approve" else "rejected"
    _conn().execute(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_role = ?, "
        "decided_at = ? WHERE id = ?",
        (new_status, decided_by, decided_role, _now(), approval_id))
    _conn().commit()
    return dict(_conn().execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())


def _ensure_approved(model_id: str, action: str, actor_role: str) -> None:
    """A tier change/retirement needs a validator/admin actor OR a fresh approved
    approval request on record (maker-checker)."""
    if actor_role in _TIER_ROLES:
        return
    if latest_approved(model_id, action) is None:
        raise PermissionError_(
            f"{action} on '{model_id}' is approval-gated — first call "
            f"request_approval(model_id='{model_id}', action='{action}', …) and have "
            "a validator/admin decide it")


# ------------------------------------------------------- effective challenge --

def log_challenge(model_id: str, finding: str, severity: str = "medium",
                  disposition: str = "open", logged_by: str = "validator") -> dict:
    if severity not in ("low", "medium", "high", "critical"):
        raise ValueError("severity must be one of low/medium/high/critical")
    if disposition not in ("open", "accepted", "mitigated", "rejected"):
        raise ValueError("disposition must be one of open/accepted/mitigated/rejected")
    c = {
        "id": _new_id("chg"),
        "model_id": model_id, "finding": finding, "severity": severity,
        "disposition": disposition, "logged_by": logged_by, "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO challenges (id, model_id, finding, severity, disposition, "
        "logged_by, created_at) VALUES (:id,:model_id,:finding,:severity,:disposition,"
        ":logged_by,:created_at)", c)
    _conn().commit()
    return dict(c)


def list_challenges(model_id: str = "") -> list[dict]:
    if model_id:
        rows = _conn().execute(
            "SELECT * FROM challenges WHERE model_id = ? ORDER BY created_at DESC",
            (model_id,)).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM challenges ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ evidence --

def attach_evidence(model_id: str, kind: str = "report", description: str = "",
                    path: str = "", created_by: str = "agent") -> dict:
    e = {
        "id": _new_id("evd"),
        "model_id": model_id, "kind": kind, "description": description,
        "path": path, "created_by": created_by, "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO evidence (id, model_id, kind, description, path, created_by, "
        "created_at) VALUES (:id,:model_id,:kind,:description,:path,:created_by,"
        ":created_at)", e)
    _conn().commit()
    return dict(e)


def list_evidence(model_id: str = "") -> list[dict]:
    if model_id:
        rows = _conn().execute(
            "SELECT * FROM evidence WHERE model_id = ? ORDER BY created_at DESC",
            (model_id,)).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM evidence ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- validation reports --

def save_validation_report(model_id: str, name: str, metrics: dict,
                           findings: list, status: str = "passed",
                           evidence: list | None = None) -> dict:
    r = {
        "id": _new_id("vrep"),
        "model_id": model_id, "name": name,
        "metrics": json.dumps(metrics, default=str),
        "findings": json.dumps(findings, default=str),
        "status": status, "evidence": json.dumps(evidence or [], default=str),
        "created_at": _now(),
    }
    _conn().execute(
        "INSERT INTO validation_reports (id, model_id, name, metrics, findings, "
        "status, evidence, created_at) VALUES (:id,:model_id,:name,:metrics,:findings,"
        ":status,:evidence,:created_at)", r)
    _conn().commit()
    return dict(r)


def list_validation_reports(model_id: str = "") -> list[dict]:
    if model_id:
        rows = _conn().execute(
            "SELECT * FROM validation_reports WHERE model_id = ? ORDER BY created_at "
            "DESC", (model_id,)).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM validation_reports ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d.get("metrics") or "{}")
        d["findings"] = json.loads(d.get("findings") or "[]")
        out.append(d)
    return out


# ------------------------------------------------------------- audit log (RO) --

def audit(actor: str, role: str, purpose: str, tool: str,
          params_hash: str = "", result_summary: str = "",
          result_status: str = "ok", model_id: str = "") -> None:
    """Append one immutable audit event. INSERT-only by construction."""
    _conn().execute(
        "INSERT INTO audit_log (ts, actor, role, purpose, tool, model_id, "
        "params_hash, result_summary, result_status) VALUES (?,?,?,?,?,?,?,?,?)",
        (_now(), actor, role, purpose, tool, model_id, params_hash,
         result_summary, result_status))
    _conn().commit()


def audit_log(limit: int = 100, model_id: str = "", tool: str = "",
              actor: str = "") -> list[dict]:
    q = "SELECT * FROM audit_log"
    clauses, args = [], []
    if model_id:
        clauses.append("model_id = ?")
        args.append(model_id)
    if tool:
        clauses.append("tool = ?")
        args.append(tool)
    if actor:
        clauses.append("actor = ?")
        args.append(actor)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in _conn().execute(q, args).fetchall()]
