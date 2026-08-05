from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .arxiv_fetcher import PaperInfo, download_pdf, fetch_by_id
from .config import Config
from .gpu import GPUManager
from .graph import KnowledgeGraph
from .llm import LLMInterface
from .parser import extract_images_from_pdf, extract_referenced_arxiv_ids, extract_sections, extract_text

logger = logging.getLogger(__name__)


def _sanitize_id(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60].strip("_")


class PaperPipeline:
    def __init__(
        self,
        config: Config,
        llm: LLMInterface,
        kg: KnowledgeGraph,
        gpu_mgr: GPUManager | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.kg = kg
        self.gpu_mgr = gpu_mgr

    def process_paper(self, paper: PaperInfo, gpu_id: int | None = None, model: str | None = None) -> dict[str, Any]:
        paper_id = paper.arxiv_id
        existing = self.kg.get_paper(paper_id)
        if existing:
            return {"status": "exists", "paper_id": paper_id}

        node = self.kg.add_paper(
            paper_id=paper_id,
            title=paper.title,
            authors=paper.authors_str,
            published=paper.published,
            abstract=paper.abstract,
            categories=paper.categories,
            affiliations=paper.affiliations_str,
        )

        pdf_text = ""
        pdf_path = None
        figures = []
        if self.config.arxiv_download_pdf:
            pdf_path = download_pdf(paper_id, self.config.papers_dir)
            if pdf_path and pdf_path.exists():
                pdf_text = extract_text(pdf_path)
                safe_title = _sanitize_id(paper.title) or paper_id
                figures_dir = Path(self.config.vault_dir) / safe_title / "figures"
                figures = extract_images_from_pdf(pdf_path, figures_dir)
        text_for_analysis = pdf_text or paper.abstract

        analysis = self._analyze_text(text_for_analysis, paper.title, figures=figures, model=model, gpu_id=gpu_id)

        concepts = analysis.get("concepts", [])
        relations = analysis.get("relations", [])
        summary = analysis.get("summary", "")
        tags = analysis.get("tags", [])
        notes = analysis.get("notes", "")
        experiment = analysis.get("experiment", {})
        results = analysis.get("results", {})
        experiments_list = analysis.get("experiments", [])
        lineage_notes = analysis.get("lineage_notes", "")

        import json as _json
        extra = _json.dumps({"notes": notes, "experiment": experiment, "results": results})
        node.definition = extra[:2000]

        for tag in tags:
            tag_id = _sanitize_id(tag)
            matched = self.kg.find_similar_concept(tag)
            if matched:
                cid = matched.id
            else:
                cid = tag_id
                self.kg.add_concept(cid, tag, definition=f"A paper tagged with '{tag}'.", concept_type="tag")
            self.kg.add_edge(paper_id, cid, "related_to")

        for c in concepts:
            cid = _sanitize_id(c.get("name", "")) or _sanitize_id(c.get("label", ""))
            if not cid:
                continue
            label = c.get("name", c.get("label", cid))
            definition = c.get("definition", "")
            matched = self.kg.find_similar_concept(label)
            if matched:
                cid = matched.id
                if definition and not matched.definition:
                    matched.definition = definition
            else:
                self.kg.add_concept(
                    cid,
                    label,
                    definition=definition,
                    concept_type=c.get("type", "concept"),
                )
            rel = c.get("relation", "related_to")
            self.kg.add_edge(paper_id, cid, rel)

        for r in relations:
            raw_src = r.get("source", paper_id)
            raw_tgt = r.get("target", "")
            rel = r.get("relation", "related_to")
            if raw_src and raw_tgt:
                src = self._resolve_id(raw_src, paper_id)
                tgt = self._resolve_id(raw_tgt, paper_id)
                if src and tgt:
                    self.kg.add_edge(src, tgt, rel)

        lineage_refs = []
        if pdf_text:
            lineage_refs = self.fetch_lineage(paper_id, pdf_text, gpu_id=gpu_id)
            if lineage_refs:
                logger.info("Lineage: %d prior papers linked for %s", len(lineage_refs), paper_id)

        note_path = self._write_notes_multi(
            paper_id, paper, summary, tags, concepts,
            notes=notes, experiment=experiment, results=results,
            experiments_list=experiments_list, lineage_notes=lineage_notes,
            figures=figures,
        )
        self.kg.save()

        result = {
            "status": "added",
            "paper_id": paper_id,
            "concepts": len(concepts),
            "tags": len(tags),
            "relations": len(relations),
            "note_path": str(note_path) if note_path else None,
            "has_notes": bool(notes),
            "has_experiment": bool(experiments_list),
            "has_results": bool(results and isinstance(results, dict) and any(v for v in results.values())),
            "figures": len(figures),
            "gpu_id": gpu_id,
        }

        if lineage_refs:
            result["lineage"] = lineage_refs

        return result

    def process_papers_parallel(self, papers: list[PaperInfo], model: str | None = None) -> list[dict[str, Any]]:
        count = len(papers)
        if count == 0:
            return []
        max_parallel = min(
            self.config.gpu_parallel_papers,
            self.gpu_mgr.device_count() if self.gpu_mgr else 1,
            count,
        )
        results: list[dict[str, Any] | None] = [None] * count

        def _process(idx: int, paper: PaperInfo) -> None:
            gpu_id = idx % max_parallel if max_parallel > 1 else None
            try:
                results[idx] = self.process_paper(paper, gpu_id=gpu_id, model=model)
            except Exception as e:
                logger.error("Parallel process for %s failed: %s", paper.arxiv_id, e)
                results[idx] = {"status": "error", "paper_id": paper.arxiv_id, "error": str(e)}

        threads = []
        for i, paper in enumerate(papers):
            t = threading.Thread(target=_process, args=(i, paper), daemon=True)
            t.start()
            threads.append(t)
            if len(threads) >= max_parallel:
                for tt in threads:
                    tt.join(timeout=600)
                threads = []

        for t in threads:
            t.join(timeout=600)

        return [r for r in results if r is not None]

    def fetch_lineage(self, paper_id: str, pdf_text: str, max_refs: int = 10, gpu_id: int | None = None) -> list[dict[str, Any]]:
        ref_ids = extract_referenced_arxiv_ids(pdf_text)
        if not ref_ids:
            return []
        fetched = []
        for i, aid in enumerate(ref_ids[:max_refs]):
            if self.kg.get_paper(aid):
                self.kg.add_edge(paper_id, aid, "cites")
                fetched.append({"arxiv_id": aid, "status": "exists"})
                continue
            if i > 0:
                time.sleep(3)
            prior = fetch_by_id(aid)
            if prior is None:
                continue
            self.kg.add_paper(
                paper_id=aid,
                title=prior.title,
                authors=prior.authors_str,
                published=prior.published,
                abstract=prior.abstract,
                categories=prior.categories,
                affiliations=prior.affiliations_str,
            )
            self.kg.add_edge(paper_id, aid, "cites")
            fetched.append({"arxiv_id": aid, "title": prior.title[:80], "status": "added"})
            logger.info("Lineage: linked prior paper %s — %s", aid, prior.title[:80])
        if fetched:
            self.kg.save()
        return fetched

    def _resolve_id(self, name: str, fallback: str) -> str:
        sid = _sanitize_id(name)
        node = self.kg.get_paper(sid) or self.kg.get_concept(sid)
        if node:
            return node.id
        for n in self.kg._hive.nodes:
            if n.label.lower() == name.lower():
                return n.id
        for n in self.kg._hive.nodes:
            if name.lower() in n.label.lower() or n.label.lower() in name.lower():
                return n.id
        return sid or fallback

    def _build_figure_context(self, figures: list[dict[str, Any]]) -> str:
        if not figures:
            return ""
        by_page: dict[int, list[str]] = {}
        for f in figures:
            by_page.setdefault(f["page"], []).append(f["filename"])
        lines = ["\nFigures available in the PDF (reference them using [FIGURE:page=N]):"]
        for p in sorted(by_page):
            lines.append(f"  Page {p}: {', '.join(by_page[p])}")
        lines.append("")
        return "\n".join(lines)

    def _analyze_text(
        self,
        text: str,
        title: str,
        figures: list[dict[str, Any]] | None = None,
        model: str | None = None,
        gpu_id: int | None = None,
    ) -> dict[str, Any]:
        max_chars = 12000
        truncated = text[:max_chars]

        fast_prompt = (
            f"Paper: {title}\n\n"
            f"{truncated[:2000]}\n\n"
            'Extract up to 5 key tags (short keywords) as a JSON list: {"tags": [...]}'
        )
        tags_result = self.llm.extract_structured(
            fast_prompt,
            model=self.config.ollama_fast_model,
            gpu_id=gpu_id,
        )
        tags = tags_result.get("tags", [])

        figure_context = self._build_figure_context(figures or [])

        main_prompt = (
            f"Title: {title}\n\n"
            f"{figure_context}"
            f"{truncated}\n\n"
            "Return ONLY valid JSON with these fields:\n"
            "{\n"
            '  "summary": "2-3 sentence summary covering problem, approach, and key results (include numbers)",\n'
            '  "notes": "Detailed explanation of the method, architecture, experiments, and results with specific details numbers",\n'
            '  "experiments": [\n'
            '    {\n'
            '      "name": "Experiment name",\n'
            '      "goal": "What this tests",\n'
            '      "methodology": "Method used",\n'
            '      "dataset": "Dataset name",\n'
            '      "setup": "Hyperparameters, dimensions",\n'
            '      "baselines": "Methods compared against",\n'
            '      "metrics": {"metric_name": "value"},\n'
            '      "results": "Key results with numbers",\n'
            '      "findings": "Key takeaways"\n'
            '    }\n'
            '  ],\n'
            '  "experiment": {"methodology": "...", "dataset": "...", "setup": "..."},\n'
            '  "results": {"main_findings": "...", "metrics": {"metric_name": "value"}},\n'
            '  "lineage_notes": "Prior work this builds on and how it differs",\n'
            '  "concepts": [{"name": "...", "definition": "...", "relation": "type"}],\n'
            '  "relations": [{"source": "...", "target": "...", "relation": "..."}]\n'
            "}"
        )
        analysis = self.llm.extract_structured(main_prompt, model=model, gpu_id=gpu_id)
        analysis["tags"] = tags
        return analysis

    def _embed_figures(
        self,
        markdown_text: str,
        figures: list[dict[str, Any]],
        relative_prefix: str = "figures/",
    ) -> str:
        if not figures:
            return markdown_text
        page_figures: dict[int, list[dict[str, Any]]] = {}
        for f in figures:
            page_figures.setdefault(f["page"], []).append(f)

        def _replace(match):
            page = int(match.group(1))
            fs = page_figures.get(page, [])
            if not fs:
                return match.group(0)
            links = "\n".join(
                f"![{f.get('caption', '').strip() or 'Figure from page ' + str(page)}]({relative_prefix}{f['filename']})"
                for f in fs
            )
            return links

        return re.sub(r"\[FIGURE:page=(\d+)\]", _replace, markdown_text)

    def _write_notes_multi(
        self,
        paper_id: str,
        paper: PaperInfo,
        summary: str,
        tags: list[str],
        concepts: list[dict[str, Any]],
        notes: str = "",
        experiment: dict[str, Any] | None = None,
        results: dict[str, Any] | None = None,
        experiments_list: list[dict[str, Any]] | None = None,
        lineage_notes: str = "",
        figures: list[dict[str, Any]] | None = None,
        safe_title: str | None = None,
    ) -> Path | None:
        vault = Path(self.config.vault_dir)
        vault.mkdir(parents=True, exist_ok=True)
        safe_title = safe_title or _sanitize_id(paper.title) or paper_id
        paper_dir = vault / safe_title
        paper_dir.mkdir(parents=True, exist_ok=True)

        figures = figures or []
        figures_dir = paper_dir / "figures"
        if figures:
            figures_dir.mkdir(parents=True, exist_ok=True)

        note_lines: list[str] = [
            "---",
            f"arxiv_id: {paper_id}",
            f"title: \"{paper.title}\"",
            f"authors: \"{paper.authors_str}\"",
            f"published: {paper.published}",
            f"tags: [{', '.join(tags)}]",
            "---",
            "",
        ]

        if summary:
            note_lines.extend(["## Summary", "", summary, ""])

        if notes:
            embedded_notes = self._embed_figures(notes, figures, relative_prefix="figures/")
            note_lines.extend(["## Notes", "", embedded_notes, ""])

        if lineage_notes:
            note_lines.extend(["## Prior Work / Research Lineage", "", lineage_notes, ""])

        if results and isinstance(results, dict):
            res_parts: list[str] = []
            mf = results.get("main_findings")
            if mf:
                if isinstance(mf, list):
                    res_parts.extend(mf)
                else:
                    res_parts.append(str(mf))
            if results.get("metrics") and isinstance(results["metrics"], dict):
                m_items = []
                for mk, mv in results["metrics"].items():
                    if mv is None or mv == "":
                        continue
                    if isinstance(mv, (list, tuple)):
                        m_items.append(f"{mk}: {', '.join(str(x) for x in mv)}")
                    else:
                        m_items.append(f"{mk}: {mv}")
                if m_items:
                    res_parts.append("Metrics: " + " | ".join(m_items))
            if res_parts:
                note_lines.extend(["## Results", ""])
                for part in res_parts:
                    note_lines.append(part)
                note_lines.append("")

        if concepts:
            note_lines.extend(["## Concepts", ""])
            for c in concepts:
                name = c.get("name", c.get("label", ""))
                rel = c.get("relation", "")
                note_lines.append(f"- **{name}** ({rel})")
            note_lines.append("")

        note_lines.extend(["## Links", "", f"- [arXiv](https://arxiv.org/abs/{paper_id})"])
        if any(c.get("definition") for c in concepts):
            note_lines.extend(["", "## Definitions", ""])
            for c in concepts:
                if c.get("definition"):
                    note_lines.append(f"- **{c.get('name', c.get('label', ''))}**: {c['definition']}")

        if figures:
            note_lines.extend(["", "## Figures", ""])
            for f in figures:
                cap = f.get("caption", "").strip()
                label = cap if cap else f['filename']
                note_lines.extend([
                    f"- **Page {f['page']}**: {label}",
                    "",
                    f"![{label}](figures/{f['filename']})",
                    "",
                ])

        safe_lines = [str(item) if not isinstance(item, str) else item for item in note_lines]
        notes_path = paper_dir / "00_notes.md"
        with open(notes_path, "w") as f:
            f.write("\n".join(safe_lines))

        if experiments_list:
            for exp in experiments_list:
                if not isinstance(exp, dict):
                    continue
                exp_name = exp.get("name", "").strip()
                if not exp_name:
                    continue
                safe_exp = _sanitize_id(exp_name) or "experiment"
                exp_lines: list[str] = [
                    "---",
                    f"arxiv_id: {paper_id}",
                    f"experiment: \"{exp_name}\"",
                    "---",
                    "",
                    f"# {exp_name}",
                    "",
                ]
                for key in ("goal", "methodology", "dataset", "setup", "baselines"):
                    val = exp.get(key, "")
                    if val:
                        exp_lines.extend([f"## {key.capitalize()}", "", str(val), ""])
                metrics = exp.get("metrics", {})
                if metrics and isinstance(metrics, dict):
                    exp_lines.extend(["## Metrics", ""])
                    for mk, mv in metrics.items():
                        if mv is None or mv == "":
                            continue
                        exp_lines.append(f"- **{mk}**: {mv}")
                    exp_lines.append("")
                results_text = exp.get("results", "")
                if results_text:
                    exp_lines.extend(["## Results", "", str(results_text), ""])
                findings = exp.get("findings", "")
                if findings:
                    exp_lines.extend(["## Key Findings", "", str(findings), ""])

                safe_exp_lines = [str(item) if not isinstance(item, str) else item for item in exp_lines]
                exp_path = paper_dir / f"{safe_exp}-00-experiment.md"
                with open(exp_path, "w") as f:
                    f.write("\n".join(safe_exp_lines))

        return notes_path
