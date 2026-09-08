from __future__ import annotations

import re
import httpx

from . import arxiv, crossref, europe_pmc, openalex
from .schemas import Paper


def is_doi(s: str) -> bool:
    return bool(re.match(r"10\.\d+/.+", s.strip()))

def is_arxiv(s: str) -> bool:
    return bool(re.match(r"(\d{4}\.\d{4,5}|[a-z\-]+/\d+)", s.strip())) or "arxiv" in s.lower()


def resolve(query: str) -> Paper | None:
    q = query.strip()
    # try DOI
    if is_doi(q):
        # prefer openalex then crossref
        try:
            return openalex.fetch_by_doi(q)
        except Exception:
            return crossref.fetch_by_doi(q)
    # arXiv id
    if is_arxiv(q):
        # extract id
        m = re.search(r"(\d{4}\.\d{4,5})", q)
        if m:
            p = arxiv.fetch(m.group(1))
            if p:
                return p
    # openalex id
    if q.startswith("https://openalex.org/W") or re.match(r"W\d+", q):
        try:
            return openalex.fetch_by_id(q)
        except Exception:
            pass
    # fallback: search
    try:
        res = openalex.search(q, top_k=1)
        if res:
            return res[0]
    except Exception:
        pass
    return None


def fetch_full_text(p: Paper) -> str | None:
    """Attempt legal full-text fetch: OA url, arxiv pdf text stub, Europe PMC."""
    # 1. Europe PMC if DOI
    if p.doi:
        txt = europe_pmc.fetch_fulltext_sections(p.doi)
        if txt:
            return txt
    # 2. arXiv: fetch pdf_url page? For simplicity return abstract + note
    #    (full PDF parsing would require pdfminer; we keep lightweight)
    # 3. OA URL fetch (html)
    if p.oa_url:
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as c:
                r = c.get(p.oa_url, headers={"User-Agent": "hive-research/0.1"})
                if r.status_code == 200 and "pdf" not in r.headers.get("content-type", ""):
                    # return truncated html text
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, "html.parser")
                    return soup.get_text(separator="\n", strip=True)[:30000]
        except Exception:
            pass
    return None
