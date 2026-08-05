from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .arxiv_fetcher import search_arxiv
from .similarity import jaccard_tokens

logger = logging.getLogger(__name__)

CACHE_TTL = 12 * 3600
REFRESH_INTERVAL = 12 * 3600
MAX_PER_TOPIC = 10

DEFAULT_TOPICS = [
    {"name": "Knowledge graphs", "query": "knowledge graph embedding"},
    {"name": "Federated learning", "query": "federated learning"},
    {"name": "AI security", "query": "AI security adversarial machine learning"},
    {"name": "LLM security", "query": "large language model security"},
    {"name": "AI alignment", "query": "AI alignment"},
    {"name": "Adversarial ML", "query": "adversarial machine learning"},
    {"name": "Graph neural networks", "query": "graph neural network"},
    {"name": "Vision-language models", "query": "vision language model"},
]

TOPIC_COLORS = [
    "#60a5fa", "#34d399", "#fbbf24", "#f87171",
    "#c084fc", "#22d3ee", "#fb923c", "#a78bfa",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    query TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS papers (
    arxiv_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    authors TEXT NOT NULL DEFAULT '[]',
    authors_str TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    abstract TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '[]',
    pdf_url TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    imported INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_papers_last_seen ON papers(last_seen);
CREATE INDEX IF NOT EXISTS idx_papers_imported ON papers(imported);

CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    timestamp REAL NOT NULL
);
"""


class ResearchPool:
    def __init__(self, store_dir: str | Path) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

        self._db_path = str(self.store_dir / "pool.db")
        self._local = threading.local()
        self._init_db()

        self._lock = threading.Lock()

        if not self._has_topics():
            self._seed_default_topics()

        self._bg_thread = threading.Thread(target=self._bg_loop, daemon=True)
        self._bg_thread.start()

    @property
    def _db(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _has_topics(self) -> bool:
        row = self._db.execute("SELECT COUNT(*) AS cnt FROM topics").fetchone()
        return row["cnt"] > 0

    def _seed_default_topics(self) -> None:
        now = datetime.utcnow().isoformat()
        for t in DEFAULT_TOPICS:
            self._db.execute(
                "INSERT OR IGNORE INTO topics (name, query, created_at) VALUES (?, ?, ?)",
                (t["name"], t["query"], now),
            )
        self._db.commit()

    def get_topics(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT name, query FROM topics ORDER BY id"
        ).fetchall()
        return [{"name": r["name"], "query": r["query"]} for r in rows]

    def add_topic(self, name: str, query: str, **kwargs: Any) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM topics WHERE name = ?", (name,)
            )
            self._db.execute(
                "INSERT INTO topics (name, query) VALUES (?, ?)",
                (name, query),
            )
            self._db.commit()

    def remove_topic(self, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM topics WHERE name = ?", (name,))
            self._db.commit()

    def get(self) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT value, timestamp FROM cache WHERE key = 'feed'"
        ).fetchone()
        age = time.time() - (row["timestamp"] if row else 0) if row else float("inf")
        if row and age < CACHE_TTL:
            return json.loads(row["value"])
        if age > CACHE_TTL:
            self._bg_refresh()
        return json.loads(row["value"]) if row else {}

    def refresh(self) -> dict[str, Any]:
        return self._do_refresh()

    def _bg_refresh(self) -> None:
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self) -> dict[str, Any]:
        try:
            data = self._fetch_all()
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, value, timestamp) VALUES ('feed', ?, ?)",
                (json.dumps(data), time.time()),
            )
            self._db.commit()
            return data
        except Exception as e:
            logger.error("Pool refresh failed: %s", e)
            row = self._db.execute(
                "SELECT value FROM cache WHERE key = 'feed'"
            ).fetchone()
            return json.loads(row["value"]) if row else {}

    def _fetch_all(self) -> dict[str, Any]:
        topics = self.get_topics()
        result: dict[str, Any] = {}
        for i, topic in enumerate(topics):
            name = topic.get("name", "untitled")
            query = topic.get("query", "")
            if i > 0:
                time.sleep(4)
            try:
                papers = search_arxiv(query, max_results=MAX_PER_TOPIC)
                entries = []
                for p in papers:
                    entry = {
                        "arxiv_id": p.arxiv_id,
                        "title": p.title,
                        "authors": p.authors,
                        "authors_str": p.authors_str,
                        "published": p.published,
                        "abstract": p.abstract[:500],
                        "categories": p.categories,
                        "pdf_url": p.pdf_url,
                    }
                    entries.append(entry)
                    self._observe(entry, name)
                result[name] = entries
                logger.info("Pool topic '%s': %d papers", name, len(entries))
            except Exception as e:
                logger.warning("Pool topic '%s' fetch failed: %s", name, e)
                result[name] = []
        return result

    def _bg_loop(self) -> None:
        time.sleep(30)
        self._bg_refresh()
        while True:
            time.sleep(REFRESH_INTERVAL)
            self._bg_refresh()

    def _observe(self, entry: dict[str, Any], topic: str) -> None:
        aid = entry["arxiv_id"]
        now = datetime.utcnow().isoformat()
        with self._lock:
            row = self._db.execute(
                "SELECT topics, imported FROM papers WHERE arxiv_id = ?", (aid,)
            ).fetchone()
            if row:
                topics = json.loads(row["topics"])
                if topic not in topics:
                    topics.append(topic)
                self._db.execute(
                    "UPDATE papers SET topics = ?, last_seen = ? WHERE arxiv_id = ?",
                    (json.dumps(topics), now, aid),
                )
            else:
                self._db.execute(
                    "INSERT INTO papers (arxiv_id, title, authors, authors_str, "
                    "published, abstract, categories, pdf_url, topics, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        aid,
                        entry.get("title", ""),
                        json.dumps(entry.get("authors", [])),
                        entry.get("authors_str", ""),
                        entry.get("published", ""),
                        entry.get("abstract", "")[:500],
                        json.dumps(entry.get("categories", [])),
                        entry.get("pdf_url", ""),
                        json.dumps([topic]),
                        now,
                        now,
                    ),
                )
            self._db.commit()

    def get_observed_papers(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM papers ORDER BY last_seen DESC"
        ).fetchall()
        now_ts = time.time()
        papers = []
        for r in rows:
            p = dict(r)
            p["authors"] = json.loads(p.get("authors", "[]"))
            p["categories"] = json.loads(p.get("categories", "[]"))
            p["topics"] = json.loads(p.get("topics", "[]"))
            p["tags"] = json.loads(p.get("tags", "[]"))
            p["imported"] = bool(p["imported"])
            fs = p.get("first_seen")
            try:
                p["is_new"] = fs and (now_ts - datetime.fromisoformat(fs).timestamp()) < 86400
            except Exception:
                p["is_new"] = False
            papers.append(p)
        return papers

    def mark_imported(self, arxiv_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE papers SET imported = 1, imported_at = ? WHERE arxiv_id = ?",
                (datetime.utcnow().isoformat(), arxiv_id),
            )
            self._db.commit()

    def update_tags(self, arxiv_id: str, tags: list[str]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE papers SET tags = ? WHERE arxiv_id = ?",
                (json.dumps(tags), arxiv_id),
            )
            self._db.commit()

    def get_pool_graph(self) -> dict[str, Any]:
        papers = self.get_observed_papers()
        nodes = []
        for p in papers:
            nodes.append({
                "id": p["arxiv_id"],
                "label": p.get("title", "")[:60],
                "type": "paper",
                "title": p.get("title", ""),
                "abstract": (p.get("abstract", "") or "")[:300],
                "imported": p.get("imported", False),
                "is_new": p.get("is_new", False),
                "topics": p.get("topics", []),
            })
        edges = []
        n = len(nodes)
        for i in range(n):
            for j in range(i + 1, n):
                ti = (nodes[i]["title"] or "") + " " + (nodes[i]["abstract"] or "")
                tj = (nodes[j]["title"] or "") + " " + (nodes[j]["abstract"] or "")
                score = jaccard_tokens(ti, tj)
                if score >= 0.12:
                    edges.append({
                        "source": nodes[i]["id"],
                        "target": nodes[j]["id"],
                        "similarity": round(score, 4),
                    })
        return {"nodes": nodes, "edges": edges}
