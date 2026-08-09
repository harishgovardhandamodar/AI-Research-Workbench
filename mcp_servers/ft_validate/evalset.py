"""Eval set generation for ft-validate.

Produces held-out questions over the source knowledge:

- ``heldout``   — deterministic questions derived from chunk content (works
                  fully offline; no LLM needed).
- ``synthetic`` — prefers a local LLM (Ollama) to write harder, more natural
                  questions; falls back to heldout templates offline.
- ``hard``      — multi-hop / cross-chunk questions by pairing distant chunks
                  (again with local-LLM synthesis when available).

Every question keeps evidence pointers (chunk ids) so scoring can check
faithfulness against the retrieved ground truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any

from .models import EvalQuestion, EvalSet
from .store import ValidateStore

SYNTHESIS_PROMPT = (
    "You are writing hard, realistic evaluation questions about domain "
    "material. Questions must be answerable ONLY from the provided sources and "
    "require specific facts (numbers, dates, names, policies). Return valid "
    "JSON: a list of objects with 'question' and 'gold_answer'."
)


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


def _local_llm_available() -> bool:
    try:
        req = urllib.request.Request(f"{_ollama_host()}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            tags = json.loads(resp.read().decode())
        return bool(tags.get("models"))
    except Exception:  # noqa: BLE001
        return False


def _gen_via_llm(prompt: str, model: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.6, "num_predict": 1200}}).encode()
    req = urllib.request.Request(f"{_ollama_host()}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        return data.get("response", "")
    except Exception:  # noqa: BLE001
        return ""


def _json_list(text: str) -> list[dict] | None:
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e <= s:
        return None
    try:
        data = json.loads(text[s:e + 1])
        return data if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def _stem(q: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in q.lower())
    return keep.strip("_")[:24] or "q"


def _from_chunk_heldout(chunk: dict, difficulty: str) -> EvalQuestion:
    text = " ".join(chunk["text"].split())
    snippet = text[:110]
    src = chunk.get("source") or chunk.get("metadata", {}).get("path", "")
    q = (f"Based on the source material, what is stated about: {snippet}…? "
         "Give specifics (names, numbers, dates) if present.")
    return EvalQuestion(
        id=hashlib.sha1(f"{chunk['id']}:{q}".encode()).hexdigest()[:12],
        question=q, gold_answer=text[:400],
        evidence_chunk_ids=[chunk["id"]],
        evidence_snippets=[text[:1200]],
        difficulty=difficulty, tags=["heldout", src.split("/")[-1] if src else ""],
    )


def _from_chunk_pair_hard(a: dict, b: dict) -> EvalQuestion:
    ta, tb = " ".join(a["text"].split()), " ".join(b["text"].split())
    q = (f"Synthesize across two source passages: how does '{ta[:90]}…' relate "
         f"to '{tb[:90]}…'? Identify any conflict or connection.")
    return EvalQuestion(
        id=hashlib.sha1(f"{a['id']}:{b['id']}:hard".encode()).hexdigest()[:12],
        question=q, gold_answer=f"{ta[:400]}\n---\n{tb[:400]}",
        evidence_chunk_ids=[a["id"], b["id"]],
        evidence_snippets=[ta[:1200], tb[:1200]],
        difficulty="hard", tags=["multi_hop", a.get("source", ""), b.get("source", "")],
    )


def _llm_questions(chunk: dict, prompt: str, model: str, n: int) -> list[EvalQuestion]:
    text = chunk["text"][:2200]
    response = _gen_via_llm(prompt + f"\n\nSOURCE:\n{text}\n\nGenerate {n} questions now.",
                            model)
    items = _json_list(response) or []
    out = []
    for it in items[:n]:
        if isinstance(it, dict) and it.get("question"):
            out.append(EvalQuestion(
                id=hashlib.sha1(f"{chunk['id']}:{it['question']}".encode()).hexdigest()[:12],
                question=str(it["question"]),
                gold_answer=str(it.get("gold_answer", "")),
                evidence_chunk_ids=[chunk["id"]],
                evidence_snippets=[text[:1200]],
                difficulty="hard", tags=["synthetic"],
            ))
    return out


def generate_eval_set(store: ValidateStore, index_id: str,
                      mode: str = "heldout", n: int = 20,
                      difficulty: str = "medium",
                      local_llm_model: str = "auto",
                      eval_set_id: str | None = None) -> dict:
    """Generate an eval set from a RAG index. Returns eval_set_id + sample."""
    from typing import Literal, cast
    if mode not in ("heldout", "synthetic", "hard"):
        raise ValueError(f"unknown mode '{mode}' (expected heldout/synthetic/hard)")
    mode = cast(Literal["heldout", "synthetic", "hard"], mode)
    if n < 1 or n > 2000:
        raise ValueError("n must be in 1..2000")
    data = store.get_rag_index(index_id)
    if data is None:
        raise ValueError(f"RAG index not found: {index_id} — run build_rag_index first")
    chunks, _v, _meta = data
    if not chunks:
        raise ValueError("RAG index has no chunks")

    use_llm = bool(_local_llm_available()) and mode in ("synthetic", "hard")
    questions: list[EvalQuestion] = []
    if mode == "heldout":
        for c in chunks[:max(1, n)]:
            questions.append(_from_chunk_heldout(c, difficulty))
    elif mode == "synthetic":
        for c in chunks[: max(1, n)]:
            if use_llm:
                qs = _llm_questions(c, SYNTHESIS_PROMPT, local_llm_model, 1)
                questions.extend(qs or [_from_chunk_heldout(c, difficulty)])
            else:
                questions.append(_from_chunk_heldout(c, difficulty))
    else:  # hard: pair distant chunks (multi-hop)
        step = max(1, len(chunks) // max(1, n))
        for i in range(0, min(len(chunks), n * step), step):
            j = (i + step // 2) % len(chunks)
            if i != j:
                questions.append(_from_chunk_pair_hard(chunks[i], chunks[j]))
        if not questions:
            for c in chunks[: n]:
                questions.append(_from_chunk_heldout(c, "hard"))

    questions = questions[:n]
    es_id = eval_set_id or f"ev-{hashlib.sha1(f'{mode}:{index_id}:{time.time()}'.encode()).hexdigest()[:8]}"
    es = EvalSet(id=es_id, mode=mode, created_at=time.time(), questions=questions)
    store.save_evalset(es)
    return {"eval_set_id": es_id, "mode": mode, "n": len(questions),
            "difficulty": difficulty, "used_local_llm": use_llm,
            "sample": [q.model_dump() for q in questions[:5]]}


def preview_eval_set(store: ValidateStore, eval_set_id: str, n: int = 10) -> dict:
    es = store.get_evalset(eval_set_id)
    if es is None:
        raise ValueError(f"eval set not found: {eval_set_id}")
    return {"eval_set_id": es.id, "mode": es.mode, "total": len(es.questions),
            "questions": [q.model_dump() for q in es.questions[:n]]}


def list_eval_sets(store: ValidateStore) -> dict:
    return {"eval_sets": store.list_evalsets()}
