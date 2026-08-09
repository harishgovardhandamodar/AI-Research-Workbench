"""Local RAG index for ft-validate.

Embeds source chunks with ``sentence-transformers`` (default ``all-MiniLM-L6-v2``)
and stores them in the workspace with numpy. When sentence-transformers (or
numpy) is unavailable it falls back to a deterministic hashing-based dense bag
of words, so the index is *always* buildable and queryable offline.

``retrieve`` returns the top-k chunks by cosine similarity with the question.
The index is built from the original artifacts/chunks — never from the training
JSONL — so verification avoids data leakage.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Sequence

import numpy as np

from .store import ValidateStore

TOKEN_RE = re.compile(r"[a-z0-9]{2,}", re.I)
HASH_DIM = 384  # fixed dimension for the hashing fallback


def _embed_model():
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        return SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:  # noqa: BLE001
        return None


def _hash_vec(text: str) -> np.ndarray:
    """Deterministic count-based hashing to a fixed-dim vector (fallback)."""
    vec = np.zeros(HASH_DIM, dtype=np.float32)
    for tok in TOKEN_RE.findall(text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % HASH_DIM] += 1.0
    n = float(np.linalg.norm(vec))
    return vec / n if n else vec


def _embed(texts: Sequence[str]) -> tuple[np.ndarray, str | None]:
    """Embed texts. Returns (matrix, embedding_model_name_or_None)."""
    model = _embed_model()
    if model is not None:
        try:
            vecs = np.asarray(model.encode(list(texts),
                                           show_progress_bar=False,
                                           normalize_embeddings=True))
            return vecs, "all-MiniLM-L6-v2"
        except Exception:  # noqa: BLE001
            pass
    vecs = np.vstack([_hash_vec(t) for t in texts]) if texts else np.zeros((0, HASH_DIM))
    return vecs, None


def _validate_chunk_input(chunks: Sequence[dict], root: str | None = None) -> list[dict]:
    """Ensure chunks carry id + text; resolve optional paths against root."""
    out: list[dict] = []
    for c in chunks:
        if not isinstance(c, dict) or not c.get("text"):
            continue
        item = {"id": str(c.get("id") or hashlib.sha1(c["text"].encode()).hexdigest()[:12]),
                "text": c["text"],
                "source": c.get("source_path") or c.get("path") or "",
                "metadata": c.get("metadata") or c.get("meta") or {}}
        out.append(item)
    if root:
        base = str(root)
        for c in out:
            if c["source"] and not c["source"].startswith(base):
                raise ValueError(f"chunk source escapes the requested root: "
                                 f"{c['source']}")
    return out


def build_rag_index(store: ValidateStore, chunks: Sequence[dict],
                    index_id: str = "", embedding_model: str = "auto",
                    root: str | None = None) -> dict:
    """Build or update a persistent RAG index from source chunks."""
    validated = _validate_chunk_input(chunks, root)
    if not validated:
        raise ValueError("no valid chunks given (each needs 'id' + 'text')")
    index_id = index_id or f"idx-{hashlib.sha1(''.join(c['id'] for c in validated).encode()).hexdigest()[:8]}"
    texts = [c["text"] for c in validated]
    vecs, model = _embed(texts)
    meta = store.save_rag_index(index_id, validated, vecs,
                                embedding_model if model is None else model)
    meta["fallback"] = model is None
    return meta


def retrieve(store: ValidateStore, index_id: str, question: str,
             top_k: int = 5) -> list[dict]:
    """Retrieve the top-k most similar chunks for *question*."""
    if not question or not question.strip():
        raise ValueError("question is required")
    if top_k < 1 or top_k > 50:
        raise ValueError("top_k must be in 1..50")
    data = store.get_rag_index(index_id)
    if data is None:
        raise ValueError(f"RAG index not found: {index_id} — run "
                         "build_rag_index first (see list_rag_indexes)")
    chunks, vectors, _meta = data
    if not chunks:
        return []
    qvec, _ = _embed([question])
    qvec = qvec[0]
    if vectors is None or vectors.shape[1] == 0:
        vectors, _ = _embed([c["text"] for c in chunks])
    sims = vectors @ qvec
    order = np.argsort(-sims)
    out = []
    for i in order[:top_k]:
        c = chunks[int(i)]
        out.append({"chunk_id": c["id"], "score": round(float(sims[i]), 4),
                    "text": c["text"][:1500], "source": c["source"],
                    "metadata": c["metadata"]})
    return out


def list_rag_indexes(store: ValidateStore) -> dict:
    return {"indexes": store.list_rag_indexes()}


def rag_stats(store: ValidateStore, index_id: str) -> dict:
    data = store.get_rag_index(index_id)
    if data is None:
        raise ValueError(f"RAG index not found: {index_id}")
    chunks, _v, meta = data
    return {"index_id": index_id, "chunk_count": len(chunks),
            "embedding_model": meta.get("embedding_model"),
            "vector_dim": meta.get("vector_dim", 0),
            "created_at": meta.get("created_at", 0.0),
            "sources": sorted({c["source"] for c in chunks if c["source"]})}
