from __future__ import annotations

import httpx

from .schemas import Paper

BASE = "https://api.openalex.org"


def _to_paper(w: dict) -> Paper:
    doi = w.get("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "")
    authors = []
    for a in w.get("authorships", []):
        name = a.get("author", {}).get("display_name")
        if name:
            authors.append(name)
    # abstract inverted index -> text
    abstract = None
    inv = w.get("abstract_inverted_index")
    if inv:
        try:
            max_pos = max(max(v) for v in inv.values())
            words = [""] * (max_pos + 1)
            for word, positions in inv.items():
                for p in positions:
                    words[p] = word
            abstract = " ".join(words)
        except Exception:
            abstract = w.get("abstract")
    else:
        abstract = w.get("abstract")

    venue = None
    host = w.get("primary_location", {}).get("source", {})
    if host:
        venue = host.get("display_name")

    oa = w.get("open_access", {})
    return Paper(
        id=w.get("id", ""),
        title=w.get("title", "") or "",
        authors=authors,
        abstract=abstract,
        venue=venue,
        year=w.get("publication_year"),
        doi=doi,
        openalex_id=w.get("id"),
        url=w.get("doi") or w.get("id"),
        pdf_url=(w.get("primary_location") or {}).get("pdf_url"),
        cited_by_count=w.get("cited_by_count", 0) or 0,
        is_open_access=bool(oa.get("is_oa")),
        oa_url=oa.get("oa_url"),
        publication_date=w.get("publication_date"),
    )


def search(query: str, top_k: int = 10, mailto: str | None = None) -> list[Paper]:
    params = {
        "search": query,
        "per-page": top_k,
        "sort": "relevance_score:desc",
    }
    headers = {"User-Agent": "hive-research/0.1 (mailto: hive@example.com)"}
    if mailto:
        params["mailto"] = mailto
    with httpx.Client(timeout=20, headers=headers) as c:
        r = c.get(f"{BASE}/works", params=params)
        r.raise_for_status()
        data = r.json()
        return [_to_paper(w) for w in data.get("results", [])]


def fetch_by_id(openalex_id: str) -> Paper:
    # openalex_id like https://openalex.org/W123 or W123
    oid = openalex_id.split("/")[-1]
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{BASE}/works/{oid}")
        r.raise_for_status()
        return _to_paper(r.json())


def fetch_by_doi(doi: str) -> Paper:
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{BASE}/works/doi:{doi}")
        r.raise_for_status()
        return _to_paper(r.json())
