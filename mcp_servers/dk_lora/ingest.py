"""Ingest & normalize local artifacts for dk-lora.

Supported types: PDF (pymupdf preferred, docling optional, pdftotext fallback),
Markdown, plain text, JSON/JSONL, CSV, and diarized transcripts (speaker
labels + optional timestamps are preserved as metadata so downstream tools can
cite "who said what").

Path safety: every scanned path is resolved and must stay inside the requested
root — traversal attempts raise ``ValueError``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .models import Artifact
from .store import Workspace

SUPPORTED_TYPES = {".pdf", ".md", ".txt", ".json", ".jsonl", ".csv", ".rst"}

# Diarized transcript line: "Speaker 1: the text ..." (optional [hh:mm:ss] prefix)
_SPEAKER_LINE = re.compile(
    r"^\s*(?:\[?[\d:]{1,8}\]?[:\-]?\s+)?"
    r"(?P<speaker>[A-Za-z][A-Za-z0-9 _\-]{0,39}?)\s*:\s*(?P<text>.+)$"
)


def _slug(name: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in name.lower())
    return keep.strip("_")[:40] or "artifact"


def artifact_id_for(path: Path, root: Path) -> str:
    rel = str(path.relative_to(root)) if root else path.name
    return hashlib.sha1(rel.encode()).hexdigest()[:12]


def _resolve_within(root: Path, path: Path) -> Path:
    """Resolve *path* under *root*, rejecting any traversal."""
    root = root.resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes the requested root: {path}")
    return candidate


def _safe_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"path does not exist: {root}")
    return root


def _extract_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from a PDF: pymupdf -> docling -> pdftotext."""
    text = ""
    meta: dict[str, Any] = {"pages": 0}
    try:  # pymupdf
        import fitz  # type: ignore
        doc = fitz.open(path)
        meta["pages"] = doc.page_count
        pages: list[str] = []
        for i, page in enumerate(doc):
            t = page.get_text("text") or ""
            if t.strip():
                pages.append(f"\n\n[page {i + 1}]\n{t}")
        text = "\n".join(pages)
        doc.close()
    except Exception:  # noqa: BLE001
        text = ""
    if not text.strip():
        try:  # docling (optional)
            from docling.document_converter import DocumentConverter  # type: ignore
            conv = DocumentConverter()
            res = conv.convert(str(path))
            text = res.document.export_to_markdown()
            meta["parser"] = "docling"
        except Exception:  # noqa: BLE001
            text = ""
    if not text.strip():
        try:  # poppler-utils fallback
            import subprocess
            out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                text = out.stdout
                meta["parser"] = "pdftotext"
        except Exception:  # noqa: BLE001
            text = ""
    if not text.strip():
        raise ValueError(
            f"could not extract text from {path.name} — install pymupdf "
            "(pip install pymupdf), docling, or poppler-utils (pdftotext)")
    return text, meta


def _extract_csv(path: Path) -> tuple[str, dict[str, Any]]:
    rows: list[list[str]] = []
    with io.open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            rows.append(row)
            if len(rows) >= 5000:
                break
    meta = {"rows": len(rows), "columns": rows[0] if rows else []}
    # Render as a markdown-ish table so chunks keep structure.
    lines = []
    for row in rows:
        lines.append(" | ".join(c.strip() for c in row))
    return "\n".join(lines), meta


def _extract_json(path: Path) -> tuple[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    meta = {"kind": "json", "top_level_keys": list(data) if isinstance(data, dict) else None,
            "records": len(data) if isinstance(data, list) else 1}
    text = json.dumps(data, ensure_ascii=False, indent=2)[:2_000_000]
    return text, meta


def _extract_plain(path: Path) -> tuple[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text, {"kind": "text"}


def _diarize(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Detect speaker turns in a transcript.

    Returns (markdown-ish transcript, turns) where turns keep speaker + first
    text so chunks can carry a speaker label.
    """
    turns: list[dict[str, Any]] = []
    speaker = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SPEAKER_LINE.match(line)
        if m:
            speaker = m.group("speaker").strip()
            body = m.group("text").strip()
        else:
            body = line
        turns.append({"speaker": speaker, "text": body})
    md = "\n\n".join(f"**{t['speaker'] or '?'}:** {t['text']}" for t in turns)
    return md, turns


def _looks_diarized(text: str) -> bool:
    hits = sum(1 for line in text.splitlines()[:200]
               if _SPEAKER_LINE.match(line))
    return hits >= 3


def extract_artifact(path: Path, root: Path) -> Artifact:
    """Extract text + metadata from one file into an Artifact (no writes)."""
    ext = path.suffix.lower()
    if ext not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported file type '{ext}' (supported: "
                         f"{sorted(SUPPORTED_TYPES)})")
    meta: dict[str, Any] = {}
    if ext == ".pdf":
        text, meta = _extract_pdf(path)
    elif ext == ".csv":
        text, meta = _extract_csv(path)
    elif ext in (".json", ".jsonl"):
        text, meta = _extract_json(path)
    else:
        text, meta = _extract_plain(path)

    diarized = False
    if ext in (".txt", ".md") and _looks_diarized(text):
        try:
            text, turns = _diarize(text)
            meta["diarized"] = True
            meta["speakers"] = sorted({t["speaker"] for t in turns if t["speaker"]})
            meta["turn_count"] = len(turns)
            diarized = True
        except Exception:  # noqa: BLE001
            diarized = False

    stat = path.stat()
    title = path.stem[:120]
    return Artifact(
        id=artifact_id_for(path, root),
        path=str(path.resolve()),
        file_type=ext.lstrip("."),
        title=title,
        size_bytes=stat.st_size,
        created_at=time.time(),
        metadata={"diarized": diarized, "filename": path.name, **meta},
        text=text,
    )


def ingest_artifacts(ws: Workspace, path: str, recursive: bool = True) -> dict:
    """Scan *path* (file or dir) and register supported artifacts."""
    root = _safe_root(path)
    if root.is_file():
        candidates = [root]
        root = root.parent
    else:
        candidates = sorted(root.rglob("*")) if recursive else sorted(root.glob("*"))

    ingested: list[dict] = []
    skipped: list[dict] = []
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            safe = _resolve_within(root, cand)
        except ValueError as e:
            skipped.append({"path": str(cand), "reason": str(e)})
            continue
        if safe.suffix.lower() not in SUPPORTED_TYPES:
            continue
        try:
            art = extract_artifact(safe, root)
        except Exception as e:  # noqa: BLE001
            skipped.append({"path": str(cand), "reason": f"{type(e).__name__}: {e}"})
            continue
        ws.add_artifact(art)
        ingested.append({"id": art.id, "title": art.title, "file_type": art.file_type,
                         "chars": len(art.text), "path": art.path})
    return {"ingested": ingested, "skipped": skipped,
            "total_ingested": len(ingested), "total_skipped": len(skipped)}


def list_artifacts(ws: Workspace, filter_: str = "") -> dict:
    arts = ws.list_artifacts(filter_)
    return {"artifacts": arts, "count": len(arts)}


def get_artifact_metadata(ws: Workspace, artifact_id: str) -> dict:
    art = ws.get_artifact(artifact_id)
    if art is None:
        raise ValueError(f"artifact not found: {artifact_id} — run list_artifacts "
                         "for valid ids (or ingest_artifacts first)")
    return {"id": art.id, "title": art.title, "path": art.path,
            "file_type": art.file_type, "size_bytes": art.size_bytes,
            "created_at": art.created_at, "chars": len(art.text),
            "metadata": art.metadata,
            "preview": art.text[:1000]}
