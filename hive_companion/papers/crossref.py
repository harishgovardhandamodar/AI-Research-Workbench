from __future__ import annotations

import httpx
from .schemas import Paper


def fetch_by_doi(doi: str) -> Paper | None:
    with httpx.Client(timeout=20, headers={"User-Agent": "hive-research/0.1"}) as c:
        r = c.get(f"https://api.crossref.org/works/{doi}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        m = r.json().get("message", {})
        title = " ".join(m.get("title", []))
        authors = []
        for a in m.get("author", []):
            authors.append(f"{a.get('given','')} {a.get('family','')}".strip())
        year = None
        try:
            year = m.get("published-print", m.get("published-online", m.get("created", {}))).get("date-parts", [[None]])[0][0]
        except Exception:
            pass
        return Paper(
            id=f"doi:{doi}",
            title=title,
            authors=authors,
            abstract=m.get("abstract"),
            venue=m.get("container-title", [None])[0] if m.get("container-title") else None,
            year=year,
            doi=doi,
            url=m.get("URL"),
            cited_by_count=m.get("is-referenced-by-count", 0),
        )
