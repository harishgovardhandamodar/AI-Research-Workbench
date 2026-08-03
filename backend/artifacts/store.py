"""Artifact model + SQLite/filesystem provenance store.

Every generated figure/table/structure is an Artifact carrying its producing code,
an environment snapshot, and a link to the conversation message that led to it.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

ARTIFACT_KINDS = {"figure", "table", "structure", "text", "notebook", "data"}


class Artifact:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4().hex)
        self.kind = kw.get("kind", "text")
        self.name = kw.get("name", "artifact")
        self.description = kw.get("description", "")
        self.code = kw.get("code", "")
        self.env = kw.get("env", {})
        self.message_id = kw.get("message_id", "")
        self.run_id = kw.get("run_id", "")
        self.created_at = kw.get("created_at", time.time())
        self.data_path = kw.get("data_path", "")
        self.data_type = kw.get("data_type", "text")
        self.size = kw.get("size", 0)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name,
            "description": self.description, "code": self.code,
            "env": self.env, "message_id": self.message_id,
            "run_id": self.run_id,
            "created_at": self.created_at, "data_path": self.data_path,
            "data_type": self.data_type, "size": self.size,
        }

    @property
    def url(self) -> str | None:
        if self.data_path and self.data_type in ("png", "svg", "html"):
            return f"/artifacts/{self.id}"
        return None


class ArtifactStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.artifacts_dir = project_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = project_dir / "workbench.db"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT, name TEXT, description TEXT, code TEXT,
                env TEXT, message_id TEXT, run_id TEXT, created_at REAL,
                data_path TEXT, data_type TEXT, size INTEGER)"""
        )
        # Migration: older databases were created before run_id existed.
        try:
            self._conn.execute("ALTER TABLE artifacts ADD COLUMN run_id TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    # -- writes -------------------------------------------------------------
    def _write_bytes(self, artifact: Artifact, data: bytes, data_type: str) -> Path:
        ext = {"png": ".png", "svg": ".svg", "html": ".html", "text": ".txt"}.get(data_type, ".bin")
        path = self.artifacts_dir / f"{artifact.id}{ext}"
        path.write_bytes(data)
        artifact.data_path = str(path)
        artifact.data_type = data_type
        artifact.size = len(data)
        return path

    def add_artifact(self, artifact: Artifact, data: bytes | None = None,
                     data_type: str = "text") -> Artifact:
        if data is not None:
            self._write_bytes(artifact, data, data_type)
        self._conn.execute(
            """INSERT INTO artifacts (id, kind, name, description, code, env,
               message_id, run_id, created_at, data_path, data_type, size)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (artifact.id, artifact.kind, artifact.name, artifact.description,
             artifact.code, json.dumps(artifact.env), artifact.message_id,
             artifact.run_id, artifact.created_at, artifact.data_path,
             artifact.data_type, artifact.size),
        )
        self._conn.commit()
        return artifact

    def link_artifacts(self, artifact_ids: list, message_id: str = "",
                       run_id: str = "") -> int:
        """Attach conversation provenance to artifacts after a turn completes."""
        if not artifact_ids:
            return 0
        rows = self._conn.executemany(
            "UPDATE artifacts SET message_id=COALESCE(NULLIF(message_id,''), ?),"
            " run_id=COALESCE(NULLIF(run_id,''), ?) WHERE id=?",
            [(message_id, run_id, aid) for aid in artifact_ids])
        self._conn.commit()
        return rows.rowcount

    def update_description(self, artifact_id: str, description: str):
        self._conn.execute("UPDATE artifacts SET description=? WHERE id=?",
                           (description, artifact_id))
        self._conn.commit()

    # -- reads --------------------------------------------------------------
    def list(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_artifact(r) for r in rows]

    def get(self, artifact_id: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._row_artifact(row) if row else None

    def data(self, artifact_id: str) -> bytes | None:
        art = self.get(artifact_id)
        if not art or not art.data_path:
            return None
        p = Path(art.data_path)
        return p.read_bytes() if p.exists() else None

    def delete(self, artifact_id: str) -> bool:
        art = self.get(artifact_id)
        if not art:
            return False
        if art.data_path:
            Path(art.data_path).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        self._conn.commit()
        return True

    def _row_artifact(self, row) -> Artifact:
        return Artifact(
            id=row["id"], kind=row["kind"], name=row["name"],
            description=row["description"], code=row["code"],
            env=json.loads(row["env"] or "{}"), message_id=row["message_id"],
            run_id=row["run_id"],
            created_at=row["created_at"], data_path=row["data_path"],
            data_type=row["data_type"], size=row["size"],
        )
