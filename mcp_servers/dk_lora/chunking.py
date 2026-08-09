"""Chunking for dk-lora: recursive / fixed / semantic, with provenance.

Every chunk retains ``artifact_id``, ``source_path``, ``index``, an optional
``page`` (from the PDF ``[page N]`` markers) and an optional ``speaker`` (from
diarized transcripts). Chunks are stored as ``<artifact_id>--<n>.json`` files so
they can be looked up by artifact without scanning the whole catalog.
"""

from __future__ import annotations

import re
from typing import Sequence

from .models import Artifact, Chunk
from .store import Workspace

_PAGE_RE = re.compile(r"^\[page (\d+)\]\s*", re.M)

# Boundary separators ordered best -> worst for recursive splitting.
_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ", ""]


def _recursive_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """LangChain-style recursive character splitter."""
    if not text:
        return []
    chunks: list[str] = []

    def split_join(s: str, seps: list[str]) -> list[str]:
        sep = seps[0] if seps else ""
        if not sep:
            return [s] if s else []
        parts = s.split(sep)
        groups: list[str] = []
        cur = ""
        for part in parts:
            candidate = (cur + sep + part).strip() if cur else part.strip()
            if len(candidate) <= chunk_size or not cur:
                cur = candidate
            else:
                if cur:
                    groups.append(cur)
                cur = part.strip()
        if cur:
            groups.append(cur)
        return groups

    seps = list(_SEPARATORS)
    while seps:
        result: list[str] = []
        for piece in text.split(seps[0] * 2 if seps[0] else "\n\n") if seps[0] else [text]:
            # Merge too-short pieces, split too-long ones.
            if len(piece) <= chunk_size:
                result.append(piece)
            else:
                result.extend(split_join(piece, seps[1:] or [""]))
        if all(len(p) <= chunk_size for p in result if p):
            break
        seps = seps[1:]
    # overlap: slide a window over the boundary between neighbours
    pieces = [p for p in result if p]
    out: list[str] = []
    for i, piece in enumerate(pieces):
        out.append(piece)
        if overlap and i < len(pieces) - 1 and len(piece) > overlap:
            out.append(piece[-overlap:] + pieces[i + 1][:overlap] + "\n[continuation]")
    return out


def _fixed_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Fixed-size windows with overlap."""
    if not text:
        return []
    step = max(1, chunk_size - overlap)
    return [text[i:i + chunk_size] for i in range(0, len(text), step) if text[i:i + chunk_size].strip()]


def _embed_model():
    """Lazily loaded sentence-transformers model, or None (offline fallback)."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        return SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:  # noqa: BLE001
        return None


def _semantic_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Semantic chunking: merge sentences by embedding similarity.

    Falls back to recursive splitting when sentence-transformers (or numpy) is
    unavailable, so the pipeline always works offline.
    """
    model = _embed_model()
    if model is None:
        return _recursive_split(text, chunk_size, overlap)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) <= 1:
        return _recursive_split(text, chunk_size, overlap)
    try:
        import numpy as np
        vecs = np.asarray(model.encode(sentences, show_progress_bar=False))
    except Exception:  # noqa: BLE001
        return _recursive_split(text, chunk_size, overlap)

    chunks: list[str] = []
    cur: list[str] = [sentences[0]]
    cur_vec = vecs[0]
    for sent, vec in zip(sentences[1:], vecs[1:]):
        sim = float(np.dot(cur_vec, vec) / (np.linalg.norm(cur_vec) * np.linalg.norm(vec) + 1e-12))
        if sim >= 0.5 and sum(len(s) for s in cur) + len(sent) <= chunk_size:
            cur.append(sent)
            cur_vec += vec
        else:
            chunks.append(" ".join(cur))
            cur = [sent]
            cur_vec = vec
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def chunk_artifact(art: Artifact, strategy: str, chunk_size: int,
                   overlap: int) -> list[str]:
    """Chunk one artifact's text (strategy: recursive/fixed/semantic)."""
    text = art.text
    if strategy == "semantic":
        pieces = _semantic_split(text, chunk_size, overlap)
    elif strategy == "fixed":
        pieces = _fixed_split(text, chunk_size, overlap)
    else:
        pieces = _recursive_split(text, chunk_size, overlap)
    return [p for p in pieces if p.strip()]


def _page_of(text: str) -> int | None:
    m = _PAGE_RE.search(text)
    return int(m.group(1)) if m else None


def _speaker_of(text: str, speakers: Sequence[str]) -> str | None:
    for s in speakers:
        if s and f"**{s}:**" in text:
            return s
    return None


def chunk_artifacts(ws: Workspace, artifact_ids: list[str] | None = None,
                    strategy: str = "recursive", chunk_size: int = 512,
                    overlap: int = 64) -> dict:
    """Chunk selected artifacts (default: all) and store the results."""
    if strategy not in ("semantic", "recursive", "fixed"):
        raise ValueError(f"unknown strategy '{strategy}' (expected one of "
                         "semantic/recursive/fixed)")
    if chunk_size < 64 or chunk_size > 4096:
        raise ValueError(f"chunk_size must be in 64..4096 (got {chunk_size})")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(f"overlap must be in 0..{chunk_size - 1} (got {overlap})")

    if artifact_ids:
        arts = [ws.get_artifact(a) for a in artifact_ids]
        arts = [a for a in arts if a is not None]
    else:
        arts = [ws.get_artifact(e["id"]) for e in ws.list_entries("artifacts")]
        arts = [a for a in arts if a is not None]
    if not arts:
        raise ValueError("no artifacts to chunk — run ingest_artifacts first")

    total = 0
    per_artifact: dict[str, int] = {}
    for art in arts:
        speakers = art.metadata.get("speakers") or []
        pieces = chunk_artifact(art, strategy, chunk_size, overlap)
        chunks: list[Chunk] = []
        for i, piece in enumerate(pieces):
            chunks.append(Chunk(
                id=f"{art.id}--{i:04d}",
                artifact_id=art.id,
                index=i,
                text=piece,
                source_path=art.path,
                page=_page_of(piece),
                speaker=_speaker_of(piece, speakers),
                strategy=strategy,
            ))
        if chunks:
            ws.add_chunks(chunks)
            total += len(chunks)
            per_artifact[art.id] = len(chunks)
    return {"chunked_artifacts": per_artifact, "total_chunks": total,
            "strategy": strategy, "chunk_size": chunk_size, "overlap": overlap}


def list_chunks(ws: Workspace, artifact_ids: list[str] | None = None) -> dict:
    chunks = ws.list_chunks(artifact_ids)
    return {"chunks": [c.model_dump() for c in chunks], "count": len(chunks)}
