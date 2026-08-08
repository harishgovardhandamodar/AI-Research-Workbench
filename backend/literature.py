"""Round-11: literature grounding via the Research Knowledge Graph (RKG).

A best-effort 'Related work' block queried from the RKG RAG index, used by the
campaign planner, the reviewer, and the project report. Returns '' whenever the
RKG is unavailable or has no relevant sources, so the rest of the system runs
unchanged without a corpus.
"""

from __future__ import annotations

import asyncio


def _default_query_rag():
    """Resolve the shared RKG organizer lazily (None when not available)."""
    try:
        from .research_knowledge_graphs.router import get_org
        return get_org()
    except Exception:  # noqa: BLE001
        return None


async def literature_context(question: str, limit: int = 4,
                             query_rag=None) -> str:
    """A 'Related work' block (answer + top sources) for `question`.

    `query_rag(question) -> {"answer", "sources": [{id, title}...]}` is
    injectable for tests; defaults to the shared RKG organizer. Returns '' when
    nothing useful is available.
    """
    question = (question or "").strip()
    if not question:
        return ""
    if query_rag is None:
        org = _default_query_rag()
        if org is None:
            return ""
        query_rag = org.query_rag
    try:
        result = await asyncio.to_thread(query_rag, question)
    except Exception:  # noqa: BLE001
        return ""
    answer = (result or {}).get("answer") or ""
    sources = (result or {}).get("sources") or []
    if not answer.strip() and not sources:
        return ""
    lines = ["- Related work: " + (" ".join(str(answer).split())[:700]
                                   if str(answer).strip() else "(no summary)")]
    for s in list(sources)[:limit]:
        title = str(s.get("title") or "").strip()
        sid = str(s.get("id") or "").strip()
        if title or sid:
            lines.append(f"  - [{sid}] {title}")
    return "\n".join(lines)


def project_question(rt) -> str:
    """A sensible project-level research question for grounding: the most recent
    campaign's research question, else the latest experiment's hypothesis."""
    try:
        camps = rt.store.list_campaigns()
        for c in camps:
            q = (c.get("research_question") or "").strip()
            if q:
                return q
        exps = rt.store.list_experiments()
        for e in reversed(exps):
            h = (e.get("hypothesis") or "").strip()
            if h:
                return h
    except Exception:  # noqa: BLE001
        pass
    return ""
