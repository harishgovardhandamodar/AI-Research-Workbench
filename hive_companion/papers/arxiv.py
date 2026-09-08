from __future__ import annotations

import re
import httpx
import feedparser

ARXIV_API = "https://export.arxiv.org/api/query"

from .schemas import Paper


def search(query: str, top_k: int = 10) -> list[Paper]:
    params = {"search_query": f"all:{query}", "start": 0, "max_results": top_k, "sortBy": "relevance"}
    with httpx.Client(timeout=20) as c:
        r = c.get(ARXIV_API, params=params)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        papers: list[Paper] = []
        for e in feed.entries:
            # e.id like http://arxiv.org/abs/2301.12345v1
            m = re.search(r"arxiv\.org/abs/([^v]+)", e.id)
            arxiv_id = m.group(1) if m else e.id
            authors = [a.name for a in e.authors] if hasattr(e, "authors") else []
            pdf_url = None
            for link in e.links:
                if link.type == "application/pdf":
                    pdf_url = link.href
            papers.append(
                Paper(
                    id=f"arxiv:{arxiv_id}",
                    title=e.title.replace("\n", " ").strip(),
                    authors=authors,
                    abstract=e.summary.replace("\n", " ").strip() if hasattr(e, "summary") else None,
                    venue="arXiv",
                    year=int(e.published[:4]) if hasattr(e, "published") else None,
                    arxiv_id=arxiv_id,
                    url=e.id,
                    pdf_url=pdf_url,
                    cited_by_count=0,
                )
            )
        return papers


def fetch(arxiv_id: str) -> Paper | None:
    res = search(f"arXiv:{arxiv_id}", top_k=3)
    # fallback direct id search
    if not res:
        with httpx.Client(timeout=20) as c:
            r = c.get(ARXIV_API, params={"id_list": arxiv_id})
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            if feed.entries:
                e = feed.entries[0]
                m = re.search(r"arxiv\.org/abs/([^v]+)", e.id)
                aid = m.group(1) if m else arxiv_id
                authors = [a.name for a in e.authors] if hasattr(e, "authors") else []
                return Paper(
                    id=f"arxiv:{aid}",
                    title=e.title.replace("\n", " ").strip(),
                    authors=authors,
                    abstract=e.summary.replace("\n", " ").strip() if hasattr(e, "summary") else None,
                    venue="arXiv",
                    year=int(e.published[:4]) if hasattr(e, "published") else None,
                    arxiv_id=aid,
                    url=e.id,
                )
    return res[0] if res else None
