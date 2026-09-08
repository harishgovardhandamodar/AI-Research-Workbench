from __future__ import annotations

import httpx
from bs4 import BeautifulSoup


def fetch_fulltext_sections(doi_or_pmid: str) -> str | None:
    """Try Europe PMC to get OA full-text sections."""
    # Try DOI first
    for id_type in ["DOI", "PMID"]:
        try:
            with httpx.Client(timeout=20) as c:
                r = c.get(
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                    params={"query": f"{id_type}:\"{doi_or_pmid}\"", "format": "json", "pageSize": 1},
                )
                r.raise_for_status()
                hits = r.json().get("resultList", {}).get("result", [])
                if not hits:
                    continue
                hit = hits[0]
                if hit.get("isOpenAccess") != "Y":
                    continue
                pmcid = hit.get("pmcid")
                if not pmcid:
                    continue
                # fetch fullTextXML
                r2 = c.get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML")
                if r2.status_code != 200:
                    continue
                soup = BeautifulSoup(r2.text, "xml")
                # extract body sections
                secs = []
                for sec in soup.find_all("sec"):
                    title = sec.find("title")
                    t = title.get_text(strip=True) if title else ""
                    ps = " ".join(p.get_text(strip=True) for p in sec.find_all("p"))
                    if ps:
                        secs.append(f"## {t}\n{ps}" if t else ps)
                if secs:
                    return "\n\n".join(secs[:20])
                # fallback: plain body text
                body = soup.find("body")
                if body:
                    return body.get_text(separator="\n", strip=True)[:30000]
        except Exception:
            continue
    return None
