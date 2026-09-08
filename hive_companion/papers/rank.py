from __future__ import annotations

import math
from .schemas import Paper


def score_paper(p: Paper, query: str | None = None) -> tuple[float, dict]:
    """Transparent PaperRank — deterministic heuristics, LLM can rescore on top."""
    breakdown: dict = {}
    score = 0.0

    # citation signal (log scale) 0-30
    c = p.cited_by_count or 0
    cit_score = min(30, 6 * math.log10(c + 1) * 5) if c > 0 else 0
    # simpler: 10*log10(c+1) capped 30
    cit_score = min(30, 10 * math.log10(c + 1)) if c > 0 else 0
    breakdown["citations"] = round(cit_score, 1)
    score += cit_score

    # recency: newer gets small boost, 0-10
    if p.year:
        age = 2026 - p.year
        rec = max(0, 10 - age * 0.7)
        breakdown["recency"] = round(rec, 1)
        score += rec
    else:
        breakdown["recency"] = 0

    # venue prestige proxy: 0-15 (if venue known, assume peer-reviewed)
    if p.venue and p.venue.lower() not in ("arxiv",):
        breakdown["venue"] = 12
        score += 12
    elif p.venue == "arXiv":
        breakdown["venue"] = 5
        score += 5
    else:
        breakdown["venue"] = 0

    # open access: 0-10
    if p.is_open_access or p.pdf_url or p.oa_url:
        breakdown["open_access"] = 10
        score += 10
    else:
        breakdown["open_access"] = 0

    # abstract presence: 0-5
    if p.abstract and len(p.abstract) > 100:
        breakdown["abstract_quality"] = 5
        score += 5
    else:
        breakdown["abstract_quality"] = 0

    # query match: 0-30 (simple title/abstract contains query tokens)
    if query:
        q_tokens = [t.lower() for t in query.split() if len(t) > 2]
        text = f"{p.title} {p.abstract or ''}".lower()
        hits = sum(1 for t in q_tokens if t in text)
        q_score = min(30, hits * 6)
        breakdown["query_match"] = round(q_score, 1)
        score += q_score
    else:
        breakdown["query_match"] = 0

    score = min(100, score)
    breakdown["total"] = round(score, 1)
    return round(score, 1), breakdown


def rank_papers(papers: list[Paper], query: str) -> list[Paper]:
    for p in papers:
        s, bd = score_paper(p, query)
        p.score = s
        p.score_breakdown = bd
    return sorted(papers, key=lambda x: x.score or 0, reverse=True)
