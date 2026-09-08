from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from hive.config import DB_FILE


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, topic TEXT, created_at REAL, updated_at REAL, summary TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, title TEXT, content TEXT, created_at REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id))"""
    )
    con.commit()
    con.close()


def new_session(topic: str) -> str:
    init_db()
    sid = str(uuid.uuid4())[:8]
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (sid, topic, time.time(), time.time(), ""))
    con.commit()
    con.close()
    return sid


def save_artifact(session_id: str, kind: str, title: str, content: str) -> str:
    init_db()
    aid = str(uuid.uuid4())[:8]
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)", (aid, session_id, kind, title, content, time.time()))
    con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
    con.commit()
    con.close()
    return aid


def list_sessions(limit: int = 20):
    init_db()
    con = sqlite3.connect(DB_FILE)
    cur = con.execute("SELECT id, topic, created_at FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    con.close()
    return rows


def list_legacy_sessions(limit: int = 20):
    """Scan EXPERIMENTS_DIR (personal-experiments) for legacy runs/artifacts."""
    try:
        from hive.config import EXPERIMENTS_DIR
    except Exception:
        EXPERIMENTS_DIR = None
    if not EXPERIMENTS_DIR or not EXPERIMENTS_DIR.exists():
        return []
    out = []
    # fox/**/runs/*.json and artifacts
    for pat in ["**/runs/*.json", "**/artifacts/*", "**/experiments.json"]:
        try:
            for f in EXPERIMENTS_DIR.glob(pat):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                    # derive topic from parent folder
                    topic = f.parent.parent.name if f.parent.name in ("runs","artifacts") else f.parent.name
                    # try read json title
                    title = f.name
                    if f.suffix == ".json":
                        try:
                            j = __import__('json').loads(f.read_text()[:1000])
                            if isinstance(j, dict):
                                title = j.get("topic") or j.get("title") or j.get("id") or title
                        except Exception:
                            pass
                    out.append((f.stem[:8], f"{topic}/{title}", st.st_mtime))
                except Exception:
                    continue
                if len(out) >= limit*3:
                    break
        except Exception:
            continue
    # dedupe, sort by mtime desc
    seen=set()
    uniq=[]
    for r in sorted(out, key=lambda x: x[2], reverse=True):
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
        if len(uniq) >= limit:
            break
    return uniq


def list_all_sessions(limit: int = 20):
    """Merge DB sessions + legacy (personal-experiments)."""
    db = list_sessions(limit=limit)
    legacy = list_legacy_sessions(limit=limit)
    # tag legacy with prefix so UI can distinguish
    # merge sorted by time
    merged = [(i,t,c) for i,t,c in db] + [(i,f"[legacy] {t}",c) for i,t,c in legacy]
    merged.sort(key=lambda x: x[2], reverse=True)
    return merged[:limit]
