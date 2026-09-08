from __future__ import annotations

from hive.config import AppConfig
from hive.llm import ChatMessage, chat
from hive.papers import openalex, arxiv, rank as rank_mod, resolver as resolver_mod
from hive.papers.schemas import Paper

from . import prompts
from .session import new_session, save_artifact


def _papers_block(papers: list[Paper]) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        lines.append(
            f"{i}. {p.title} — {', '.join(p.authors[:3])} ({p.year or 'n.d.'}) "
            f"[score {p.score} | cites {p.cited_by_count} | {p.venue or ''}] "
            f"DOI:{p.doi or '-'} arXiv:{p.arxiv_id or '-'} url:{p.url or p.oa_url or ''}\n   Abstract: {(p.abstract or '')[:500]}"
        )
    return "\n".join(lines)


def _fulltext_block(items: list[tuple[Paper, str | None]]) -> str:
    out = []
    for p, txt in items:
        if not txt:
            continue
        out.append(f"--- {p.title} ({p.doi or p.arxiv_id}) ---\n{txt[:6000]}")
    return "\n\n".join(out) or "(no full-text available)"


def search_and_rank(query: str, top_k: int = 10, cfg: AppConfig | None = None) -> list[Paper]:
    # try openalex, fallback arxiv
    papers: list[Paper] = []
    try:
        papers = openalex.search(query, top_k=top_k)
    except Exception:
        pass
    if len(papers) < top_k:
        try:
            papers += arxiv.search(query, top_k=top_k - len(papers))
        except Exception:
            pass
    if not papers:
        return []
    return rank_mod.rank_papers(papers, query)


def enrich_full_text(papers: list[Paper], top_n: int = 3) -> list[tuple[Paper, str | None]]:
    out = []
    for p in papers[:top_n]:
        txt = resolver_mod.fetch_full_text(p)
        out.append((p, txt))
    return out


def run_workflow(kind: str, topic: str, cfg: AppConfig, top_k: int = 10, full_text_top: int = 3) -> str:
    papers = search_and_rank(topic, top_k=top_k, cfg=cfg)
    ft = enrich_full_text(papers, top_n=full_text_top)
    block = _papers_block(papers)
    ft_block = _fulltext_block(ft)

    mapping = {
        "deepresearch": prompts.DEEP_RESEARCH,
        "lit": prompts.LIT_REVIEW,
        "review": prompts.REVIEW,
        "audit": prompts.AUDIT,
        "replicate": prompts.REPLICATE,
        "recipe": prompts.RECIPE,
        "compare": prompts.COMPARE,
        "draft": prompts.DRAFT,
        "autoresearch": prompts.AUTORESEARCH,
        "watch": prompts.WATCH,
    }
    tmpl = mapping.get(kind, prompts.DEEP_RESEARCH)
    # build prompt kwargs per template
    kwargs = {"topic": topic, "papers_block": block, "fulltext_block": ft_block, "n": full_text_top}
    # fill extra keys for specific templates
    kwargs.setdefault("artifact", topic)
    kwargs.setdefault("paper", topic)
    kwargs.setdefault("notes", "(no codebase provided)")
    kwargs.setdefault("context", "(local replication)")
    kwargs.setdefault("task", topic)
    kwargs.setdefault("prior", "(none)")
    kwargs.setdefault("idea", topic)
    try:
        prompt = tmpl.format(**kwargs)
    except KeyError as e:
        prompt = tmpl.format(topic=topic, papers_block=block, fulltext_block=ft_block, n=full_text_top)

    msgs = [ChatMessage(role="system", content=prompts.SYSTEM), ChatMessage(role="user", content=prompt)]
    resp = chat(cfg, msgs)
    content = resp.content
    # persist
    sid = new_session(f"{kind}:{topic[:60]}")
    save_artifact(sid, kind, f"{kind} — {topic[:50]}", content)
    return content
