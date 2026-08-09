"""Scoring for ft-validate.

Metrics (all 0..1, higher is better unless noted):

- ``faithfulness``  — fraction of the answer's content tokens supported by the
                      retrieved evidence (RAGAS-style, rule-based).
- ``accuracy``      — answer vs gold answer agreement (token F1 + optional
                      embedding cosine).
- ``hallucination`` — *lower* is better: share of answer content not found in
                      the evidence.
- ``retention``     — base↔adapter agreement on the same questions (a proxy for
                      catastrophic forgetting: if the adapter drifts wildly on
                      domain questions, retention drops).

All scoring works fully offline. When a local judge model is configured
(``judge_model`` with Ollama) a qualitative per-answer critique is appended,
but it never blocks or replaces the rule-based metrics.
"""

from __future__ import annotations

import hashlib
import re
from typing import Sequence

import numpy as np

STOP_WORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
              "with", "is", "are", "was", "were", "that", "this", "it", "as",
              "by", "at", "from", "be", "been", "will", "would", "should",
              "can", "could", "may", "might", "must", "have", "has", "had",
              "not", "no", "yes", "do", "does", "did", "about", "into", "over",
              "under", "between", "than", "which", "what", "when", "where",
              "who", "whom", "how", "also", "than", "then", "there", "here"}

_TOKEN_RE = re.compile(r"[a-zA-Z0-9']+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in STOP_WORDS]


def token_f1(a: str, b: str) -> float:
    """Token-level F1 between two strings (0..1)."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    p, r = inter / len(ta), inter / len(tb)
    return round(2 * p * r / (p + r), 4)


def _content_coverage(answer: str, evidence: Sequence[str]) -> tuple[float, float]:
    """Fraction of answer content tokens present in evidence, and the F1."""
    toks = _tokens(answer)
    if not toks:
        return 1.0, 1.0
    ev = set()
    for e in evidence:
        ev |= set(_tokens(e))
    covered = sum(1 for t in toks if t in ev)
    return covered / len(toks), (covered / len(toks)) if ev else 0.0


def _embed_pair(a: str, b: str) -> float | None:
    """Cosine similarity via sentence-transformers when available (else None)."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        model = SentenceTransformer("all-MiniLM-L6-v2")
        v = np.asarray(model.encode([a, b], normalize_embeddings=True))
        return float(v[0] @ v[1])
    except Exception:  # noqa: BLE001
        return None


def score_answer(answer: str, evidence: Sequence[str], gold: str = "",
                 metrics: Sequence[str] | None = None) -> dict[str, float]:
    """Score one answer against evidence + optional gold. Returns 0..1 metrics."""
    metrics = metrics or ["faithfulness", "accuracy", "hallucination", "retention"]
    out: dict[str, float] = {}
    content_cov, f1 = _content_coverage(answer, list(evidence))
    if "faithfulness" in metrics:
        sim = _embed_pair(answer, " ".join(evidence)) if evidence else None
        out["faithfulness"] = round(max(content_cov, f1 or 0.0, sim or 0.0), 4)
    if "accuracy" in metrics:
        if gold:
            cos = _embed_pair(answer, gold)
            out["accuracy"] = round(max(token_f1(answer, gold), cos or 0.0), 4)
        else:
            # No gold: evidence is the ground truth.
            out["accuracy"] = round(max(content_cov, f1 or 0.0), 4)
    if "hallucination" in metrics:
        # Lower is better: fraction of answer content NOT in evidence.
        out["hallucination"] = round(1.0 - content_cov, 4)
    if "retention" in metrics:
        # Set later by compare_base_vs_adapter (needs both answers).
        out["retention"] = 0.0
    return out


def answer_drift(base: str, adapter: str) -> float:
    """1 - agreement between base and adapter answers (drift, 0..1)."""
    return round(1.0 - token_f1(base, adapter), 4)


def aggregate_scores(per_question: Sequence[dict], model_key: str) -> dict:
    """Mean/min/max of one model's metrics across questions."""
    import numpy as _np

    scores: dict[str, list[float]] = {}
    for q in per_question:
        for k, v in (q.get(f"{model_key}_scores") or {}).items():
            scores.setdefault(k, []).append(v)
    out = {}
    for k, vals in scores.items():
        if vals:
            arr = _np.asarray(vals, dtype=float)
            out[k] = {"mean": round(float(arr.mean()), 4),
                      "min": round(float(arr.min()), 4),
                      "max": round(float(arr.max()), 4)}
    return out


def rank_failures(per_question: Sequence[dict], key: str = "faithfulness",
                  n: int = 10) -> list[dict]:
    """Rank the worst questions by a metric (with snippets to act on)."""
    scored = []
    for q in per_question:
        v = (q.get("adapter_scores") or {}).get(key)
        if v is None:
            v = (q.get("base_scores") or {}).get(key, 0.0)
        scored.append((v, q))
    scored.sort(key=lambda t: t[0])
    return [{"question": q["question"], "score": round(v, 4), "metric": key,
             "gold_answer": q.get("gold_answer", ""),
             "evidence": (q.get("evidence_snippets") or [])[:1]}
            for v, q in scored[:n]]


def question_digest(qid: str, question: str) -> str:
    return hashlib.sha1(f"{qid}:{question}".encode()).hexdigest()[:8]
