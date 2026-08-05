from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config
from .graph import KnowledgeGraph
from .llm import LLMInterface

logger = logging.getLogger(__name__)


class Chunk:
    def __init__(
        self,
        text: str,
        source_id: str,
        source_title: str = "",
        chunk_idx: int = 0,
    ) -> None:
        self.text = text
        self.source_id = source_id
        self.source_title = source_title
        self.chunk_idx = chunk_idx


class RAGEngine:
    def __init__(
        self,
        config: Config,
        llm: LLMInterface,
        kg: KnowledgeGraph,
    ) -> None:
        self.config = config
        self.llm = llm
        self.kg = kg
        self.store_dir = Path(config.root_dir) / "rag"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.chunks: list[Chunk] = []
        self.embeddings: np.ndarray | None = None
        self._load()

    def _index_path(self) -> Path:
        return self.store_dir / "index.json"

    def _embeddings_path(self) -> Path:
        return self.store_dir / "embeddings.npy"

    def _save(self) -> None:
        index = [
            {
                "text": c.text,
                "source_id": c.source_id,
                "source_title": c.source_title,
                "chunk_idx": c.chunk_idx,
            }
            for c in self.chunks
        ]
        with open(self._index_path(), "w") as f:
            json.dump(index, f, indent=2)
        if self.embeddings is not None:
            np.save(str(self._embeddings_path()), self.embeddings)

    def _load(self) -> None:
        if self._index_path().exists():
            try:
                with open(self._index_path()) as f:
                    index = json.load(f)
                self.chunks = [Chunk(**item) for item in index]
                if self._embeddings_path().exists():
                    self.embeddings = np.load(str(self._embeddings_path()))
                logger.info(
                    "Loaded RAG index with %d chunks", len(self.chunks)
                )
            except Exception as e:
                logger.warning("Failed to load RAG index: %s", e)

    def _chunk_text(self, text: str) -> list[str]:
        size = self.config.rag_chunk_size
        overlap = self.config.rag_chunk_overlap
        words = text.split()
        chunks = []
        start = 0
        while start < len(words):
            end = min(start + size, len(words))
            chunks.append(" ".join(words[start:end]))
            if end == len(words):
                break
            start += size - overlap
        return chunks

    def index_paper(self, paper_id: str, text: str) -> int:
        node = self.kg.get_paper(paper_id)
        title = node.label if node else paper_id
        chunks = self._chunk_text(text)
        new_chunks = []
        for i, chunk_text in enumerate(chunks):
            new_chunks.append(Chunk(chunk_text, paper_id, title, i))
        if not new_chunks:
            return 0
        new_texts = [c.text for c in new_chunks]
        new_embs = self.llm.embed_parallel(new_texts)
        new_matrix = np.array(new_embs, dtype=np.float32)
        self.chunks.extend(new_chunks)
        if self.embeddings is None:
            self.embeddings = new_matrix
        else:
            self.embeddings = np.vstack([self.embeddings, new_matrix])
        self._save()
        return len(new_chunks)

    def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        if not self.chunks or self.embeddings is None:
            return []
        top_k = top_k or self.config.rag_top_k
        q_emb = np.array(self.llm.embed(query), dtype=np.float32)
        sims = self.embeddings @ q_emb
        norms = np.linalg.norm(self.embeddings, axis=1) * np.linalg.norm(q_emb)
        sims = np.divide(sims, norms, out=np.zeros_like(sims), where=norms != 0)
        top_indices = np.argsort(sims)[-top_k:][::-1]
        results = []
        for idx in top_indices:
            if sims[idx] > 0:
                results.append({
                    "text": self.chunks[idx].text,
                    "source_id": self.chunks[idx].source_id,
                    "source_title": self.chunks[idx].source_title,
                    "score": float(round(sims[idx], 4)),
                })
        return results

    def answer(self, query: str) -> dict[str, Any]:
        results = self.search(query)
        if not results:
            return {"answer": "No relevant papers found in the index.", "sources": []}
        context = "\n\n".join(
            f"[{i+1}] {r['text']}" for i, r in enumerate(results)
        )
        prompt = (
            f"Answer the question using only the provided context. "
            f"Cite sources as [1], [2], etc.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {query}\n\n"
            f"Answer:"
        )
        answer = self.llm.generate(prompt, temperature=0.0)
        sources = []
        seen = set()
        for r in results:
            if r["source_id"] not in seen:
                sources.append({
                    "id": r["source_id"],
                    "title": r["source_title"],
                })
                seen.add(r["source_id"])
        return {"answer": answer, "sources": sources}

    def stats(self) -> dict[str, Any]:
        return {
            "chunks": len(self.chunks),
            "dimension": self.embeddings.shape[1] if self.embeddings is not None else 0,
            "papers": len(set(c.source_id for c in self.chunks)),
        }
