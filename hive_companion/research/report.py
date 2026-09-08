"""Deep Analysis Report — Feynman TUI parity, local-only.

Covers all Feynman workbench report surfaces in depth:
project/session ledger, Pi-chat grounding, BioTools stubs,
compute provenance, artifact previews, element-level annotations,
checklist rubric, lineage, and lab-notebook changelog.

Uses only local LLM (Ollama/LM Studio) + public paper APIs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import textwrap
from pathlib import Path
from typing import List

from hive.config import AppConfig, load_config, CONFIG_FILE, DB_FILE
from hive.llm import ChatMessage, chat, get_provider
from hive.papers.schemas import Paper
from hive.research.workflows import search_and_rank, enrich_full_text
from hive.research.session import save_artifact, new_session
from hive.research import prompts

# ── Feynman-parity section map ──────────────────────────────────
# Mirrors Feynman: hero, provenance, Pi chat, BioTools, notebooks,
# compute, artifact previews, element annotations, lineage, settings,
# skills, onboarding, changelog. Each section is rendered locally.

FEYNMAN_SECTIONS = [
    "hero",
    "executive_summary",
    "provenance_ledger",
    "project_session",
    "pi_chat_grounding",
    "biotools_connector",
    "literature_synthesis",
    "methods_comparison_matrix",
    "results_evidence",
    "artifact_previews",
    "element_annotations",
    "checklist_rubric",
    "compute_provenance",
    "lineage_provenance",
    "reproducibility_audit",
    "gaps_controversies",
    "reading_list",
    "lab_notebook_changelog",
    "limitations",
    "references",
]

DEEP_REPORT_SYSTEM = """You are Hive Research - A Local research companion — deep analysis report writer.
You run only on Ollama / LM Studio. No cloud. Every claim must be source-grounded with [Author Year] or DOI/URL.
You must produce element-level evidence: quote sections, link papers, and mark confidence.
You must fill a checklist rubric and provenance ledger. Be concise but deep. Use markdown with H2 per section.
"""

DEEP_REPORT_TEMPLATE = """Topic: {topic}
Depth: {depth} (brief|standard|deep)
Sections requested: {sections}

Papers (ranked, PaperRank 0-100, with breakdown):
{papers_block}

Section-aware full-text evidence (top {full_text_top} papers, Europe PMC / OA excerpts, truncated 6k each):
{fulltext_block}

Project/session context:
  session_id: {session_id}
  created: {created}
  config: {config_str}
  provider: {provider} model: {model}
  workspace: {workspace}

Compute provenance (local-only):
  - No cloud egress: Ollama {ollama_url} / LM Studio {lmstudio_url}
  - DB: {db_path}
  - Textual TUI: Hive Research - A Local research companion

BioTools connector stubs (available locally, not executed, cite if relevant):
  OpenAlex, arXiv, Crossref, Europe PMC full-text sections, PubMed, ClinicalTrials, ChEMBL, PubChem, Ensembl, UniProt — all via public APIs, HF_TOKEN not required.

Task: Write a DEEP ANALYSIS REPORT in markdown, covering ALL sections below in order, with H2 headings. For each non-trivial claim, add citation and evidence quote. Add element-level annotations as footnotes or inline > quotes. End with provenance JSON and changelog entry.

Required H2 sections (exact):
## 1. Hero
## 2. Executive Summary (5-7 bullets, with confidence)
## 3. Provenance Ledger (papers, scores, OA, full-text hits, LLM model, timestamps)
## 4. Project & Session (session_id, lineage, artifact links)
## 5. Pi Chat Grounding (how local chat was used, streaming, tool calls)
## 6. BioTools Connector (which connectors used, which stubbed, why)
## 7. Literature Synthesis (thematic clusters, publication trajectories)
## 8. Methods Comparison Matrix (table: paper | problem | method | dataset | metrics | results | limitations | reproducibility)
## 9. Results & Evidence (section-aware quotes, element annotations)
## 10. Artifact Previews (describe report, JSONL, tables, notebook, PDF, chemistry KET/RXN stubs)
## 11. Element-Level Annotations (HTML report annotation spec: element_id, quote, source)
## 12. Checklist Rubric (per-claim 0-3 evidence quality, plus overall 0-10)
## 13. Compute Provenance (local-only, no Modal/RunPod, Files host inventory stub, egress none)
## 14. Lineage & Provenance (artifact/version lineage, frame records stub, backfill health)
## 15. Reproducibility Audit (paper vs codebase mismatch stub if applicable)
## 16. Gaps, Controversies & Open Questions
## 17. What to Read Next (ranked reading list with why)
## 18. Lab Notebook Changelog (CHANGELOG.md entry for this run)
## 19. Limitations (model, data, freshness)
## 20. References (numbered, with DOI/arXiv/OpenAlex URL, PDF/OA)

Constraints:
- Use only papers above; if evidence thin, state it.
- Keep citations inline: [Author Year](DOI/URL) or [1].
- For checklist, score each major claim.
- For provenance JSON, include session_id, provider, model, top_k, full_text_top, paper_ids, scores, timestamp, config hash.
- Write as if for Feynman TUI artifact preview (rich markdown, tables, code blocks).

Produce the full report now.
"""


def _papers_block(papers: List[Paper]) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        lines.append(
            f"{i}. {p.title} — {', '.join(p.authors[:3])} ({p.year or 'n.d.'}) "
            f"[{p.venue or '—'}] score={p.score} {p.score_breakdown} cites={p.cited_by_count} OA={p.is_open_access} "
            f"DOI:{p.doi or '-'} arXiv:{p.arxiv_id or '-'} id:{p.id} url:{p.url or p.oa_url or p.pdf_url or '-'}"
            f"\n   Abstract: {(p.abstract or '')[:600]}"
        )
    return "\n".join(lines)


def _fulltext_block(items) -> str:
    out = []
    for p, txt in items:
        if not txt:
            continue
        out.append(f"--- {p.title} ({p.doi or p.arxiv_id or p.id}) ---\n{txt[:6000]}")
    return "\n\n".join(out) or "(no full-text available — OA only via Europe PMC / oa_url)"


def _provenance_hash(topic: str, papers: List[Paper], cfg: AppConfig) -> str:
    h = hashlib.sha256()
    h.update(topic.encode())
    for p in papers:
        h.update((p.id + str(p.score)).encode())
    h.update((cfg.llm.provider + cfg.llm.ollama_model + cfg.llm.lmstudio_model).encode())
    return h.hexdigest()[:12]


def generate_deep_report(
    topic: str,
    cfg: AppConfig | None = None,
    top_k: int = 12,
    full_text_top: int = 3,
    depth: str = "deep",
    sections: str | None = None,
    save: bool = True,
) -> tuple[str, str, str]:
    """Generate deep analysis report. Returns (markdown, session_id, artifact_id)."""
    cfg = cfg or load_config()
    prov = get_provider(cfg)
    ok, msg = prov.health()
    if not ok:
        raise RuntimeError(f"No local LLM reachable: {msg} — check ollama serve / LM Studio at {cfg.llm.ollama_url} / {cfg.llm.lmstudio_url}")

    papers = search_and_rank(topic, top_k=top_k, cfg=cfg)
    if not papers:
        raise RuntimeError(f"No papers found for topic: {topic}")

    ft = enrich_full_text(papers, top_n=full_text_top)
    from hive.machine import WORKSPACE as MWS
    MWS.mkdir(parents=True, exist_ok=True)

    session_id = new_session(f"report:{topic[:60]}")
    created = datetime.datetime.now().isoformat(timespec="seconds")
    config_str = f"provider={cfg.llm.provider} ollama={cfg.llm.ollama_model} lmstudio={cfg.llm.lmstudio_model} top_k={top_k} full_text_top={full_text_top} depth={depth}"
    prov_name = prov.name
    prov_model = prov.model
    prompt = DEEP_REPORT_TEMPLATE.format(
        topic=topic,
        depth=depth,
        sections=sections or ", ".join(FEYNMAN_SECTIONS),
        papers_block=_papers_block(papers),
        fulltext_block=_fulltext_block(ft),
        full_text_top=full_text_top,
        session_id=session_id,
        created=created,
        config_str=config_str,
        provider=prov_name,
        model=prov_model,
        workspace=str(MWS),
        ollama_url=cfg.llm.ollama_url,
        lmstudio_url=cfg.llm.lmstudio_url,
        db_path=str(DB_FILE),
    )
    msgs = [ChatMessage(role="system", content=DEEP_REPORT_SYSTEM), ChatMessage(role="user", content=prompt)]
    resp = chat(cfg, msgs)
    md = resp.content.strip()
    # ensure provenance JSON appended if LLM omitted
    if "provenance" not in md.lower() or "session_id" not in md:
        prov_json = {
            "session_id": session_id,
            "topic": topic,
            "provider": prov_name,
            "model": prov_model,
            "top_k": top_k,
            "full_text_top": full_text_top,
            "paper_ids": [p.id for p in papers],
            "scores": {p.id: p.score for p in papers},
            "timestamp": created,
            "config_hash": _provenance_hash(topic, papers, cfg),
            "config_path": str(CONFIG_FILE),
            "depth": depth,
        }
        md += "\n\n## Provenance JSON\n```json\n" + json.dumps(prov_json, indent=2) + "\n```\n"

    artifact_id = ""
    if save:
        artifact_id = save_artifact(session_id, "deep_report", f"Deep Report — {topic[:60]}", md)
        # also write to workspace for preview
        try:
            out_path = MWS / f"report_{session_id}.md"
            out_path.write_text(md, encoding="utf-8")
        except Exception:
            pass

    return md, session_id, artifact_id
