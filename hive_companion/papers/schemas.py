from __future__ import annotations

from pydantic import BaseModel, Field


class Paper(BaseModel):
    id: str  # openalex id or doi or arxiv
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    venue: str | None = None
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    cited_by_count: int = 0
    is_open_access: bool = False
    oa_url: str | None = None
    publication_date: str | None = None
    # ranking fields
    score: float | None = None
    score_breakdown: dict | None = None
    evidence: str | None = None
